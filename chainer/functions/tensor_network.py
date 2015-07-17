import math

import numpy

from chainer import cuda
from chainer import function
from chainer.utils import type_check


def _as_vec(x):
    return x.reshape(x.size)

def _as_mat(x):
    return x.reshape(x.shape[0], x.size // x.shape[0])

def _empty_like(x):
    if isinstance(x, cuda.GPUArray):
        return cuda.empty_like(x)
    else:
        return numpy.empty_like(x)

class TensorNetwork(function.Function):

    def __init__(self, in_shape, out_size, nobias=False,
                 initialW=None, initial_bias=None):

        self.W = None
        self.gW = None
        self.V1 = None
        self.gV1 = None
        self.V2 = None
        self.gV2 = None
        self.b = None
        self.gb = None

        assert len(in_shape) == 2
        self.in_shape = in_shape

        if initialW is not None:
            assert initialW.shape == (in_shape[0], in_shape[1], out_size)
            self.W = initialW
        else:
            in_size = numpy.prod(self.in_shape)
            self.W = numpy.random.normal(
                0, math.sqrt(1. / in_size),
                (self.in_shape[0], self.in_shape[1], out_size)).astype(numpy.float32)

        if initial_bias is not None:
            assert len(initial_bias) == 3
            self.V1, self.V2, self.b = initial_bias
        elif not nobias:
            self.V1 = numpy.zeros((self.in_shape[0], out_size), dtype=numpy.float32)
            self.V2 = numpy.zeros((self.in_shape[1], out_size), dtype=numpy.float32)
            self.b = numpy.zeros((out_size, ), dtype=numpy.float32)

        self.gW = _empty_like(self.W)
        if not nobias:
            self.gV1 = _empty_like(self.V1)
            self.gV2 = _empty_like(self.V2)
            self.gb = _empty_like(self.b)

    @property
    def parameter_names(self):
        if self.b is None: # TODO: checking nobias by means of b is not smart?
            return 'W',
        assert self.V1 is not None
        assert self.V2 is not None
        return 'W', 'V1', 'V2', 'b'

    @property
    def gradient_names(self):
        if self.gb is None:
            return 'gW',
        assert self.gV1 is not None
        assert self.gV2 is not None
        return 'gW', 'gV1', 'gV2', 'gb'

    def check_type_forward(self, in_types):
        type_check.expect(
            in_types.size() == 2,
            in_types[0].ndim >= 2,
            in_types[1].ndim >= 2,
            in_types[0].shape[0] == in_types[1].shape[0]
        )

        in_shape = type_check.Variable(self.in_shape, 'in_shape')
        type_check_prod = type_check.Variable(numpy.prod, 'prod')
        type_check.expect(
            type_check_prod(in_types[0].shape[1:]) == in_shape[0],
            type_check_prod(in_types[1].shape[1:]) == in_shape[1]
        )

    def check_type_backward(self, in_types, out_types):
        pass

    def zero_grads(self):
        self.gW.fill(0)
        if self.gb is not None:
            self.gV1.fill(0)
            self.gV2.fill(0)
            self.gb.fill(0)

    def forward_cpu(self, x):
        e1 = _as_mat(x[0])
        e2 = _as_mat(x[1])
        y = numpy.einsum('ij,ik,jkl->il', e1, e2, self.W)
        if self.b is not None:
            y += e1.dot(self.V1)
            y += e2.dot(self.V2)
            y += self.b
        return y,

    def forward_gpu(self, x):
        e1 = _as_vec(x[0])
        e2 = _as_vec(x[1])
        e1e2 = cuda.empty(x[0].shape[0]*x[0].shape[1]*x[1].shape[1], dtype=numpy.float32)

        # 'ij,ik->ijk' with linealized array
        cuda.elementwise(
            'float* y, float* e1, float* e2, int e1c, int e2c',
            '''
            int I = i / e1c / e2c;
            int J = (i-I*e1c*e2c) / e2c;
            int K = i % e2c;
            y[i] = e1[I*e1c+J] * e2[I*e2c+K];
            ''',
            'row_wise_outer_product')(e1e2, e1, e2, x[0].shape[1], x[1].shape[1])

        e1e2 = e1e2.reshape(x[0].shape[0], x[0].shape[1]*x[1].shape[1])
        W_mat = self.W.reshape(self.W.shape[0]*self.W.shape[1], self.W.shape[2])
        y = cuda.empty((x[0].shape[0], self.W.shape[2]), dtype=numpy.float32)
        # 'i[jk],[jk]l->il'
        with cuda.using_cumisc():
            cuda.culinalg.dot(e1e2, W_mat, out=y)

        if self.b is not None:
            e1 = _as_mat(x[0])
            e2 = _as_mat(x[1])
            with cuda.using_cumisc():
                cuda.culinalg.add_dot(e1, self.V1, y)
                cuda.culinalg.add_dot(e2, self.V2, y)
            cuda.elementwise(
                'float* y, float* b, int n_channel',
                'y[i] += b[i % n_channel]',
                'linear_bias')(y, self.b, self.b.size)
        return y,

    def backward_cpu(self, x, gy):
        e1 = _as_mat(x[0])
        e2 = _as_mat(x[1])

        self.gW += numpy.einsum('ij,ik,il->jkl', e1, e2, gy[0])
        if self.b is not None:
            self.gV1 += e1.T.dot(gy[0])
            self.gV2 += e2.T.dot(gy[0])
            self.gb += gy[0].sum(0)

        ge1 = numpy.einsum('ik,jkl,il->ij', e2, self.W, gy[0])
        ge2 = numpy.einsum('ij,jkl,il->ik', e1, self.W, gy[0])
        if self.b is not None:
            ge1 += gy[0].dot(self.V1.T)
            ge2 += gy[0].dot(self.V2.T)
        return (ge1, ge2)

    def backward_gpu(self, x, gy):
        pass
