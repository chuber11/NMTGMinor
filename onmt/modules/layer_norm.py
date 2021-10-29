import math
import torch
import numbers
from torch.nn.parameter import Parameter
from torch.nn import init
from torch.nn import functional as F
import importlib

try:
    from torch.cuda.amp import custom_fwd, custom_bwd
except (ModuleNotFoundError, ImportError) as e:
    from .optimized.compat import custom_fwd, custom_bwd

global fused_layer_norm_cuda
fused_layer_norm_cuda = None


def _cast_if_autocast_enabled(*args):
    if not torch.is_autocast_enabled():
        return args
    else:
        try:
            return torch.cuda.amp.autocast_mode._cast(args, torch.get_autocast_gpu_dtype())
        except AttributeError:
            return torch.cuda.amp.autocast_mode._cast(args, torch.half)


"""
Faster version of Layer Norm from apex (new)
"""

try:
    import fast_layer_norm_cuda

    fast_fused = True
    # print("[INFO] Fast layer norm implementation detected.")
except (ModuleNotFoundError, ImportError) as e:
    fast_layer_norm = None
    fast_fused = False
    # print("[INFO] Fast layer norm implementation not found.")


class FastLayerNormFN(torch.autograd.Function):
    @staticmethod
    @custom_fwd(cast_inputs=torch.float16)
    def forward(ctx, x, gamma, beta, epsilon):
        x = x.contiguous()
        gamma = gamma.contiguous()
        beta = beta.contiguous()
        hidden_size = gamma.numel()
        xmat = x.view((-1, hidden_size))
        ymat, mu, rsigma = fast_layer_norm_cuda.ln_fwd(xmat, gamma, beta, epsilon)
        ctx.save_for_backward(x, gamma, mu, rsigma)
        return ymat.view(x.shape)

    @staticmethod
    @custom_bwd
    def backward(ctx, dy):
        # assert dy.is_contiguous()
        dy = dy.contiguous()  # this happens!
        x, gamma, mu, rsigma = ctx.saved_tensors

        hidden_size = gamma.numel()
        xmat = x.view((-1, hidden_size))
        dymat = dy.view(xmat.shape)
        dxmat, dgamma, dbeta = fast_layer_norm_cuda.ln_bwd(dymat, xmat, mu, rsigma, gamma)
        dx = dxmat.view(x.shape)
        return dx, dgamma, dbeta, None


"""
Fast version of Layer Norm from Apex
"""


class FusedLayerNormAffineFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input, weight, bias, normalized_shape, eps):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        input_ = input.contiguous()
        weight_ = weight.contiguous()
        bias_ = bias.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward_affine(
            input_, ctx.normalized_shape, weight_, bias_, ctx.eps)
        ctx.save_for_backward(input_, weight_, bias_, mean, invvar)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input_, weight_, bias_, mean, invvar = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None
        grad_input, grad_weight, grad_bias = fused_layer_norm_cuda.backward_affine(
            grad_output.contiguous(), mean, invvar,
            input_, ctx.normalized_shape,
            weight_, bias_, ctx.eps)
        return grad_input, grad_weight, grad_bias, None, None


class FusedLayerNormFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input, normalized_shape, eps):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        input_ = input.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward(
            input_, ctx.normalized_shape, ctx.eps)
        ctx.save_for_backward(input_, mean, invvar)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input_, mean, invvar = ctx.saved_tensors
        grad_input = None
        grad_input = fused_layer_norm_cuda.backward(
            grad_output.contiguous(), mean, invvar,
            input_, ctx.normalized_shape,
            ctx.eps)
        return grad_input, None, None


def fast_layer_norm_affine(input, weight, bias, normalized_shape, eps=1e-6):
    return FastLayerNormFN.apply(input, weight, bias, eps)


def fused_layer_norm_affine(input, weight, bias, normalized_shape, eps=1e-6):
    args = _cast_if_autocast_enabled(input, weight, bias, normalized_shape, eps)
    with torch.cuda.amp.autocast(enabled=False):
        return FusedLayerNormAffineFunction.apply(*args)


def fused_layer_norm(input, normalized_shape, eps=1e-6):
    args = _cast_if_autocast_enabled(input, normalized_shape, eps)
    with torch.cuda.amp.autocast(enabled=False):
        return FusedLayerNormFunction.apply(*args)


class FP32LayerNorm(torch.nn.Module):

    def __init__(self, normalized_shape, eps=1e-5, elementwise_affine=True):
        super().__init__()

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        if self.elementwise_affine:
            self.weight = Parameter(torch.Tensor(*normalized_shape))
            self.bias = Parameter(torch.Tensor(*normalized_shape))
        else:
            self.register_parameter('weight', None)
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        if self.elementwise_affine:
            init.ones_(self.weight)
            init.zeros_(self.bias)

    def forward(self, input):

        eps = self.eps

        return F.layer_norm(
            input.float(), self.normalized_shape, self.weight, self.bias, eps).type_as(input)

    def extra_repr(self):
        return '{normalized_shape}, eps={eps}, ' \
               'elementwise_affine={elementwise_affine}'.format(**self.__dict__)


class LayerNorm(torch.nn.Module):
    """
    See LayerNorm for details.

    Note, however, that unlike LayerNorm this norm includes a batch component.
    """

    def __init__(self, normalized_shape, eps=1e-5, elementwise_affine=True):
        super().__init__()

        global fused_layer_norm_cuda
        self.fused = True
        try:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        except ModuleNotFoundError:
            self.fused = False

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        if self.elementwise_affine:
            self.weight = Parameter(torch.Tensor(*normalized_shape))
            self.bias = Parameter(torch.Tensor(*normalized_shape))
        else:
            self.register_parameter('weight', None)
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        if self.elementwise_affine:
            init.ones_(self.weight)
            init.zeros_(self.bias)

    def forward(self, input):

        eps = self.eps

        if not (input.is_cuda and input.type() == 'torch.cuda.HalfTensor') or not self.fused:
            return F.layer_norm(
                input, self.normalized_shape, self.weight, self.bias, eps)
        if self.elementwise_affine:

            # at the moment the super fast layer norm only supports hidden size 1024 :)
            # if fast_fused and input.size(-1) == 1024:
            #     return fast_layer_norm_affine(input, self.weight, self.bias, self.normalized_shape, eps)

            return fused_layer_norm_affine(
                input, self.weight, self.bias, self.normalized_shape, eps)
        else:
            return FusedLayerNormFunction.apply(input, self.normalized_shape, eps)

    def extra_repr(self):
        return '{normalized_shape}, eps={eps}, ' \
               'elementwise_affine={elementwise_affine}'.format(**self.__dict__)


class MultilingualLayerNorm(torch.nn.Module):
    """
    See LayerNorm for details.

    Note, however, that unlike LayerNorm this norm includes a batch component.
    """

    def __init__(self, normalized_shape, eps=1e-5, elementwise_affine=True, n_languages=1):
        super().__init__()
        self.n_languages = n_languages

        global fused_layer_norm_cuda
        self.fused = True
        try:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        except ModuleNotFoundError:
            self.fused = False

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.elementwise_affine = elementwise_affine

        if self.elementwise_affine:
            self.weight = Parameter(torch.Tensor(self.n_languages, *self.normalized_shape))
            self.bias = Parameter(torch.Tensor(self.n_languages, *self.normalized_shape))
        else:
            self.register_parameter('weight', None)
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        if self.elementwise_affine:
            init.ones_(self.weight)
            init.zeros_(self.bias)

    def forward(self, input, factor):

        eps = self.eps

        if self.elementwise_affine:
            weight = torch.index_select(self.weight, 0, factor).squeeze(0)
            bias = torch.index_select(self.bias, 0, factor).squeeze(0)
        else:
            weight, bias = None, None

        if not input.is_cuda or not self.fused:
            return F.layer_norm(
                input, self.normalized_shape, weight, bias, eps)
        if self.elementwise_affine:
            if fast_fused and input.is_cuda:
                return fast_layer_norm_affine(input, weight, bias, self.normalized_shape, eps)

            return fused_layer_norm_affine(
                input, weight, bias, self.normalized_shape, eps)
        else:
            return fused_layer_norm(input, self.normalized_shape, eps)

    def extra_repr(self):
        return '{normalized_shape}, eps={eps}, ' \
               'elementwise_affine={elementwise_affine}'.format(**self.__dict__)
