
import os
import sys
test_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
sys.path.append(test_root)

import common.xpu_test_base
# Owner(s): ["module: nestedtensor"]

import io
import itertools
import sys
from typing import Optional, Tuple
import unittest
from functools import partial
import math

import numpy as np
import torch
import torch.nn
import torch.nn.functional as F
from torch.testing._internal.common_cuda import (
    SM70OrLater, SM80OrLater, PLATFORM_SUPPORTS_FUSED_ATTENTION,
)
from torch.testing._internal.common_device_type import (
    dtypes,
    dtypesIfCUDA,
    instantiate_device_type_tests,
    onlyCPU,
    onlyCUDA,
    skipCUDAIf,
    skipCUDAIfRocm,
    skipMeta,
    PYTORCH_CUDA_MEMCHECK,
)
from torch.testing._internal.common_dtype import floating_types_and_half
from torch.testing._internal.common_utils import (
    decorateIf,
    freeze_rng_state,
    gradcheck,
    instantiate_parametrized_tests,
    IS_FBCODE,
    IS_WINDOWS,
    parametrize,
    run_tests,
    skipIfSlowGradcheckEnv,
    skipIfTorchDynamo,
    markDynamoStrictTest,
    xfailIfTorchDynamo,
    subtest,
    TEST_WITH_ROCM,
    TestCase,
)

from torch.nested._internal.nested_tensor import (
    jagged_from_list,
    NestedTensor,
    nested_view_from_values_offsets,
)

# Tests are ported from pytorch/nestedtensor.
# This makes porting as_nested_tensor easier in the future.


def _iter_constructors():
    # yield as_nested_tensor
    yield torch.nested.nested_tensor

# Helper function to generate a pair of random nested tensors
# one is contiguous, the other is not, but they appear to have same entries
# an output nested tensor consists of
# * `len(ragged_sizes)` matrices
# * matrices[i].shape == (20, ragged_sizes[i])


def random_nt_noncontiguous_pair(ragged_sizes, device="cpu", dtype=torch.float16):
    xs = []
    for size in ragged_sizes:
        xs.append(torch.randn((size, 20), device=device, dtype=dtype))
    # contiguous nested tensor
    ys = []
    for x in xs:
        ys.append(x.transpose(-1, -2))
    nt_contiguous = torch.nested.nested_tensor(ys)
    # noncontiguous nested tensor
    n = len(ragged_sizes)
    nt_noncontiguous = torch.nested.nested_tensor(xs).transpose(-1, -2)
    return nt_contiguous, nt_noncontiguous

# Helper functions to pad a noncontiguous nested tensor
# can be replaced once to_padded_tensor supports noncontiguous memory


def noncontiguous_to_padded_tensor(input, shape=None):
    tensors = input.unbind()
    ntensors = len(tensors)
    assert ntensors > 0
    if shape is None:
        shape = []
        for size in tensors[0].shape:
            shape.append(size)
        for i in range(1, ntensors):
            new_shape = tensors[i].shape
            for j in range(len(shape)):
                shape[j] = max(shape[j], new_shape[j])
        shape = [ntensors] + shape
    result = tensors[0].new_zeros(shape)
    for itensor in range(ntensors):
        tensor = tensors[itensor]
        view = result[itensor]
        for idim in range(tensor.dim()):
            view = view.narrow(idim, 0, tensor.size(idim))
        view.copy_(tensor)
    return result

# Helper function to generate a random nested tensor


def random_nt(device, dtype, num_tensors, max_dims, min_dims=None, layout=torch.strided, require_non_empty=True):
    if min_dims is None:
        min_dims = tuple([0] * len(max_dims))

    assert len(max_dims) == len(min_dims)
    for min_dim, max_dim in zip(min_dims, max_dims):
        assert max_dim > min_dim, "random_nt: max_dim must be greater than min_dim"
        assert min_dim >= 0, "random_nt: min_dim must be non-negative"
        if require_non_empty:
            assert not (min_dim == 0 and max_dim == 1), (
                "random_nt: zero cannot be the only possible value if require_non_empty is True"
            )

    if require_non_empty:
        # Select a random idx that will be required to be non-empty
        non_zero_idx = torch.randint(low=0, high=num_tensors, size=(1,)).item()

    ts1 = []
    for i, _ in enumerate(range(num_tensors)):
        tensor_dims = []
        for min_dim, max_dim in zip(min_dims, max_dims):
            new_min_dim = min_dim
            if require_non_empty and i == non_zero_idx and min_dim == 0:
                new_min_dim = 1
            tensor_dims.append(torch.randint(low=new_min_dim, high=max_dim, size=(1,)).item())
        t1 = torch.randn(tensor_dims, device=device, dtype=dtype)
        ts1.append(t1)

    return torch.nested.nested_tensor(ts1, device=device, dtype=dtype, layout=layout)


# Alternate approach to generating a random NT.
# dims should be something like [5, None, 10], with None indicating that a
# random ragged structure should be used
def random_nt_from_dims(dims, device=None, dtype=None, layout=torch.strided, requires_grad=False):
    sizes = [
        [d if d is not None else torch.randint(2, 10, size=(1,)).item() for d in dims[1:]]
        for d in range(dims[0])
    ]
    return torch.nested.nested_tensor([
        torch.randn(*size) for size in sizes
    ], device=device, dtype=dtype, layout=layout, requires_grad=requires_grad)


# Creates an NT matching another NT's number of components and
# shape / ragged structure for all dims specified to be -1.
def random_nt_from_similar(other, dims=None):
    if dims is None:
        return torch.randn_like(other)
    assert len(dims) == other.dim()
    assert dims[0] == -1 or dims[0] == other.size(0)

    ret_sizes = []
    for t in other.unbind():
        other_size = t.shape
        ret_size = []
        for i, d in enumerate(dims[1:]):
            if d == -1:
                ret_size.append(other_size[i])
            else:
                ret_size.append(d)
        ret_sizes.append(ret_size)

    return torch.nested.nested_tensor([
        torch.randn(*size) for size in ret_sizes
    ], device=other.device)


# makes naming nice for tests that parametrize over layout.
def layout_name(layout):
    # e.g. "torch.jagged" -> "jagged"
    return layout.__repr__().split(".")[-1]


@markDynamoStrictTest
class TestNestedTensor(TestCase):
    @parametrize("batch_size", [2, 4])
    @parametrize("max_seq_len", [3, 5])
    @parametrize("vocab_size", [10, 20])
    def test_2d_nested_tensor(self, batch_size, max_seq_len, vocab_size):
        data = []
        nested_tensor_ref_list = []
        for _ in range(batch_size):
            if max_seq_len == 0:
                length = 0
            else:
                length = np.random.randint(low=1, high=max_seq_len)
            row = list(np.random.randint(low=0, high=vocab_size, size=(length,)))
            data.append(row)
            nested_tensor_ref_list.append(torch.Tensor(row))
        nested_tensor = torch.nested.nested_tensor(data, dtype=torch.int64)
        nested_tensor_list = nested_tensor.unbind()
        for id in range(batch_size):
            self.assertEqual(
                nested_tensor_list[id],
                nested_tensor_ref_list[id].type(torch.int64)
            )

    @parametrize("batch_size", [2, 4])
    @parametrize("max_seq_len", [3, 5])
    @parametrize("vocab_size", [10, 20])
    def test_3d_nested_tensor(self, batch_size, max_seq_len, vocab_size):
        data = []
        nested_tensor_ref_list = []
        for _ in range(batch_size):
            if max_seq_len == 0:
                length = 0
            else:
                length = np.random.randint(low=1, high=max_seq_len)
            row = list(np.random.randint(low=0, high=vocab_size, size=(length,)))
            row = [list(item * np.arange(max_seq_len)) for item in row]
            data.append(row)
            nested_tensor_ref_list.append(torch.Tensor(row))
        nested_tensor = torch.nested.nested_tensor(data, dtype=torch.int64)
        nested_tensor_list = nested_tensor.unbind()
        for id in range(batch_size):
            self.assertEqual(
                nested_tensor_list[id],
                nested_tensor_ref_list[id].type(torch.int64)
            )

    @parametrize("batch_size", [2, 4])
    @parametrize("max_seq_len", [3, 5])
    @parametrize("vocab_size", [10, 20])
    def test_3d_nested_tensor_float(self, batch_size, max_seq_len, vocab_size):
        data = []
        nested_tensor_ref_list = []
        for _ in range(batch_size):
            if max_seq_len == 0:
                length = 0
            else:
                length = np.random.randint(low=1, high=max_seq_len)
            row = list(
                np.random.randint(low=0, high=vocab_size, size=(length,)).astype(float)
            )
            row = [list(item * np.arange(max_seq_len)) for item in row]
            data.append(row)
            nested_tensor_ref_list.append(torch.Tensor(row))
        nested_tensor = torch.nested.nested_tensor(data, dtype=torch.float)
        nested_tensor_list = nested_tensor.unbind()
        for id in range(batch_size):
            self.assertEqual(
                nested_tensor_list[id],
                nested_tensor_ref_list[id].type(torch.float)
            )


    @torch.inference_mode()
    def _test_unbind_case(self, a, b):
        nt = torch.nested.nested_tensor([a, b])
        a1, b1 = nt.unbind()
        self.assertTrue(a is not a1)
        self.assertTrue(b is not b1)

        nt = torch.nested.nested_tensor([a, b], dtype=a.dtype)
        a1, b1 = nt.unbind(0)
        self.assertEqual(a, a1)
        self.assertEqual(b, b1)

        a = torch.randn((2, 3)).add_(1)
        nt = torch.nested.nested_tensor([a])
        self.assertEqual(a, nt.unbind(0)[0])

    @torch.inference_mode()
    def test_unbind_0(self):
        self._test_unbind_case(
            torch.tensor([1, 2]), torch.tensor([7, 8]),
        )

    @torch.inference_mode()
    def test_unbind_1(self):
        self._test_unbind_case(
            torch.tensor([1]), torch.tensor([7]),
        )

    @torch.inference_mode()
    def test_unbind_3(self):
        self._test_unbind_case(
            torch.tensor([1.0]), torch.tensor([]),
        )

    @torch.inference_mode()
    def test_unbind_4(self):
        self._test_unbind_case(
            torch.tensor([]), torch.tensor([]),
        )

    @torch.inference_mode()
    def test_unbind_dim(self):
        def _test_fn(unbind_fn):
            a = torch.rand(3, 2)
            b = torch.rand(2, 3)
            nt = torch.nested.nested_tensor([a, b])
            self.assertRaises(RuntimeError, lambda: unbind_fn(nt, 1))

        # Both of these tests are necessary, because we're using
        # torch_function.
        _test_fn(lambda x, dim: x.unbind(dim))
        # TODO: Re-enable this once using torch_dispatch
        # _test_fn(lambda x, dim: torch.unbind(x, dim))

    @torch.inference_mode()
    def test_nested_tensor(self):
        self.assertRaises(TypeError, lambda: torch.nested.nested_tensor(torch.tensor([3.0])))
        self.assertRaises(TypeError, lambda: torch.nested.nested_tensor(4.0))

    @torch.inference_mode()
    def test_nested_tensor_matching_dim(self):
        self.assertRaisesRegex(
            RuntimeError,
            "Found dimension 1 for Tensor at index 1 and dimension 0 for Tensor at index 0.",
            lambda: torch.nested.nested_tensor([torch.tensor(1.0), torch.tensor([])]),
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Found dimension 1 for Tensor at index 2 and dimension 0 for Tensor at index 1.",
            lambda: torch.nested.nested_tensor(
                [torch.tensor(1.0), torch.tensor(2.0), torch.tensor([])]
            ),
        )

    @torch.inference_mode()
    def test_default_nested_tensor(self):
        self.assertRaises(TypeError, lambda: torch.nested.nested_tensor())
        default_nested_tensor = torch.nested.nested_tensor([])
        default_tensor = torch.tensor([])
        # self.assertEqual(default_nested_tensor.nested_dim(), 1)
        # self.assertEqual(default_nested_tensor.nested_size(), ())
        self.assertEqual(default_nested_tensor.dim(), default_tensor.dim())
        self.assertEqual(default_nested_tensor.layout, default_tensor.layout)
        self.assertEqual(default_nested_tensor.device, default_tensor.device)
        self.assertEqual(default_nested_tensor.dtype, default_tensor.dtype)
        self.assertEqual(
            default_nested_tensor.requires_grad, default_tensor.requires_grad
        )
        self.assertIsNone(default_tensor.grad)
        # TODO: Re-enable once we have a performance driven
        # use case and implementation.
        # self.assertEqual(default_nested_tensor.is_pinned(),
        #                  default_tensor.is_pinned())

    @torch.inference_mode()
    def test_dim(self):
        for constructor in _iter_constructors():
            a1 = constructor([])
            self.assertEqual(a1.dim(), 1)
            a1 = constructor([torch.tensor(3.0)])
            self.assertEqual(a1.dim(), 1)
            a1 = constructor([torch.tensor([1, 2, 3, 4])])
            self.assertEqual(a1.dim(), 2)

    @unittest.skipIf(IS_FBCODE, "numel is not virtual in fbcode.")
    @torch.inference_mode()
    def test_numel(self):
        for constructor in _iter_constructors():
            a1 = constructor([])
            self.assertEqual(a1.numel(), 0)
            a1 = constructor([torch.tensor(3.0), torch.tensor(4.0)])
            self.assertEqual(a1.numel(), 2)
            a1 = constructor([torch.randn(2, 2, 2)])
            self.assertEqual(a1.numel(), 8)
            a1 = constructor([torch.randn([1, 2, 3]), torch.randn(3, 2, 1)])
            self.assertEqual(a1.numel(), 12)
            a1 = constructor([torch.randn([1, 1, 3]), torch.randn(3, 2, 4)])
            self.assertEqual(a1.numel(), 27)
            a1 = constructor([torch.randn([5, 5, 5]), torch.randn(6, 6, 6)])
            self.assertEqual(a1.numel(), 341)

            # Interesting edge case
            a1 = constructor([torch.randn([1, 2, 3]), torch.randn(1, 2, 0)])
            self.assertEqual(a1.numel(), 6)

    @torch.inference_mode()
    def test_size(self):
        for constructor in _iter_constructors():
            a1 = constructor([])
            self.assertRaisesRegex(
                RuntimeError,
                "NestedTensorImpl doesn't support sizes",
                lambda: a1.size(),
            )

    def test_size_dim(self):
        a = torch.nested.nested_tensor([])
        self.assertEqual(a.size(0), 0)

        a = torch.nested.nested_tensor([torch.tensor(1)])
        self.assertEqual(a.size(0), 1)

        a = torch.nested.nested_tensor([torch.tensor(1), torch.tensor(2)])
        self.assertEqual(a.size(0), 2)

        a = torch.nested.nested_tensor([torch.rand(1, 2),
                                        torch.rand(1, 8)])
        self.assertEqual(a.size(0), 2)
        self.assertEqual(a.size(1), 1)
        self.assertRaisesRegex(
            RuntimeError, "Given dimension 2 is irregular and does not have a size", lambda: a.size(2))

        a = torch.nested.nested_tensor([torch.rand(3, 4),
                                        torch.rand(5, 4)])
        self.assertEqual(a.size(0), 2)
        self.assertRaisesRegex(
            RuntimeError, "Given dimension 1 is irregular and does not have a size", lambda: a.size(1))
        self.assertEqual(a.size(2), 4)

    @unittest.skipIf(IS_FBCODE, "stride is not virtual in fbcode.")
    @torch.inference_mode()
    def test_stride(self):
        for constructor in _iter_constructors():
            a1 = constructor([])
            self.assertRaisesRegex(
                RuntimeError,
                "NestedTensorImpl doesn't support strides",
                lambda: a1.stride(),
            )

    @unittest.skipIf(IS_FBCODE, "is_contiguous is not virtual in fbcode.")
    @torch.inference_mode()
    def test_is_contiguous(self):
        # Test empty case
        nt_empty = torch.nested.nested_tensor([])
        assert nt_empty.is_contiguous()
        self.assertEqual(nt_empty, nt_empty.contiguous())

        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7))

        # Test contiguous case
        assert nt_contiguous.is_contiguous()
        self.assertEqual(nt_contiguous, nt_contiguous.contiguous())

        # Test non_contiguous case
        assert not nt_noncontiguous.is_contiguous()
        self.assertEqual(nt_contiguous, nt_noncontiguous.contiguous())

        # Test querying by memory_format
        self.assertTrue(nt_contiguous.is_contiguous(memory_format=torch.contiguous_format))
        self.assertTrue(not nt_noncontiguous.is_contiguous(memory_format=torch.contiguous_format))

    @torch.inference_mode()
    def test_repr_string(self):
        a = torch.nested.nested_tensor([])
        expected = "nested_tensor([\n\n])"
        self.assertEqual(str(a), expected)
        self.assertEqual(repr(a), expected)

        a = torch.nested.nested_tensor([torch.tensor(1.0)])
        expected = "nested_tensor([\n  tensor(1.)\n])"
        self.assertEqual(str(a), expected)
        self.assertEqual(repr(a), expected)

        a = torch.nested.nested_tensor([torch.tensor([[1, 2]]), torch.tensor([[4, 5]])])
        expected = "nested_tensor([\n  tensor([[1, 2]]),\n  tensor([[4, 5]])\n])"
        self.assertEqual(str(a), expected)
        self.assertEqual(repr(a), expected)

    def test_to_padded_tensor_on_empty_tensor(self):

        nt = torch.nested.nested_tensor([])
        empty = torch.nested.to_padded_tensor(nt, 4)
        self.assertEqual(empty, torch.tensor([]))

    def test_nested_namespace(self):
        nt = torch.nested.nested_tensor([torch.randn(2, 3), torch.randn(4, 5)])
        result = nt.to_padded_tensor(4)
        nested_namespace_result = torch.nested.to_padded_tensor(nt, 4)
        self.assertEqual(result, nested_namespace_result)

    def test_to(self):
        ntensors = 4
        nt = random_nt(torch.device('cpu'), torch.float32, ntensors, (4, 4))

        def test_copy_behavior(t, non_blocking=False):
            self.assertIs(t, t.to(t, non_blocking=non_blocking))
            self.assertIs(t, t.to(t.dtype, non_blocking=non_blocking))
            self.assertIs(t, t.to(torch.empty_like(t), non_blocking=non_blocking))
            self.assertIsNot(t, t.to(t, non_blocking=non_blocking, copy=True))
            self.assertIsNot(t, t.to(t.dtype, non_blocking=non_blocking, copy=True))
            self.assertIsNot(t, t.to(torch.empty_like(t), non_blocking=non_blocking, copy=True))

            devices = [t.device]
            if t.device.type == 'cuda':
                if t.device.index == -1:
                    devices.append(f'cuda:{torch.cuda.current_device()}')
                elif t.device.index == torch.cuda.current_device():
                    devices.append('cuda')
            for device in devices:
                self.assertIs(t, t.to(device, non_blocking=non_blocking))
                self.assertIs(t, t.to(device, t.dtype, non_blocking=non_blocking))
                self.assertIsNot(t, t.to(device, non_blocking=non_blocking, copy=True))
                self.assertIsNot(t, t.to(device, t.dtype, non_blocking=non_blocking, copy=True))

        test_copy_behavior(nt)
        self.assertEqual(nt.device, nt.to('cpu').device)
        self.assertEqual(nt.device, nt.to('cpu', dtype=torch.float32).device)
        self.assertIs(torch.float32, nt.to('cpu', dtype=torch.float32).dtype)
        self.assertEqual(nt.device, nt.to(torch.float32).device)
        self.assertIs(torch.float32, nt.to(dtype=torch.float32).dtype)

        def test_data_ptr(getter):
            self.assertEqual(getter(nt), getter(nt.to('cpu')))
            self.assertEqual(getter(nt), getter(nt.to(dtype=nt.dtype, device=nt.device, copy=False)))
            self.assertEqual(getter(nt), getter(nt.to('cpu', copy=False)))
            self.assertNotEqual(getter(nt), getter(nt.to('cpu', copy=True)))

        test_data_ptr(lambda nt: nt.data_ptr())

        if torch.cuda.is_available():
            for non_blocking in [True, False]:
                for cuda in ['cuda', 'cuda:0' if torch.cuda.device_count() == 1 else 'cuda:1']:
                    nt2 = random_nt(cuda, torch.float32, ntensors, (4, 4))
                    test_copy_behavior(nt2, non_blocking)
                    self.assertEqual(nt2.device, nt2.to(cuda, non_blocking=non_blocking).device)
                    self.assertEqual(nt.device, nt2.to('cpu', non_blocking=non_blocking).device)
                    self.assertEqual(nt2.device, nt.to(cuda, non_blocking=non_blocking).device)
                    self.assertIs(torch.int32, nt2.to('cpu', dtype=torch.int32, non_blocking=non_blocking).dtype)
                    self.assertEqual(nt.device, nt2.to('cpu', dtype=torch.int32, non_blocking=non_blocking).device)
                    self.assertIs(torch.int32, nt2.to(dtype=torch.int32).dtype)
                    self.assertEqual(nt2.device, nt2.to(dtype=torch.int32).device)

    def test_copy_(self):
        ntensors = 4
        nt = random_nt(torch.device('cpu'), torch.float32, ntensors, (4, 4))
        nt_copy = torch.empty_like(nt)
        nt_copy.copy_(nt)

        for (nt_ub, nt_copy_ub) in zip(nt.unbind(), nt_copy):
            self.assertEqual(nt_ub, nt_copy_ub)

        nt_error = torch.nested.nested_tensor([torch.tensor([0, 0])])
        self.assertRaisesRegex(
            RuntimeError,
            "copy_ only supports tensors that are the same size for Nested implementations",
            lambda: nt_error.copy_(nt)
        )

        if torch.cuda.is_available():
            nt = random_nt(torch.device('cuda'), torch.float32, ntensors, (4, 4))
            nt_copy = torch.empty_like(nt, device=torch.device('cpu'))
            nt_copy.copy_(nt, non_blocking=True)
            torch.cuda.current_stream(torch.cuda.current_device()).synchronize()
            for (nt_ub, nt_copy_ub) in zip(nt.unbind(), nt_copy):
                self.assertEqual(nt_ub, nt_copy_ub)

            nt_copy = torch.empty_like(nt, device=torch.device('cpu'))
            nt_copy.copy_(nt, non_blocking=False)
            for (nt_ub, nt_copy_ub) in zip(nt.unbind(), nt_copy):
                self.assertEqual(nt_ub, nt_copy_ub)

    def test_fill_(self):
        ntensors = 4
        nt = random_nt(torch.device('cpu'), torch.float32, ntensors, (4, 4))
        nt.fill_(10.)
        for nt_ub in nt.unbind():
            t = torch.empty_like(nt_ub)
            t.fill_(10.)
            self.assertEqual(nt_ub, t)

        fill_tensor = torch.tensor([11.])
        self.assertRaisesRegex(
            RuntimeError,
            "fill_ only supports 0-dimension value tensor",
            lambda: nt.fill_(fill_tensor)
        )

        nt.fill_(fill_tensor[0])
        for nt_ub in nt.unbind():
            t = torch.empty_like(nt_ub)
            t.fill_(11.)
            self.assertEqual(nt_ub, t)

    def test_zero_(self):
        ntensors = 4
        nt = random_nt(torch.device('cpu'), torch.float32, ntensors, (4, 4))
        nt.zero_()
        for nt_ub in nt.unbind():
            t = torch.empty_like(nt_ub)
            t.fill_(0.)
            self.assertEqual(nt_ub, t)

    @parametrize("func", [torch.ones_like, torch.zeros_like, torch.randn_like],
                 name_fn=lambda f: f.__name__)
    def test_like_functions(self, func):
        ntensors = 4
        nt = random_nt(torch.device('cpu'), torch.float32, ntensors, (4, 4))
        torch.manual_seed(1)
        nt_like = func(nt)

        torch.manual_seed(1)
        for nt_ub in nt_like.unbind():
            t_like = func(nt_ub)
            self.assertEqual(nt_ub, t_like)

    def test_cat(self):
        # dim=0 success case
        # No constraints on ragged structures matching.
        x = random_nt_from_dims([5, None, 10])
        y = random_nt_from_dims([3, 4, None])
        output = torch.cat([x, y], dim=0)
        for out_component, xy_component in zip(
                output.unbind(), itertools.chain(x.unbind(), y.unbind())):
            self.assertEqual(out_component, xy_component)

        # dim=-1 success case
        # shape (B, *, D)
        x = random_nt_from_dims([5, None, 10])
        # shape (B, *, D'); same structure as x but dim=-1 differs
        y = random_nt_from_similar(x, dims=[-1, -1, 8])
        # should be shape (B, *, D + D') when supported
        output = torch.cat([x, y], dim=-1)
        for out_component, x_component, y_component in zip(output.unbind(), x.unbind(), y.unbind()):
            self.assertEqual(out_component, torch.cat([x_component, y_component], dim=-1))

        # dim between 0 and -1 success case
        x = random_nt_from_dims([5, None, 2, 3])
        # same structure as x but dim=2 differs
        y = random_nt_from_similar(x, dims=[-1, -1, 4, -1])
        output = torch.cat([x, y], dim=2)
        for out_component, x_component, y_component in zip(output.unbind(), x.unbind(), y.unbind()):
            self.assertEqual(out_component, torch.cat([x_component, y_component], dim=1))

        # error case: mixed NT / dense inputs
        x = random_nt_from_dims([5, None, 2])
        y = torch.randn(5, 3, 2)
        with self.assertRaisesRegex(
                RuntimeError, "expected each tensor in given list to be nested"):
            torch.cat([x, y], dim=-1)

        # error case: NTs with different dims
        x = random_nt_from_dims([5, None, 2])
        y = random_nt_from_dims([5, None, 2, 3])
        with self.assertRaisesRegex(
                RuntimeError, "expected all nested tensors to have matching ragged structures outside of the concatenated dim"):
            torch.cat([x, y], dim=-1)

        # error case: non-contiguous NT
        x, y = random_nt_noncontiguous_pair((2, 3, 4), dtype=torch.float32)
        # transpose to put ragged dim next to batch dim
        x, y = x.transpose(-2, -1), y.transpose(-2, -1)
        with self.assertRaisesRegex(
                RuntimeError, "only contiguous nested tensors are supported"):
            torch.cat([x, y], dim=-1)

        # error case: multiple ragged dims in inputs
        x = random_nt_from_dims([5, None, None, 2])
        y = random_nt_from_similar(x)
        with self.assertRaisesRegex(
                RuntimeError, "only nested tensors with a single ragged dim next to the batch dim are supported"):
            torch.cat([x, y], dim=-1)

        # error case: ragged dim not next to batch dim
        x = random_nt_from_dims([5, 2, None])
        y = random_nt_from_similar(x)
        with self.assertRaisesRegex(
                RuntimeError, "only nested tensors with a single ragged dim next to the batch dim are supported"):
            torch.cat([x, y], dim=1)

        # error case: NTs with different batch sizes
        x = random_nt_from_dims([5, None, 2])
        y = random_nt_from_dims([3, None, 2])
        with self.assertRaisesRegex(
                RuntimeError, "expected all nested tensors to have matching ragged structures outside of the concatenated dim"):
            torch.cat([x, y], dim=-1)

        # error case: NTs with different ragged structures
        x = torch.nested.nested_tensor([
            torch.randn(2, 6),
            torch.randn(4, 6),
            torch.randn(5, 6),
        ])
        y = torch.nested.nested_tensor([
            torch.randn(5, 6),
            torch.randn(4, 6),
            torch.randn(2, 6),
        ])
        with self.assertRaisesRegex(
                RuntimeError, "expected all nested tensors to have matching ragged structures outside of the concatenated dim"):
            torch.cat([x, y], dim=-1)


@markDynamoStrictTest
class TestNestedTensorDeviceType(TestCase):
    # Helper function to generate a pair of random nested tensors
    # the 2 nested tensors have same shapes
    def random_nt_pair(self, device, dtype, num_tensors, max_dims):
        ts1 = []
        ts2 = []
        for _ in range(num_tensors):
            tensor_dims = tuple([torch.randint(low=0, high=max_dim, size=(1,)).item() for max_dim in max_dims])
            t1 = torch.randn(tensor_dims, device=device, dtype=dtype)
            t2 = torch.randn(tensor_dims, device=device, dtype=dtype)
            ts1.append(t1)
            ts2.append(t2)
        return (torch.nested.nested_tensor(ts1, device=device, dtype=dtype),
                torch.nested.nested_tensor(ts2, device=device, dtype=dtype))

    @dtypes(*floating_types_and_half())
    def test_detach(self, device, dtype):
        a = torch.randn(2, 4, device=device, dtype=dtype, requires_grad=False)
        b = torch.randn(5, 4, device=device, dtype=dtype, requires_grad=False)
        x = torch.nested.nested_tensor([a, b], requires_grad=True)

        x_detach = x.detach()

        z = x_detach * 4
        self.assertFalse(x_detach.requires_grad)
        self.assertFalse(z.requires_grad)

        a = torch.randn(2, 4, device=device, dtype=dtype, requires_grad=True)
        b = torch.randn(5, 4, device=device, dtype=dtype, requires_grad=True)
        x = torch.nested.as_nested_tensor([a, b])

        y = x * 2
        y = y.detach()
        self.assertFalse(y.requires_grad)
        self.assertIsNone(y.grad_fn)

        z = x + y
        torch.nested.to_padded_tensor(z, 0).sum().backward()
        # This is an incorrect gradient, but we assume that's what the user
        # wanted. detach() is an advanced option.
        self.assertEqual(a.grad, torch.ones(2, 4, device=device, dtype=dtype))
        self.assertEqual(b.grad, torch.ones(5, 4, device=device, dtype=dtype))

    @dtypes(torch.float, torch.float16, torch.double)
    def test_unbind_noncontiguous(self, device, dtype):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7), device, dtype)
        ub_contiguous = nt_contiguous.unbind()
        ub_noncontiguous = nt_noncontiguous.unbind()
        self.assertEqual(len(ub_contiguous), len(ub_noncontiguous))
        n = len(ub_contiguous)
        for i in range(n):
            self.assertEqual(ub_contiguous[i], ub_noncontiguous[i])

    @dtypes(torch.float)
    @skipMeta
    def test_to_then_from_padded_tensor_no_transform0213(self, device, dtype):
        t = torch.randn(4, 4, 4, device=device, dtype=dtype)
        ts = list(torch.unbind(t))
        ts[0] = ts[0][:-1]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        padded = torch.nested.to_padded_tensor(nt, 0)

        nt_to = torch._nested_from_padded_and_nested_example(padded, nt)

        for (t1, t2) in zip(nt.unbind(), nt_to.unbind()):
            self.assertEqual(t1, t2)
        self.assertEqual(nt.device, nt_to.device)

    @dtypes(torch.float)
    @dtypesIfCUDA(torch.float, torch.half)
    @skipMeta
    @torch.inference_mode()
    def test_layer_norm(self, device, dtype):
        def _test(size):
            # Simple shapes test
            t0 = torch.randn(2, size, device=device, dtype=dtype, requires_grad=False)
            t1 = torch.randn(2, size, device=device, dtype=dtype, requires_grad=False)
            ts = [t0, t1, t0, t1]
            nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
            layer_norm = torch.nn.LayerNorm(size, device=device, dtype=dtype)
            nt_result = layer_norm(nt)
            for (nt_subresult, t) in zip(nt_result.unbind(), ts):
                t_result = layer_norm(t.reshape(1, -1, size).squeeze(0))
                self.assertEqual(nt_subresult, t_result)

            # More complex nt test with different lengths for each tensor
            t0 = torch.randn(4, size, device=device, dtype=dtype, requires_grad=False)
            t1 = torch.randn(10, size, device=device, dtype=dtype, requires_grad=False)
            t2 = torch.randn(7, size, device=device, dtype=dtype, requires_grad=False)
            ts = [t0, t1, t2, t0, t2]
            nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
            layer_norm = torch.nn.LayerNorm(size, device=device, dtype=dtype)
            nt_result = layer_norm(nt)
            for (nt_subresult, t) in zip(nt_result.unbind(), ts):
                t_result = layer_norm(t.reshape(1, -1, size).squeeze(0))
                self.assertEqual(nt_subresult, t_result)

            if size <= 128:
                # Test with multidimensional tensors after irregular dim
                # (run only with smaller dimensions to ensure fast execution)
                t0 = torch.randn(4, size, size, 4, device=device, dtype=dtype, requires_grad=False)
                t1 = torch.randn(10, size, size, 4, device=device, dtype=dtype, requires_grad=False)
                t2 = torch.randn(7, size, size, 4, device=device, dtype=dtype, requires_grad=False)
                ts = [t0, t1, t2, t0, t2]
                nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
                layer_norm = torch.nn.LayerNorm((size, size, 4), device=device, dtype=dtype)
                nt_result = layer_norm(nt)
                for (nt_subresult, t) in zip(nt_result.unbind(), ts):
                    t_result = layer_norm(t.reshape(1, -1, size, size, 4).squeeze(0))
                    self.assertEqual(nt_subresult, t_result)

                # Test where the normalizing dimensions are not all
                layer_norm = torch.nn.LayerNorm((size, 4), device=device, dtype=dtype)
                nt_result = layer_norm(nt)
                for (nt_subresult, t) in zip(nt_result.unbind(), ts):
                    t_result = layer_norm(t.reshape(1, -1, size, size, 4).squeeze(0))
                    self.assertEqual(nt_subresult, t_result)

        for size in (1024, 1023, 513, 512, 256, 128, 2, 4, 32):
            _test(size)

    @dtypes(torch.float)
    @dtypesIfCUDA(torch.float, torch.half)
    @skipMeta
    @torch.inference_mode()
    def test_layer_norm_breaking(self, device, dtype):
        size = 128
        t0 = torch.randn(4, size, size, 4, device=device, dtype=dtype, requires_grad=False)
        t1 = torch.randn(10, size, size, 4, device=device, dtype=dtype, requires_grad=False)
        t2 = torch.randn(7, size, size, 4, device=device, dtype=dtype, requires_grad=False)
        ts = [t0, t1, t2, t0, t2]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        layer_norm = torch.nn.LayerNorm((4, size, size, 4), device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "normalized_shape extends into irregular dimensions for the nested tensor",
            lambda: layer_norm(nt),
        )
        layer_norm = torch.nn.LayerNorm((size + 1, size, 4), device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "The shape at dimension 0",
            lambda: layer_norm(nt),
        )

    @decorateIf(
        xfailIfTorchDynamo,
        # only fails in python 3.11. TODO: Ensure this is fixed once views work!
        lambda params: params["layout"] == torch.jagged and sys.version_info >= (3, 11)
    )
    @parametrize("layout", [torch.strided, torch.jagged], name_fn=layout_name)
    def test_embedding(self, device, layout):
        inputs = [
            torch.randint(100, (L,), device=device, dtype=torch.int64)
            for L in torch.randint(5, 50, (8,))
        ]
        x = torch.nested.nested_tensor(inputs, device=device, dtype=torch.int64, layout=layout)
        emb = torch.nn.Embedding(100, 8, device=device)
        y = emb(x)
        ys = y.unbind()
        for i, inp in enumerate(inputs):
            self.assertEqual(emb(inp), ys[i])


    @skipMeta
    @torch.inference_mode()
    @dtypes(*floating_types_and_half())
    def test_masked_fill(self, device, dtype):
        # nested tensor * nested tensor
        (nt, mask) = self.random_nt_pair(device, dtype, 4, (4, 4))
        mask = torch.nested.nested_tensor([m < 0 for m in mask.unbind()])
        ref = torch.nested.nested_tensor([t.masked_fill(m, 0) for (t, m) in zip(nt.unbind(), mask.unbind())])
        out = nt.masked_fill(mask, 0)
        self.assertEqual(ref, out)


    @dtypes(torch.float, torch.float16)
    def test_to_padded_tensor_simple(self, device, dtype):
        t = torch.randn(4, 4, 4, device=device, dtype=dtype)
        ts = list(torch.unbind(t))
        ts[0] = ts[0][:-1]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        for padding_value in (0, 1):
            padded = torch.nested.to_padded_tensor(nt, padding_value)

            correct_output = t.clone()
            if padding_value == 0:
                correct_output[0][-1] = torch.zeros_like(correct_output[0][-1])
            else:
                correct_output[0][-1] = torch.ones_like(correct_output[0][-1])

            self.assertEqual(padded, correct_output)
            self.assertEqual(padded.device, torch.device(device))
            self.assertEqual(padded.dtype, dtype)

    @dtypes(torch.float, torch.float16)
    def test_to_padded_tensor_output_size(self, device, dtype):
        t = torch.randn(4, 4, 4, device=device, dtype=dtype)
        output_size = (4, 6, 5)
        ts = list(torch.unbind(t))
        ts[0] = ts[0][:-1]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        for padding_value in (0, 1):
            padded = torch.nested.to_padded_tensor(nt, padding_value, output_size=output_size)
            correct_output = torch.ones(output_size, device=device, dtype=dtype) * padding_value
            correct_output[:4:, :4, :4] = t.clone()
            if padding_value == 0:
                correct_output[0][3] = torch.zeros_like(correct_output[0][3])
            else:
                correct_output[0][3] = torch.ones_like(correct_output[0][3])

            self.assertEqual(padded, correct_output)
            self.assertEqual(padded.device, torch.device(device))
            self.assertEqual(padded.dtype, dtype)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_to_padded_tensor_dim2(self, device, dtype):
        ts = [
            torch.randn(160, device=device, dtype=dtype),
            torch.randn(1240, device=device, dtype=dtype),
            torch.randn(2400, device=device, dtype=dtype),
        ]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        pad = 42
        correct_output = []
        for t in ts:
            next_output = torch.ones_like(ts[2]) * pad
            correct_output.append(next_output)
            next_output[:t.size(0)].copy_(t)
        correct_output = torch.stack(correct_output)
        padded = torch.nested.to_padded_tensor(nt, pad)
        self.assertEqual(padded, correct_output)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_to_padded_tensor_dim3(self, device, dtype):
        ts = [
            torch.randn(16, 21, device=device, dtype=dtype),
            torch.randn(24, 32, device=device, dtype=dtype),
            torch.randn(40, 53, device=device, dtype=dtype),
        ]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        pad = 42
        correct_output = []
        for t in ts:
            next_output = torch.ones_like(ts[2]) * pad
            correct_output.append(next_output)
            next_output[:t.size(0), :t.size(1)].copy_(t)
        correct_output = torch.stack(correct_output)
        padded = torch.nested.to_padded_tensor(nt, pad)
        self.assertEqual(padded, correct_output)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_to_padded_tensor_dim4(self, device, dtype):
        ts = [
            torch.randn(16, 21, 13, device=device, dtype=dtype),
            torch.randn(24, 32, 14, device=device, dtype=dtype),
            torch.randn(40, 53, 16, device=device, dtype=dtype),
        ]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        pad = 42
        correct_output = []
        for t in ts:
            next_output = torch.ones_like(ts[2]) * pad
            correct_output.append(next_output)
            next_output[:t.size(0), :t.size(1), :t.size(2)].copy_(t)
        correct_output = torch.stack(correct_output)
        padded = torch.nested.to_padded_tensor(nt, pad)
        self.assertEqual(padded, correct_output)

    # TODO: test noncontiguous to_padded_tensor
    # For now this tests the functionality of noncontiguous_to_padded_tensor
    # and the error message of to_padded_tensor
    # since to_padded_tensor does not support noncontiguous buffer yet
    @dtypes(torch.float, torch.float16, torch.double)
    @torch.inference_mode()
    def test_to_padded_tensor_noncontiguous(self, device, dtype):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7), device, dtype)
        # test noncontiguous_to_padded_tensor functionality
        self.assertEqual(
            torch.nested.to_padded_tensor(nt_contiguous, 0.0),
            noncontiguous_to_padded_tensor(nt_noncontiguous))
        # test to_padded_tensor error message
        self.assertRaisesRegex(
            RuntimeError,
            r"for now to_padded_tensor only supports contiguous nested tensor",
            lambda: torch.nested.to_padded_tensor(nt_noncontiguous, 0.0)
        )

    @skipMeta
    def test_device_checks(self, device):
        nt = torch.nested.nested_tensor([], device=device)
        is_cuda = 'cuda' in str(device)
        self.assertEqual(nt.is_cuda, is_cuda)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_nested_tensor_indexing(self, device, dtype):
        # edge case: empty nested tensor
        nt0 = torch.nested.nested_tensor([])
        self.assertRaises(IndexError, lambda: nt0[0])
        # normal case
        x0 = torch.randn((2, 5), device=device, dtype=dtype)
        x1 = torch.randn((3, 4), device=device, dtype=dtype)
        nt = torch.nested.nested_tensor([x0, x1])
        # single index: only support integer in the batch dimension
        self.assertEqual(nt[0], x0)
        self.assertEqual(nt[-1], x1)
        self.assertRaises(IndexError, lambda: nt[2])
        self.assertRaises(IndexError, lambda: nt[-3])
        self.assertRaises(NotImplementedError, lambda: nt[:])
        self.assertRaises(NotImplementedError, lambda: nt[...])
        # tuple of indices: only support integer in the batch dimension
        #                 + all possible indexing in the original tensor dimensions
        self.assertEqual(nt[0, 0, 0], x0[0, 0])
        self.assertEqual(nt[0, 1, :], x0[1, :])
        self.assertEqual(nt[1, ...], x1)
        self.assertRaises(IndexError, lambda: nt[1, 4, 2])
        self.assertRaises(NotImplementedError, lambda: nt[:, 1, 1])
        # test select on non-batch dimensions
        self.assertEqual(nt.select(1, 0)[0], x0.select(0, 0))
        self.assertEqual(nt.select(1, 0)[1], x1.select(0, 0))
        self.assertRaises(IndexError, lambda: nt.select(1, 3))
        self.assertEqual(nt.select(2, 0)[0], x0.select(1, 0))
        self.assertEqual(nt.select(2, 0)[1], x1.select(1, 0))
        self.assertRaises(IndexError, lambda: nt.select(2, 5))
        # make sure indexing returns a view
        nt[0].fill_(100.0)
        answer = torch.tensor(100.0, device=device, dtype=dtype).expand((2, 5))
        self.assertEqual(nt[0], answer)
        nt[1, 1, :].fill_(200.0)
        answer = torch.tensor(200.0, device=device, dtype=dtype).expand(4)
        self.assertEqual(nt[1, 1, :], answer)

        # Test that indexing works when requires_grad_(True)
        # previously this was failing because the backward kernel for select.int uses .sizes()
        nt = torch.nested.nested_tensor([x0, x1]).requires_grad_(True)
        self.assertEqual(nt[0], x0)
        self.assertEqual(nt[-1], x1)
        grad_x0 = torch.randn((2, 5), device=device, dtype=dtype)
        nt[0].backward(grad_x0)
        expected_grad = torch.nested.nested_tensor([grad_x0, torch.zeros((3, 4), device=device, dtype=dtype)])
        self.assertEqual(nt.grad, expected_grad)

    @parametrize("func", [subtest(torch.nn.functional.relu, name='relu'),
                          subtest(torch.nn.functional.relu_, name='relu_'),
                          subtest(torch.nn.functional.gelu, name='gelu'),
                          subtest(torch._C._nn.gelu_, name='gelu_'),
                          subtest(torch.tanh, name='tanh'),
                          subtest(torch.tanh_, name='tanh_'),
                          subtest(torch.neg, name='neg'),
                          subtest(torch.nn.functional.silu, name='silu'),
                          subtest(partial(torch.nn.functional.silu, inplace=True), name='silu_'),
                          subtest(torch.abs, name="abs"),
                          subtest(torch.abs_, name="abs_"),
                          subtest(torch.sgn, name="sgn"),
                          subtest(torch.logical_not, name='logical_not'),
                          subtest(torch.sin, name='sin'),
                          subtest(torch.cos, name='cos')])
    def test_activations(self, device, func):
        nt, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7), device=device, dtype=torch.float32)
        nested_result = func(nt)
        self.assertTrue(nested_result.is_nested)
        for t, t_res in zip(nt.unbind(), nested_result.unbind()):
            self.assertEqual(func(t), t_res)
        self.assertRaisesRegex(
            RuntimeError,
            "NestedTensor must be contiguous to get buffer.",
            lambda: func(nt_noncontiguous))

    @parametrize("func", [subtest(torch.ge, name='ge'),
                          subtest(torch.eq, name='eq')])
    def test_binary_ops_with_scalar(self, device, func):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair(
            (2, 3, 6, 7), device=device, dtype=torch.float32)
        scalar = 0.0

        # should work regardless of contiguity
        for nt in (nt_contiguous, nt_noncontiguous):
            nested_result = func(nt, scalar)
            self.assertTrue(nested_result.is_nested)
            for t, t_res in zip(nt.unbind(), nested_result.unbind()):
                self.assertEqual(func(t, scalar), t_res)

    @dtypes(*floating_types_and_half())
    def test_nested_tensor_chunk(self, device, dtype):
        # Transformer use case
        a = torch.randn(3, 3 * 4, device=device, dtype=dtype)
        b = torch.randn(2, 3 * 4, device=device, dtype=dtype)
        c = torch.randn(1, 3 * 4, device=device, dtype=dtype)
        a_chunks = a.chunk(3, dim=-1)
        b_chunks = b.chunk(3, dim=-1)
        c_chunks = c.chunk(3, dim=-1)

        a_nt = [a_chunks[0], b_chunks[0], c_chunks[0]]
        b_nt = [a_chunks[1], b_chunks[1], c_chunks[1]]
        c_nt = [a_chunks[2], b_chunks[2], c_chunks[2]]

        nt = torch.nested.nested_tensor([a, b, c])
        chunked = nt.chunk(3, dim=-1)

        self.assertEqual(chunked[0], torch.nested.nested_tensor(a_nt))
        self.assertEqual(chunked[1], torch.nested.nested_tensor(b_nt))
        self.assertEqual(chunked[2], torch.nested.nested_tensor(c_nt))

        for chunk in chunked:
            self.assertFalse(chunk.is_contiguous())

        # Failure chunking on ragged dimensions
        self.assertRaisesRegex(
            RuntimeError, "Chunk for nested tensors is currently only supported for the last dimension.",
            lambda: torch.chunk(nt, 5, dim=1))
        self.assertRaisesRegex(
            RuntimeError, "Chunk for nested tensors is currently only supported for the last dimension.",
            lambda: torch.chunk(nt, 5, dim=0))

        # Failure on non-contiguous nt
        _, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3), device, dtype)
        self.assertRaisesRegex(
            RuntimeError, "chunk expects `self` to be contiguous.", lambda: torch.chunk(nt_noncontiguous, 5, dim=-1))

        # Failure when calling non divisible n_chunks
        self.assertRaisesRegex(
            RuntimeError, "Chunk for nested tensors is only supported for "
            "nested tensors with trailing dimension divisible by chunks.",
            lambda: torch.chunk(nt, 5, dim=-1))

        # Failure when calling backward on a chunk
        a = torch.randn(3, 3 * 4, device=device, dtype=dtype, requires_grad=True)
        b = torch.randn(2, 3 * 4, device=device, dtype=dtype, requires_grad=True)
        nt_grad = torch.nested.as_nested_tensor([a, b])
        chunked = torch.chunk(nt_grad, 2, dim=-1)
        self.assertRaisesRegex(RuntimeError, "derivative for aten::chunk is not implemented",
                               lambda: chunked[0].backward(chunked[0].clone()))

    @dtypes(*floating_types_and_half())
    def test_nested_tensor_split_with_sizes(self, device, dtype):
        a = torch.randn(3, 20, device=device, dtype=dtype)
        b = torch.randn(2, 20, device=device, dtype=dtype)
        c = torch.randn(1, 20, device=device, dtype=dtype)

        split_sizes = [4, 6, 10]
        a_splits = a.split_with_sizes(split_sizes, dim=-1)
        b_splits = b.split_with_sizes(split_sizes, dim=-1)
        c_splits = c.split_with_sizes(split_sizes, dim=-1)

        nt = torch.nested.nested_tensor([a, b, c])
        nt_splits = nt.split_with_sizes(split_sizes, dim=-1)

        for i, nt_split in enumerate(nt_splits):
            self.assertEqual(nt_split, torch.nested.nested_tensor(
                [a_splits[i], b_splits[i], c_splits[i]]))
            dense_strides = torch.stack([
                torch.tensor(a_splits[i].stride()),
                torch.tensor(b_splits[i].stride()),
                torch.tensor(c_splits[i].stride())
            ])
            self.assertEqual(nt_split._nested_tensor_strides(), dense_strides)
            self.assertFalse(nt_split.is_contiguous())

        # Failure calling on ragged dimensions
        self.assertRaisesRegex(
            RuntimeError, "split_with_sizes for nested tensors is currently only supported for the last dimension.",
            lambda: torch.split_with_sizes(nt, split_sizes, dim=1))

        # Failure calling on non-last dimension
        self.assertRaisesRegex(
            RuntimeError, "split_with_sizes for nested tensors is currently only supported for the last dimension.",
            lambda: torch.split_with_sizes(nt, split_sizes, dim=0))

        # Failure on non-contiguous nt
        _, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3), device, dtype)
        self.assertRaisesRegex(
            RuntimeError, "split_with_sizes expects `self` to be contiguous.",
            lambda: torch.split_with_sizes(nt_noncontiguous, split_sizes, dim=-1))

        # Failure when calling with split_sizes that don't cover the full dim size
        bad_split_sizes = [4, 6, 9]  # don't add up to 20
        self.assertRaisesRegex(
            RuntimeError, "split_with_sizes expects split_sizes to sum exactly to 20",
            lambda: torch.split_with_sizes(nt, bad_split_sizes, dim=-1))

    @dtypes(torch.float, torch.float16, torch.double)
    @torch.inference_mode()
    def test_nested_tensor_indexing_noncontiguous(self, device, dtype):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7), device, dtype)
        self.assertEqual(nt_contiguous.size(0), nt_noncontiguous.size(0))
        n = nt_contiguous.size(0)
        for i in range(n):
            self.assertEqual(nt_contiguous[i], nt_noncontiguous[i])

    @dtypes(torch.float, torch.float16)
    @skipMeta
    @torch.inference_mode()
    @parametrize("transpose", [True, False])
    def test_nested_tensor_add(self, device, dtype, transpose):
        if transpose:
            a = torch.randn(2, 2, 2, device=device, dtype=dtype)
            b = torch.rand(2, 2, 2, device=device, dtype=dtype)
            c = a.transpose(-1, -2).contiguous()
            d = b.transpose(-1, -2).contiguous()
            nt1 = torch.nested.nested_tensor([a, b, a, b])
            nt2 = torch.nested.nested_tensor([c, d, c, d]).transpose(-1, -2)
        else:
            (nt1, nt2) = self.random_nt_pair(device, dtype, 4, (4, 4))
        ref = torch.nested.nested_tensor([t1 + t2 for (t1, t2) in zip(nt1.unbind(), nt2.unbind())])
        out = nt1 + nt2
        self.assertEqual(ref, out)

    @dtypes(torch.float, torch.float16)
    @skipMeta
    @torch.inference_mode()
    @parametrize("transpose", [True, False])
    def test_nested_tensor_sub(self, device, dtype, transpose):
        if transpose:
            a = torch.randn(2, 2, 2, device=device, dtype=dtype)
            b = torch.rand(2, 2, 2, device=device, dtype=dtype)
            c = a.transpose(-1, -2).contiguous()
            d = b.transpose(-1, -2).contiguous()
            nt1 = torch.nested.nested_tensor([a, b, a, b])
            nt2 = torch.nested.nested_tensor([c, d, c, d]).transpose(-1, -2)
        else:
            (nt1, nt2) = self.random_nt_pair(device, dtype, 4, (4, 4))
        ref = torch.nested.nested_tensor([t1 - t2 for (t1, t2) in zip(nt1.unbind(), nt2.unbind())])
        out = nt1 - nt2
        self.assertEqual(ref, out)

    @onlyCUDA
    @dtypes(torch.float, torch.float16)
    @torch.inference_mode()
    @parametrize("embedding_dim", [8, 128, 256, 384])
    def test_nested_tensor_dense_elementwise(self, device, dtype, embedding_dim):
        def _test_add_mul(nt, t):
            ref_add = torch.nested.nested_tensor(
                [t1 + t2 for (t1, t2) in zip(nt.unbind(), t.unbind())])
            ref_mul = torch.nested.nested_tensor(
                [t1 * t2 for (t1, t2) in zip(nt.unbind(), t.unbind())])
            self.assertEqual(nt.add(t), ref_add)
            self.assertEqual(nt.mul(t), ref_mul)

        batch_size = 32
        seq_lens = torch.randint(low=0, high=10, size=(batch_size,))

        # [B, *, D], [B, 1, D] case
        ts = [torch.randn((seq_len, embedding_dim)) for seq_len in seq_lens]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        t = torch.randn((batch_size, 1, embedding_dim), device=device, dtype=dtype)
        _test_add_mul(nt, t)

        # [B, *], [B, 1] case
        ts = [torch.randn(seq_len) for seq_len in seq_lens]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype)
        t = torch.randn((batch_size, 1), device=device, dtype=dtype)
        _test_add_mul(nt, t)

    @dtypes(torch.float, torch.float16)
    @skipMeta
    @torch.inference_mode()
    def test_nested_tensor_mul(self, device, dtype):
        # nested tensor * nested tensor
        (nt1, nt2) = self.random_nt_pair(device, dtype, 4, (4, 4))
        ref = torch.nested.nested_tensor([t1 * t2 for (t1, t2) in zip(nt1.unbind(), nt2.unbind())])
        out = nt1 * nt2
        self.assertEqual(ref, out)
        # nested tensor * scalar
        number = 10.0
        scalar = torch.tensor(number).to(dtype).to(device)
        ref = torch.nested.nested_tensor([t * number for t in nt1.unbind()])
        out_number0 = nt1 * number
        out_number1 = number * nt1
        out_scalar0 = nt1 * scalar
        out_scalar1 = scalar * nt1
        self.assertEqual(out_number0, ref)
        self.assertEqual(out_number1, ref)
        self.assertEqual(out_scalar0, ref)
        self.assertEqual(out_scalar1, ref)
        # error case: numel == 1 but dim > 0
        vector = torch.tensor([number]).to(dtype).to(device)
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both self and other to be nested, but got a nested self and non-nested other",
            lambda: nt1.mul(vector)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both self and other to be nested, but got a non-nested self and nested other",
            lambda: vector.mul(nt1)
        )

    @dtypes(torch.float, torch.float16)
    @skipMeta
    @torch.inference_mode()
    def test_nested_tensor_div(self, device, dtype):
        nt, nt2 = self.random_nt_pair(device, dtype, 4, (4, 4))
        scale = 4.0
        ref = torch.nested.nested_tensor([t / scale for t in nt.unbind()])
        out = nt / 4.0
        self.assertEqual(ref, out)
        ref_transposed = ref.transpose(1, 2)
        out = nt.transpose(1, 2) / 4.0
        self.assertEqual(ref_transposed, out)

        ref = torch.nested.nested_tensor([t / t2 for (t, t2) in zip(nt.unbind(), nt2.unbind())])
        out = nt / nt2
        self.assertEqual(ref, out)

        out = nt.transpose(1, 2) / nt2.transpose(1, 2)
        self.assertEqual(ref.transpose(1, 2), out)

        nt_transpose_copy = torch.nested.nested_tensor([t.transpose(0, 1) for t in nt.unbind()])

        self.assertRaisesRegex(
            RuntimeError, "div requires strides to match when given NestedTensors",
            lambda: nt_transpose_copy.transpose(1, 2) / nt2)

        nt = torch.nested.nested_tensor([torch.randn(i, 4) for i in [3, 4, 5]], device=device, dtype=dtype)
        nt_chunks = nt.chunk(2, -1)
        self.assertRaisesRegex(
            RuntimeError, "div requires offsets to match when given NestedTensors",
            lambda: nt_chunks[0] / nt_chunks[1])

    @dtypes(torch.float, torch.float16)
    @skipMeta
    @torch.inference_mode()
    def test_nested_tensor_add_in_place(self, device, dtype):
        (nt1, nt2) = self.random_nt_pair(device, dtype, 4, (4, 4))
        ref = torch.nested.nested_tensor([t1 + t2 for (t1, t2) in zip(nt1.unbind(), nt2.unbind())])
        nt1 += nt2
        self.assertEqual(ref, nt1)

    @dtypes(torch.float, torch.float16)
    @skipMeta
    @torch.inference_mode()
    def test_nested_tensor_mul_in_place(self, device, dtype):
        # nested tensor * nested tensor
        (nt1, nt2) = self.random_nt_pair(device, dtype, 4, (4, 4))
        ref = torch.nested.nested_tensor([t1 * t2 for (t1, t2) in zip(nt1.unbind(), nt2.unbind())])
        nt1 *= nt2
        self.assertEqual(ref, nt1)
        # nested tensor * scalar
        number = 10.0
        scalar = torch.tensor(number).to(dtype).to(device)
        ref = torch.nested.nested_tensor([t * number for t in nt1.unbind()])
        out_number = nt1.clone()
        out_number *= number
        out_scalar = nt1.clone()
        out_scalar *= scalar
        self.assertEqual(out_number, ref)
        self.assertEqual(out_scalar, ref)
        self.assertRaisesRegex(
            RuntimeError,
            r"output with shape \[.*\] doesn't match the broadcast shape \[.*\]",
            lambda: scalar.mul_(nt1)
        )
        # error case: numel == 1 but dim > 0
        vector = torch.tensor([number]).to(dtype).to(device)
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both self and other to be nested, but got a nested self and non-nested other",
            lambda: nt1.mul_(vector)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both self and other to be nested, but got a non-nested self and nested other",
            lambda: vector.mul_(nt1)
        )

    @onlyCPU
    @skipMeta
    @dtypes(torch.float)
    def test_nested_tensor_sum_dim(self, device, dtype):
        params = ((2, (1, 1)), ((4), (4, 4)), (10, (3, 5, 7)))

        def test_sum(device, dtype, ntensors, max_sizes, dim, keepdim=True):
            nt = random_nt(device, dtype, ntensors, max_sizes, require_non_empty=False)
            nt2 = nt.clone()
            ub2 = nt2.unbind()
            nt.requires_grad_(True)
            [t.requires_grad_(True) for t in ub2]
            nt_sum = nt.sum(dim=dim, keepdim=keepdim)
            ub2_sum = [t.sum(-1, keepdim=keepdim) for t in ub2]
            self.assertEqual(nt_sum, torch.nested.nested_tensor(ub2_sum))

            # test backward
            # generate gradient tensor that has the same size as the output
            size = nt_sum._nested_tensor_size()
            gt2 = []
            for i in range(ntensors):
                gt2.append(torch.randn(size[i].tolist(), device=device, dtype=dtype))
            gt = torch.nested.nested_tensor(gt2).clone()
            nt_sum.backward(gt)
            for t2, g2 in zip(ub2_sum, gt2):
                t2.backward(g2)
            self.assertEqual(nt.grad, torch.nested.nested_tensor([t.grad for t in ub2]))
            return

        for ntensors, max_sizes in params:
            test_sum(device, dtype, ntensors, max_sizes, len(max_sizes))

        # Test error inputs
        with self.assertRaisesRegex(RuntimeError, "NestedTensor can only be reduced across the last"):
            torch.nested.nested_tensor([torch.tensor([3, 4, 5]), torch.tensor([1, 2])]).sum(0, keepdim=True)

        with self.assertRaisesRegex(RuntimeError, "NestedTensor only allows reduction of a single"):
            torch.nested.nested_tensor([torch.tensor([[3, 4, 5]]), torch.tensor([[1, 2]])]).sum([0, 1], keepdim=True)

        with self.assertRaisesRegex(RuntimeError, "NestedTensor always requires keepdim=True for now."):
            torch.nested.nested_tensor([torch.tensor([3, 4, 5]), torch.tensor([1, 2])]).sum(-1)

    @dtypes(torch.float, torch.float16)
    def test_contiguous(self, device, dtype):
        # Since we don't have access to the buffer in python this is harder to show what
        # we are testing for. When we call chunk on a consistent dim of a NT
        # for chunk_size > 1 the resulting tensors are views of the original NT
        # whose numels is now less than the size of the buffer. Clone was
        # previously creating a new NT with a buffer that was the same size as the
        # original.
        nt_contiguous = torch.nested.nested_tensor([torch.randn(2, 20, device=device, dtype=dtype),
                                                    torch.randn(4, 20, device=device, dtype=dtype)])
        # Split up the last dimension which has a consistent size of 20 into 5 chunks
        chunks = nt_contiguous.chunk(5, dim=-1)

        # # Check chunks are contiguous after calling contiguous
        for chunk in chunks:
            self.assertFalse(chunk.is_contiguous())
            self.assertTrue(chunk.contiguous().is_contiguous())

    @dtypes(torch.float, torch.float16)
    @skipMeta
    def test_clone(self, device, dtype):
        nt1 = random_nt(device, dtype, 4, (4, 4), (1, 1))
        nt2 = nt1.clone()
        # Verify the values match
        self.assertEqual(nt1, nt2)
        # Verify modifying nt2 doesn't affect nt1
        nt2.mul_(nt1)
        ub1 = nt1.unbind()
        ub2 = nt2.unbind()
        for i in range(len(ub1)):
            self.assertNotEqual(ub1[i], ub2[i])

        nt1.clone(memory_format=torch.preserve_format)
        msg = "Nested tensor clone supports Preserve and Contiguous memory formats, called clone with memory format: ChannelsLast"
        with self.assertRaisesRegex(RuntimeError, msg):
            nt1.clone(memory_format=torch.channels_last)

    # cannot test torch.float16 because: RuntimeError: "bernoulli_scalar_cpu_" not implemented for 'Half'
    @decorateIf(xfailIfTorchDynamo, lambda params: params["layout"] == torch.jagged)
    @dtypes(torch.float, torch.double)
    @parametrize("layout", [torch.strided, torch.jagged], name_fn=layout_name)
    def test_dropout(self, device, dtype, layout):
        # edge case: empty nested tensor
        # TODO: support empty NT in jagged layout
        if layout == torch.strided:
            nt0 = torch.nested.nested_tensor([], layout=layout)
            y = torch.nn.functional.dropout(nt0, 0.5)
            self.assertEqual(nt0, y)
        # normal nested tensor
        ntensors = 4
        if layout == torch.jagged:
            nt = random_nt(device, dtype, ntensors, (4, 4), (0, 3), layout=layout)
        else:
            nt = random_nt(device, dtype, ntensors, (4, 4), layout=layout)
        # edge case: invalid dropout
        self.assertRaises(ValueError, lambda: torch.nn.Dropout(-0.1))
        self.assertRaises(ValueError, lambda: torch.nn.Dropout(1.1))
        self.assertRaises(ValueError, lambda: torch.nn.functional.dropout(nt, -0.1))
        self.assertRaises(ValueError, lambda: torch.nn.functional.dropout(nt, 1.1))
        # edge case: no dropout
        dropouter = torch.nn.Dropout(0.0)
        y0 = dropouter(nt)
        y1 = torch.nn.functional.dropout(nt, 0.0)
        self.assertEqual(nt, y0)
        self.assertEqual(nt, y1)
        # edge case: all dropout
        dropouter = torch.nn.Dropout(1.0)
        y0 = dropouter(nt)
        y1 = torch.nn.functional.dropout(nt, 1.0)
        nt0 = torch.zeros_like(nt)
        self.assertEqual(nt0, y0)
        self.assertEqual(nt0, y1)
        # normal case: normal dropout
        p = 0.2
        y = torch.nn.functional.dropout(nt, p)
        expect = nt.clone()
        if layout == torch.jagged:
            expect = torch.where(y == 0.0, y, nt)
            expect /= 1.0 - p
            self.assertEqual(y, expect)
        else:
            expect = nt.clone()
            for i in range(ntensors):
                actual_tensor = y[i].view(-1)
                expect_tensor = expect[i].view(-1)
                for j in range(actual_tensor.shape[0]):
                    if actual_tensor[j].item() == 0.0:
                        expect_tensor[j] = 0.0
                    else:
                        expect_tensor[j] /= 1.0 - p
            self.assertEqual(y, expect)
        with freeze_rng_state():
            dropouter = torch.nn.Dropout(p)
            y0 = dropouter(nt)
        with freeze_rng_state():
            y1 = torch.nn.functional.dropout(nt, p)
        self.assertEqual(y0, y1)

    @dtypes(torch.float, torch.double)
    def test_dropout_noncontiguous(self, device, dtype):
        ntensors = 4
        nt0 = random_nt(device, dtype, ntensors, (4, 4))
        nt1 = nt0.transpose(-1, -2)
        p = 0.3
        with freeze_rng_state():
            dropouter = torch.nn.Dropout(p)
            y0 = dropouter(nt0)
        with freeze_rng_state():
            y1 = torch.nn.functional.dropout(nt1, p).transpose(-1, -2)
        self.assertEqual(y0, y1)

    # cannot test torch.float16 because: RuntimeError: "softmax_kernel_impl" not implemented for 'Half'
    @dtypes(torch.float, torch.double)
    def test_softmax(self, device, dtype):
        # normal nested tensor
        ntensors = 4
        nt = random_nt(device, dtype, ntensors, (4, 4))
        # error case: softmax across nested dimension
        self.assertRaisesRegex(
            RuntimeError,
            "Cannot apply softmax across nested dimension 0",
            lambda: torch.nn.functional.softmax(nt, 0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Cannot apply softmax across nested dimension 0",
            lambda: torch.nn.functional.softmax(nt, -3)
        )
        # error case: dimension out of range
        self.assertRaises(IndexError, lambda: torch.nn.functional.softmax(nt, 3))
        self.assertRaises(IndexError, lambda: torch.nn.functional.softmax(nt, -4))
        # normal case: should equal to padding -inf
        softmaxer = torch.nn.Softmax(1)
        y0 = softmaxer(nt)
        y1 = torch.nn.functional.softmax(nt, 1)
        self.assertEqual(y0, y1)
        pt = torch.nested.to_padded_tensor(nt, float("-inf"))
        # if an entire slice is padded, then softmax will return 0.0 / 0.0 = nan
        # however, physically speaking that should be 0.0
        expect = torch.nn.functional.softmax(pt, 1).nan_to_num_(0.0)
        self.assertEqual(torch.nested.to_padded_tensor(y0, 0.0), expect)
        # edge case: empty nested tensor
        nt0 = torch.nested.nested_tensor([])
        y = torch.nn.functional.softmax(nt0, 1)
        self.assertEqual(nt0, y)
        # edge case: nesting scalars
        nt1 = torch.nested.nested_tensor([torch.tensor(0.0), torch.tensor(1.0)])
        self.assertRaises(RuntimeError, lambda: torch.nn.functional.softmax(nt1, 0))
        self.assertRaises(IndexError, lambda: torch.nn.functional.softmax(nt1, 1))

    @dtypes(torch.float, torch.double)
    @torch.inference_mode()
    def test_softmax_noncontiguous(self, device, dtype):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7), device, dtype)
        self.assertEqual(
            torch.nn.functional.softmax(nt_contiguous, -1),
            torch.nn.functional.softmax(nt_noncontiguous, -1))

    def _test_bmm(self, device, dtype):
        # error case: one is nested but the other is not
        nt = torch.nested.nested_tensor([torch.randn(2), torch.randn(3)], device=device, dtype=dtype)
        t = torch.randn(4, device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both to be nested, but got a nested self and non-nested other",
            lambda: nt.bmm(t)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both to be nested, but got a non-nested self and nested other",
            lambda: t.bmm(nt)
        )
        # error case: not 3D tensors
        nt0 = torch.nested.nested_tensor([], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn(2), torch.randn(3)], device=device, dtype=dtype)
        nt2 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 4))], device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "batch1 must be a 3D tensor",
            lambda: nt0.bmm(nt0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch1 must be a 3D tensor",
            lambda: nt0.bmm(nt1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch1 must be a 3D tensor",
            lambda: nt0.bmm(nt2)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch1 must be a 3D tensor",
            lambda: nt1.bmm(nt0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch1 must be a 3D tensor",
            lambda: nt1.bmm(nt1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch1 must be a 3D tensor",
            lambda: nt1.bmm(nt2)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch2 must be a 3D tensor",
            lambda: nt2.bmm(nt0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "batch2 must be a 3D tensor",
            lambda: nt2.bmm(nt1)
        )
        # error case: incompatible batch size
        nt0 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 4))], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((4, 6)),
                                          torch.randn((4, 5)),
                                          torch.randn((4, 7))],
                                         device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "Expected size for the 1st dimension of batch2 tensor to be: 2 but got: 3.",
            lambda: nt0.bmm(nt1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Expected size for the 1st dimension of batch2 tensor to be: 3 but got: 2.",
            lambda: nt1.bmm(nt0)
        )
        # error case: underlying matrices cannot be multiplied
        nt0 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 4))], device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            r"0-th nested matrices in batch cannot be multiplied \(2x4 and 2x4\)",
            lambda: nt0.bmm(nt0)
        )
        # normal nested tensor
        nt0 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 7))], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((4, 6)), torch.randn((7, 5))], device=device, dtype=dtype)
        actual = torch.nested.to_padded_tensor(nt0.bmm(nt1), 0.0)
        expect = torch.nested.to_padded_tensor(nt0, 0.0).bmm(torch.nested.to_padded_tensor(nt1, 0.0))
        if dtype == torch.float16:
            self.assertEqual(actual, expect, rtol=1e-3, atol=1e-3)
        else:
            self.assertEqual(actual, expect)

        # test tensorcore path
        nt0 = torch.nested.nested_tensor([torch.randn((2, 8)), torch.randn((3, 16))], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((8, 8)), torch.randn((16, 8))], device=device, dtype=dtype)
        actual = torch.nested.to_padded_tensor(nt0.bmm(nt1), 0.0)
        expect = torch.nested.to_padded_tensor(nt0, 0.0).bmm(torch.nested.to_padded_tensor(nt1, 0.0))
        if dtype == torch.float16:
            self.assertEqual(actual, expect, rtol=1e-3, atol=1e-3)
        else:
            self.assertEqual(actual, expect)

    @onlyCUDA
    @dtypes(torch.float, torch.double, torch.float16)
    def test_bmm_cuda(self, device, dtype):
        self._test_bmm(device, dtype)

    @onlyCPU
    # cannot test torch.float16 because: RuntimeError: "addmm_impl_cpu_" not implemented for 'Half'
    @dtypes(torch.float, torch.double)
    def test_bmm_cpu(self, device, dtype):
        self._test_bmm(device, dtype)

    # cannot test torch.float16 because: RuntimeError: "addmm_impl_cpu_" not implemented for 'Half'
    @dtypes(torch.float, torch.double)
    def test_bmm_noncontiguous(self, device, dtype):
        nt0_contiguous, nt0_noncontiguous = random_nt_noncontiguous_pair((2, 3), device, dtype)
        nt1_contiguous, nt1_noncontiguous = random_nt_noncontiguous_pair((6, 7), device, dtype)
        self.assertEqual(
            nt0_contiguous.transpose(-1, -2).bmm(nt1_contiguous),
            nt0_noncontiguous.transpose(-1, -2).bmm(nt1_noncontiguous))

    @dtypes(torch.float, torch.double)
    def test_matmul_with_bmm_path(self, device, dtype):
        def unbind_rebind_matmul(nt1, nt2):
            t1s = nt1.unbind()
            t2s = nt2.unbind()
            out_ts = [t1.matmul(t2) for t1, t2 in zip(t1s, t2s)]
            return torch.nested.nested_tensor(out_ts)

        # [N, n_head, *, head_dim], [N, n_head, head_dim, *]
        Ns = [1, 2, 5]
        n_heads = np.random.randint(2, 5)
        head_dim = 3
        t1s = []
        t2s = []
        for N in Ns:
            for _ in range(N):
                seq_len1 = np.random.randint(2, 5)
                seq_len2 = np.random.randint(2, 5)
                t1s.append(torch.randn(n_heads, seq_len1, head_dim))
                t2s.append(torch.randn(n_heads, head_dim, seq_len2))
            nt1 = torch.nested.nested_tensor(t1s, device=device, dtype=dtype)
            nt2 = torch.nested.nested_tensor(t2s, device=device, dtype=dtype)
            self.assertEqual(torch.matmul(nt1, nt2), unbind_rebind_matmul(nt1, nt2))

        # test with noncontiguous
        t3s = []
        t4s = []
        for _ in range(N):
            seq_len = np.random.randint(2, 5)
            t3s.append(torch.randn(seq_len, n_heads, head_dim))
            t4s.append(torch.randn(seq_len, n_heads, head_dim))
        nt3 = torch.nested.nested_tensor(t3s, device=device, dtype=dtype).transpose(1, 2)
        nt4 = torch.nested.nested_tensor(t4s, device=device, dtype=dtype).transpose(1, 2).transpose(2, 3)
        self.assertEqual(torch.matmul(nt3, nt4), unbind_rebind_matmul(nt3, nt4))

    # cannot test torch.float16 because: RuntimeError: "bmm" not implemented for 'Half'
    @dtypes(torch.float, torch.double)
    def test_matmul(self, device, dtype):
        # error case: one is nested but the other is not
        nt = torch.nested.nested_tensor([torch.randn(2), torch.randn(3)], device=device, dtype=dtype)
        t = torch.randn(4, device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both to be nested, but got a nested self and non-nested other",
            lambda: torch.matmul(nt, t)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Expected both to be nested, but got a non-nested self and nested other",
            lambda: torch.matmul(t, nt)
        )
        # error case: not 3+D tensors
        nt0 = torch.nested.nested_tensor([], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn(2), torch.randn(3)], device=device, dtype=dtype)
        nt2 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 4))], device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 1st input has rank: [0-9]+",
            lambda: torch.matmul(nt0, nt0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 1st input has rank: [0-9]+",
            lambda: torch.matmul(nt0, nt1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 1st input has rank: [0-9]+",
            lambda: torch.matmul(nt0, nt2)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 1st input has rank: [0-9]+",
            lambda: torch.matmul(nt1, nt0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 1st input has rank: [0-9]+",
            lambda: torch.matmul(nt1, nt1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 1st input has rank: [0-9]+",
            lambda: torch.matmul(nt1, nt2)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 2nd input has rank: [0-9]+",
            lambda: torch.matmul(nt2, nt0)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: For nested tensors, only inputs with >= 3 dims are currently supported. 2nd input has rank: [0-9]+",
            lambda: torch.matmul(nt2, nt1)
        )
        # error case: incompatible batch size
        nt0 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 4))], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((4, 6)),
                                          torch.randn((4, 5)),
                                          torch.randn((4, 7))],
                                         device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: Expected size for the 1st dimension of 2nd input tensor to be: [0-9]+ but got: [0-9]+.",
            lambda: torch.matmul(nt0, nt1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"matmul: Expected size for the 1st dimension of 2nd input tensor to be: [0-9]+ but got: [0-9]+.",
            lambda: torch.matmul(nt1, nt0)
        )
        # error case: incompatible (wrong) batch sizes that shouldn't even broadcast?
        nt0 = torch.nested.nested_tensor([torch.randn((2, 2, 4)),
                                          torch.randn((2, 3, 4))],
                                         device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((3, 4, 6)),
                                          torch.randn((3, 4, 5))],
                                         device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "matmul(): For nested tensors, batch dimensions must have the same sizes,",
            lambda: torch.matmul(nt0, nt1)
        )
        # error case: incompatible batch sizes that should technically broadcast
        nt0 = torch.nested.nested_tensor([torch.randn((2, 2, 4)),
                                          torch.randn((1, 3, 4))],
                                         device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((1, 4, 6)),
                                          torch.randn((3, 4, 5))],
                                         device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "matmul(): For nested tensors, batch dimensions must have the same sizes,",
            lambda: torch.matmul(nt0, nt1)
        )
        # error case: underlying matrices cannot be multiplied
        nt0 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 4))], device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "matmul(): Nested tensors cannot be matrix multiplied",
            lambda: torch.matmul(nt0, nt0)
        )
        # normal nested tensor: 3D
        nt0 = torch.nested.nested_tensor([torch.randn((2, 4)), torch.randn((3, 7))], device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((4, 6)), torch.randn((7, 5))], device=device, dtype=dtype)
        actual = torch.nested.to_padded_tensor(torch.matmul(nt0, nt1), 0.0)
        expect = torch.matmul(torch.nested.to_padded_tensor(nt0, 0.0), torch.nested.to_padded_tensor(nt1, 0.0))
        self.assertEqual(actual, expect)
        # normal nested tensor: 4D (with testing for batch_size=1)
        nt0 = torch.nested.nested_tensor([torch.randn((1, 2, 4)),
                                          torch.randn((8, 3, 7))],
                                         device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((1, 4, 6)),
                                          torch.randn((8, 7, 5))],
                                         device=device, dtype=dtype)
        actual = torch.nested.to_padded_tensor(torch.matmul(nt0, nt1), 0.0)
        expect = torch.matmul(torch.nested.to_padded_tensor(nt0, 0.0), torch.nested.to_padded_tensor(nt1, 0.0))
        self.assertEqual(actual, expect)
        # normal nested tensor: 5D
        nt0 = torch.nested.nested_tensor([torch.randn((8, 9, 2, 4)),
                                          torch.randn((8, 9, 3, 7))],
                                         device=device, dtype=dtype)
        nt1 = torch.nested.nested_tensor([torch.randn((8, 9, 4, 6)),
                                          torch.randn((8, 9, 7, 5))],
                                         device=device, dtype=dtype)
        actual = torch.nested.to_padded_tensor(torch.matmul(nt0, nt1), 0.0)
        expect = torch.matmul(torch.nested.to_padded_tensor(nt0, 0.0), torch.nested.to_padded_tensor(nt1, 0.0))
        self.assertEqual(actual, expect)

    # only supported on CUDA for now
    @dtypes(torch.float, torch.double)
    def test_matmul_nt_with_broadcasted_t(self, device, dtype):
        # NT (B, *, C, D) with T (D, E) broadcasting case
        nt = random_nt_from_dims([3, None, 4, 5], device=device, dtype=dtype)
        t = torch.randn(5, 6, device=device, dtype=dtype)
        output = torch.matmul(nt, t)

        # should be equivalent to matmul-ing each component with the dense tensor
        self.assertEqual(nt.size(0), output.size(0))
        for component, out_component in zip(nt, output):
            self.assertEqual(out_component, torch.matmul(component, t))

    # cannot test torch.float16 because: RuntimeError: "bmm" not implemented for 'Half'
    @dtypes(torch.float, torch.double)
    def test_matmul_noncontiguous(self, device, dtype):
        nt0_contiguous, nt0_noncontiguous = random_nt_noncontiguous_pair((2, 3), device, dtype)
        nt1_contiguous, nt1_noncontiguous = random_nt_noncontiguous_pair((6, 7), device, dtype)
        self.assertEqual(
            torch.matmul(nt0_contiguous.transpose(-1, -2), nt1_contiguous),
            torch.matmul(nt0_noncontiguous.transpose(-1, -2), nt1_noncontiguous))

    @dtypes(torch.float, torch.double)
    def test_linear(self, device, dtype):
        a = torch.randn(1, 2, device=device, dtype=dtype)
        b = torch.randn(2, 2, device=device, dtype=dtype)
        c = torch.randn(3, 2, device=device, dtype=dtype)
        nt = torch.nested.nested_tensor([a, b, c])

        weight = torch.randn(2, 2, device=device, dtype=dtype)
        bias = torch.randn(2, device=device, dtype=dtype)
        # success case
        torch.functional.F.linear(nt, weight, bias)

        # invalid nested tensor dimension
        msg = r'Linear requires nested_tensor.dim == 3 and dense_matrix.dim == 2. Nested tensor dim: 2. Dense tensor dim: 2'
        nt1 = torch.nested.nested_tensor([torch.randn(1, device=device, dtype=dtype),
                                          torch.randn(2, device=device, dtype=dtype)])
        with self.assertRaisesRegex(RuntimeError, msg):
            torch.functional.F.linear(nt1, weight, bias)

        # invalid weight shape
        msg = r'Linear requires nested_tensor.dim == 3 and dense_matrix.dim == 2. Nested tensor dim: 3. Dense tensor dim: 3'
        weight1 = torch.randn(2, 2, 3, device=device, dtype=dtype)
        with self.assertRaisesRegex(RuntimeError, msg):
            torch.functional.F.linear(nt, weight1, bias)

        # inconsistent last dim of nested tensor
        msg = r"Expected all tensors in nested tensor to have the same trailing dimension, instead last dimension equals:"
        nt2 = torch.nested.nested_tensor([torch.randn(1, 2, device=device, dtype=dtype),
                                          torch.randn(2, 3, device=device, dtype=dtype)])
        with self.assertRaisesRegex(RuntimeError, msg):
            torch.functional.F.linear(nt2, weight, bias)

        # Mismatch of nested tensor last dim and weight dimension
        weight2 = torch.randn(2, 4, device=device, dtype=dtype)
        msg = r"Shape mismatch for NestedTensor Linear: Expected input's \(a nested tensor\) 'last_dim'" \
            r" to equal 'weight.size\(1\), but got: last_dim = 2, and weight.size\(1\) = 4"
        with self.assertRaisesRegex(RuntimeError, msg):
            torch.functional.F.linear(nt, weight2, bias)

        # Nested tensor input and nested weight
        nt_weight = nt.clone()
        msg = r"Linear does not support nested weight when input is a nested tensor."
        with self.assertRaisesRegex(RuntimeError, msg):
            torch.functional.F.linear(nt, nt_weight, bias)

    # TODO: test noncontiguous linear
    # For now this tests the error message of linear
    # since linear does not support noncontiguous buffer yet
    @dtypes(torch.float, torch.double)
    def test_linear_noncontiguous(self, device, dtype):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7), device, dtype)
        weight = torch.randn((8, 5), device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            r"for now linear only supports contiguous nested tensor",
            lambda: torch.nn.functional.linear(nt_noncontiguous, weight)
        )

    @dtypes(torch.float, torch.float16, torch.double)
    def test_to_padded_tensor_zero_numel_errors(self, device, dtype):
        ts = [torch.ones(1, 0), torch.ones(0, 0)]
        nt = torch.nested.nested_tensor(ts, device=device, dtype=dtype, layout=torch.strided)
        self.assertRaisesRegex(
            RuntimeError,
            r"at least one constituent tensor should have non-zero numel",
            lambda: torch.nested.to_padded_tensor(nt, 0.0)
        )

    @dtypes(torch.float, torch.float16, torch.double)
    def test_transpose(self, device, dtype):
        nt = random_nt(device, dtype, 4, (4, 4))
        # error case: transpose nested dimension
        self.assertRaisesRegex(
            RuntimeError,
            "Nested tensor dimension 0 cannot be transposed",
            lambda: nt.transpose(0, 1)
        )
        self.assertRaisesRegex(
            RuntimeError,
            "Nested tensor dimension 0 cannot be transposed",
            lambda: nt.transpose(1, -3)
        )
        # error case: dimension out of range
        self.assertRaises(IndexError, lambda: nt.transpose(1, 3))
        self.assertRaises(IndexError, lambda: nt.transpose(-4, -1))
        # normal case
        ntT = nt.transpose(-1, -2)
        ptT_from_ntT = noncontiguous_to_padded_tensor(ntT)
        pt = torch.nested.to_padded_tensor(nt, 0.0)
        ptT = pt.transpose(-1, -2)
        self.assertEqual(ptT, ptT_from_ntT)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_squeeze_unsqueeze(self, device, dtype):
        a = torch.arange(6).reshape(2, 3)
        b = torch.arange(15).reshape(5, 3)
        nt = torch.nested.nested_tensor([a, b], device=device, dtype=dtype)
        # error case: squeeze no dimension
        self.assertRaisesRegex(
            RuntimeError,
            "For nested tensors, squeeze without the dim argument",
            lambda: nt.squeeze()
        )
        # error case: squeeze nested dimension
        self.assertRaisesRegex(
            RuntimeError,
            "For nested tensors, squeezing dimension 0",
            lambda: nt.squeeze(0)
        )
        # error case: dimension out of range
        self.assertRaises(IndexError, lambda: nt.squeeze(3))
        # error case: squeeze nested tensor of singleton tensors
        c = torch.ones(1)
        nt_singleton = torch.nested.nested_tensor([c, c], device=device, dtype=dtype)
        self.assertRaisesRegex(
            RuntimeError,
            "For nested tensors, squeezing a nested tensor of singleton",
            lambda: nt_singleton.squeeze(1)
        )

        # squeezing a dim which does not have size 1 should be a no-op
        nt2 = nt.squeeze(-1)
        self.assertEqual(nt, nt2)

        # test cases that should work
        nt_sizes = nt._nested_tensor_size()
        nt_strides = nt._nested_tensor_strides()
        for i in range(-2, 4):
            if (i == 0):
                # cannot unsqueeze batch dim
                continue
            nt_unsqueezed = nt.unsqueeze(i)
            # negative dim will correspond to unsqueeze() applied at dim = dim + nt.dim() + 1
            wrapped_i = i + nt.dim() + 1 if i < 0 else i
            # col_index into nt size tensor is requires subtraction of 1 to ignore batch dim
            size_idx = wrapped_i - 1
            self.assertEqual(nt_unsqueezed._nested_tensor_size()[:, size_idx], torch.ones(2, dtype=torch.long))
            unsqueezed_stride = nt_unsqueezed._nested_tensor_strides()[:, size_idx]
            if (i == nt.ndim or i == -1):
                self.assertEqual(unsqueezed_stride, torch.ones(2, dtype=torch.long))
            else:
                stride_col_after = nt_strides[:, size_idx]
                size_col_after = nt_sizes[:, size_idx]
                self.assertEqual(unsqueezed_stride, stride_col_after * size_col_after)
            nt_squeezed = nt_unsqueezed.squeeze(i)
            self.assertEqual(nt_squeezed, nt)
            self.assertEqual(nt_squeezed._nested_tensor_size(), nt_sizes)
            self.assertEqual(nt_squeezed._nested_tensor_strides(), nt_strides)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_transpose_inference_mode_interaction(self, device, dtype):
        nt = random_nt(device, dtype, 4, (4, 4))
        # Construct in default mode and transpose while in inference mode
        with torch.inference_mode():
            ntT = nt.transpose(-1, -2)
            ptT_from_ntT = noncontiguous_to_padded_tensor(ntT)
            pt = torch.nested.to_padded_tensor(nt, 0.0)
            ptT = pt.transpose(-1, -2)
            self.assertEqual(ptT, ptT_from_ntT)

        # Construct and transpose while in inference mode
        with torch.inference_mode():
            nt = random_nt(device, dtype, 4, (4, 4))
            ntT = nt.transpose(-1, -2)
            ptT_from_ntT = noncontiguous_to_padded_tensor(ntT)
            pt = torch.nested.to_padded_tensor(nt, 0.0)
            ptT = pt.transpose(-1, -2)
            self.assertEqual(ptT, ptT_from_ntT)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_view(self, device, dtype):
        nt = random_nt(device, dtype, 4, (4, 4))
        # error case: empty shape
        self.assertRaisesRegex(
            RuntimeError,
            r"shape '\[\]' is invalid for a nested tensor",
            lambda: nt.view(())
        )
        # error case: empty nested tensor
        nt_empty = torch.nested.nested_tensor([])
        self.assertRaisesRegex(
            RuntimeError,
            "empty nested tensor cannot be reshaped",
            lambda: nt_empty.view(-1)
        )
        # error case: -1 for batch size
        self.assertRaisesRegex(
            RuntimeError,
            r"view: For now nested view cannot change or infer the implicit batch dimension",
            lambda: nt.view(-1, 2, 3)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"shape '\[.*\]' is invalid for input of size [0-9]+",
            lambda: nt.view(4, 2, 3)
        )
        # normal case
        x0 = torch.randn((2, 20), device=device, dtype=dtype)
        x1 = torch.randn((3, 20), device=device, dtype=dtype)
        nt = torch.nested.nested_tensor([x0, x1])
        pt = torch.nested.to_padded_tensor(nt, 0.0)
        # error case, trying to reshape batch dim to a legit shape
        self.assertRaisesRegex(
            RuntimeError,
            r"For now nested view cannot change or infer the implicit batch dimension",
            lambda: nt.transpose(-1, -2).view(40, -1)
        )
        # inherit only the ragged dimension
        # (2, 20) -> (2, 5, 4)
        # (3, 20) -> (3, 5, 4)
        nt1 = nt.view(2, -1, 5, 4)
        # (2, 3, 20) -> (2, 3, 5, 4) -> (2, 4, 5, 4)
        pt1 = pt.view(2, -1, 5, 4)
        self.assertEqual(noncontiguous_to_padded_tensor(nt1), pt1)

        # more than one -1 (even for "old" dims), should fail
        # this attempts to do # (2, (2, 3), 5, 4) -> (2, (2, 3), 5, 2, 2)
        # but we ban "inherit old behavior" for >1 dimension
        self.assertRaisesRegex(
            RuntimeError,
            r"only one dimension can be inferred",
            lambda: nt1.view(2, -1, -1, 2, 2)
        )

    @dtypes(torch.float, torch.float16, torch.double)
    def test_view_inference_mode_interaction(self, device, dtype):
        # Construct in default mode and view while in inference mode
        nt = torch.nested.nested_tensor([torch.randn((2, 20)), torch.randn((3, 20))], device=device, dtype=dtype)
        with torch.inference_mode():
            ntT = nt.view(2, -1, 4, 5)
            ptT_from_ntT = noncontiguous_to_padded_tensor(ntT)
            pt = torch.nested.to_padded_tensor(nt, 0.0)
            ptT = pt.view(2, -1, 4, 5)
            self.assertEqual(ptT, ptT_from_ntT)
        # Construct and view while in inference mode
        with torch.inference_mode():
            nt = torch.nested.nested_tensor([torch.randn((2, 20)), torch.randn((3, 20))], device=device, dtype=dtype)
            ntT = nt.view(2, -1, 4, 5)
            ptT_from_ntT = noncontiguous_to_padded_tensor(ntT)
            pt = torch.nested.to_padded_tensor(nt, 0.0)
            ptT = pt.view(2, -1, 4, 5)
            self.assertEqual(ptT, ptT_from_ntT)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_reshape(self, device, dtype):
        nt = random_nt(device, dtype, 4, (4, 4))
        # error case: empty shape
        self.assertRaisesRegex(
            RuntimeError,
            r"shape '\[\]' is invalid for a nested tensor",
            lambda: nt.reshape(())
        )
        # error case: empty nested tensor
        nt_empty = torch.nested.nested_tensor([])
        self.assertRaisesRegex(
            RuntimeError,
            "empty nested tensor cannot be reshaped",
            lambda: nt_empty.reshape(-1)
        )
        # error case: -1 for batch size
        self.assertRaisesRegex(
            RuntimeError,
            r"reshape: For now nested reshape cannot change or infer the implicit batch dimension",
            lambda: nt.reshape(-1, 2, 3)
        )
        self.assertRaisesRegex(
            RuntimeError,
            r"shape '\[.*\]' is invalid for input of size [0-9]+",
            lambda: nt.reshape(4, 2, 3)
        )
        # normal case
        x0 = torch.randn((2, 20), device=device, dtype=dtype)
        x1 = torch.randn((3, 20), device=device, dtype=dtype)
        nt = torch.nested.nested_tensor([x0, x1])  # (2, (2, 3), 20)
        pt = torch.nested.to_padded_tensor(nt, 0.0)
        # error case, trying to reshape batch dim to a legit shape
        self.assertRaisesRegex(
            RuntimeError,
            r"reshape: For now nested reshape cannot change or infer the implicit batch dimension",
            lambda: nt.transpose(-1, -2).reshape(40, -1)
        )
        # inherit only the ragged dimension
        # (2, 20) -> (2, 5, 4)
        # (3, 20) -> (3, 5, 4)
        nt1 = nt.reshape(2, -1, 5, 4)
        # (2, 3, 20) -> (2, 3, 5, 4) -> (2, 4, 5, 4)
        pt1 = pt.reshape(2, -1, 5, 4)
        self.assertEqual(noncontiguous_to_padded_tensor(nt1), pt1)

        # more than one -1 (even for "old" dims), should fail
        # this attempts to do # (2, (2, 3), 5, 4) -> (2, (2, 3), 5, 2, 2)
        # but we ban "inherit old behavior" for >1 dimension
        self.assertRaisesRegex(
            RuntimeError,
            r"only one dimension can be inferred",
            lambda: nt1.reshape(2, -1, -1, 2, 2)
        )

    @dtypes(torch.float, torch.float16, torch.double)
    def test_narrow(self, device, dtype):
        nt = random_nt_from_dims([5, None, None, None], device=device, dtype=dtype)

        # narrow on dim=0 from start to end
        bounds = [(0, 5), (0, 3), (1, 2), (1, 5), (2, 4)]
        for start, end in bounds:
            length = end - start
            narrowed = nt.narrow(dim=0, start=start, length=length)
            # ensure output is a view
            self.assertTrue(narrowed._base is nt)
            for nc, c in zip(narrowed.unbind(), nt.unbind()[start:end]):
                self.assertEqual(nc, c)

        # dim != 0 is not supported
        for dim in range(1, nt.dim()):
            with self.assertRaisesRegex(RuntimeError, "only dim=0 supported for nested tensors"):
                nt.narrow(dim=dim, start=0, length=1)

        # error case: non-contiguous NT
        _, nt_noncont = random_nt_noncontiguous_pair((2, 3, 4))
        with self.assertRaisesRegex(RuntimeError, "only contiguous nested tensors supported"):
            nt_noncont.narrow(dim=0, start=0, length=1)

    @parametrize("input_dim", [3, 4])
    def test_scaled_dot_product_attention(self, device, input_dim):

        def rand_tensor(*shape):
            return torch.randn(shape, device=device)

        E = 8
        if input_dim == 3:
            # Shape: (N, L, E); ragged L
            query = torch.nested.nested_tensor([rand_tensor(2, E), rand_tensor(3, E), rand_tensor(4, E)])

            # Shape: (N, S, E); ragged S
            key = torch.nested.nested_tensor([rand_tensor(3, E), rand_tensor(4, E), rand_tensor(5, E)])
            value = torch.nested.nested_tensor([rand_tensor(3, E), rand_tensor(4, E), rand_tensor(5, E)])
        elif input_dim == 4:
            # In the 4D case the L and S is ragged
            # Shape: (N, N', L, E); ragged N' and L
            query = torch.nested.nested_tensor([rand_tensor(2, 2, E), rand_tensor(3, 3, E), rand_tensor(4, 4, E)])
            # Shape: (N, N', S, E); ragged N' and S
            key = torch.nested.nested_tensor([rand_tensor(2, 3, E), rand_tensor(3, 4, E), rand_tensor(4, 5, E)])
            value = torch.nested.nested_tensor([rand_tensor(2, 3, E), rand_tensor(3, 4, E), rand_tensor(4, 5, E)])
        else:
            self.fail(f"Invalid input_dim {input_dim} encountered in SDP test")

        def rand_mask(size):
            return torch.randint(0, 2, size=size, dtype=torch.bool, device=device)

        # Shape: (N, L, S); ragged L and S matching above
        attn_mask = torch.nested.nested_tensor([rand_mask((2, 3)), rand_mask((3, 4)), rand_mask((4, 5))])

        dropout_p = 0.0  # no dropout for reproducibility

        # Success case: no attn_mask set and is_causal=False.
        actual = torch.nn.functional.scaled_dot_product_attention(
            query, key, value, attn_mask=None, is_causal=False, dropout_p=dropout_p)

        expected_outputs = []
        for q, k, v in zip(query.unbind(), key.unbind(), value.unbind()):
            output = torch.nn.functional.scaled_dot_product_attention(
                q.unsqueeze(0), k.unsqueeze(0), v.unsqueeze(0), attn_mask=None, dropout_p=dropout_p)
            expected_outputs.append(output.squeeze(0))
        expected_output_nested = torch.nested.nested_tensor(expected_outputs)
        self.assertEqual(actual, expected_output_nested)

        # Error case: explicit attn_mask set.
        with self.assertRaisesRegex(RuntimeError, "not supported when an explicit attn_mask is set"):
            torch.nn.functional.scaled_dot_product_attention(
                query, key, value, attn_mask=attn_mask, dropout_p=dropout_p)

        # Error case: is_causal=True.
        with self.assertRaisesRegex(RuntimeError, "not supported when is_causal=True"):
            torch.nn.functional.scaled_dot_product_attention(
                query, key, value, dropout_p=dropout_p, is_causal=True)

    @dtypes(torch.float, torch.float16, torch.double)
    def test_empty_like(self, device, dtype):
        ntensors = 4
        nt = random_nt(device, dtype, ntensors, (4, 4))

        # Create empty on same device as original nested tensor
        nt_empty = torch.empty_like(nt)
        assert nt.is_same_size(nt_empty)
        self.assertEqual(nt.dtype, nt_empty.dtype)
        self.assertEqual(nt.device, nt_empty.device)
        self.assertEqual(nt.layout, nt_empty.layout)

        if torch.cuda.is_available():
            if device == "cpu":
                nt_cuda = torch.empty_like(nt, device='cuda')
                self.assertEqual(torch.device("cuda").type, nt_cuda.device.type)
            else:
                nt_cpu = torch.empty_like(nt, device='cpu')
                self.assertEqual(torch.device("cpu").type, nt_cpu.device.type)

        # Check changing dtype of empty_like nested tensor output
        dtype_set = {torch.float, torch.float16, torch.double}
        for other_dtype in dtype_set - {dtype}:
            nt_empty_other_dtype = torch.empty_like(nt, dtype=other_dtype)
            self.assertEqual(nt.dtype, dtype)
            self.assertEqual(nt_empty_other_dtype.dtype, other_dtype)
            self.assertEqual(nt.device, nt_empty.device)
            self.assertEqual(nt.layout, nt_empty.layout)

        # Create tensor for autograd
        nt_empty_req_grad = torch.empty_like(nt, requires_grad=True)
        self.assertEqual(nt_empty_req_grad.requires_grad, True)

        # Test noncontiguous tensor does not fail to copy
        nt_cont, nt_noncont = random_nt_noncontiguous_pair((2, 3, 6, 7))
        nt_empty = torch.empty_like(nt_cont)
        assert nt_cont.is_same_size(nt_empty)
        nt_empty_non_contig = torch.empty_like(nt_noncont)
        assert nt_noncont.is_same_size(nt_empty_non_contig)

        # Test the contiguous memory format option
        nt_empty_contig = torch.empty_like(nt_cont, memory_format=torch.contiguous_format)
        assert nt_cont.is_same_size(nt_empty_contig)
        assert nt_empty_contig.is_contiguous()

        nt_empty_non_contig = torch.empty_like(nt_noncont, memory_format=torch.contiguous_format)
        assert nt_noncont.is_same_size(nt_empty_non_contig)
        assert nt_empty_non_contig.is_contiguous()

        # Test other memory formats fail
        self.assertRaises(RuntimeError, lambda: torch.empty_like(nt_cont, memory_format=torch.channels_last))
        self.assertRaises(RuntimeError, lambda: torch.empty_like(nt_noncont, memory_format=torch.channels_last))
        self.assertRaises(RuntimeError, lambda: torch.empty_like(nt_cont, memory_format=torch.channels_last_3d))
        self.assertRaises(RuntimeError, lambda: torch.empty_like(nt_noncont, memory_format=torch.channels_last_3d))

@markDynamoStrictTest
class TestNestedTensorAutograd(TestCase):
    # Note [Gradcheck args check_batched_grad=False] the common_utils testing version of gradcheck
    # includes the default parameters used for testing ops with gradcheck. However nested tensor
    # does not support the stack op therefore we turn it off for these tests
    def _create_leaf_nested_tensor_from_list(self, tensor_device, requires_grad=False):
        return torch.nested.nested_tensor([torch.randn(1, 2,),
                                           torch.randn(7, 8)], requires_grad=requires_grad, device=tensor_device)

    def _create_nested_tensor_from_list(self, tensor_device, requires_grad=False):
        return torch.nested.as_nested_tensor([torch.randn(1, 2, requires_grad=requires_grad),
                                              torch.randn(7, 8, requires_grad=requires_grad)], device=tensor_device)

    def _create_nested_tensor_from_mask(self, tensor_device, requires_grad=False):
        data = torch.randn(2, 3, 4, requires_grad=requires_grad, device=tensor_device)
        mask = torch.ones_like(data[:, :, 0]).bool()
        return torch._nested_tensor_from_mask(data, mask)

    def test_as_nested_tensor_propagates_gradients(self, device):
        a = torch.arange(3, dtype=torch.float, device=device)
        b = torch.arange(5, dtype=torch.float, device=device)
        nt = torch.nested.as_nested_tensor([a, b])
        # tensors with requires_grad=False are leaves
        self.assertTrue(nt.is_leaf)
        self.assertTrue(not nt.requires_grad)

        a = torch.arange(3, dtype=torch.float, requires_grad=True, device=device)
        b = torch.arange(5, dtype=torch.float, requires_grad=True, device=device)
        nt2 = torch.nested.as_nested_tensor([a, b])
        fake_grad = torch.nested.nested_tensor([torch.ones_like(a), torch.zeros_like(b)], device=device)
        nt2.backward(fake_grad)
        self.assertEqual(a.grad, fake_grad[0])
        self.assertEqual(b.grad, fake_grad[1])

    def test_nested_tensor_generates_leaf(self, device):
        a = torch.arange(3, dtype=torch.float, requires_grad=True, device=device)
        b = torch.arange(5, dtype=torch.float, requires_grad=True, device=device)

        nt = torch.nested.nested_tensor([a, b], requires_grad=False)
        self.assertTrue(nt.is_leaf)
        self.assertTrue(not nt.requires_grad)

        nt2 = torch.nested.nested_tensor([a, b], requires_grad=True)
        self.assertTrue(nt2.is_leaf)
        self.assertTrue(nt2.requires_grad)

        fake_grad = torch.nested.nested_tensor([torch.ones_like(a), torch.zeros_like(b)], device=device)
        nt2.backward(fake_grad)
        self.assertEqual(nt2.grad, fake_grad)
        self.assertEqual(a.grad, None)
        self.assertEqual(b.grad, None)

    def test_set_requires_grad_from_list(self, device):
        nt = self._create_nested_tensor_from_list(device)
        nt.requires_grad_()
        assert nt.requires_grad

    def test_set_requires_grad_from_mask(self, device):
        nt = self._create_nested_tensor_from_mask(device)
        nt.requires_grad_()
        assert nt.requires_grad

    def test_backward_for_add_op(self, device):
        nt_1 = self._create_nested_tensor_from_mask(device)
        nt_2 = self._create_nested_tensor_from_mask(device)

        nt_1.requires_grad_()
        c = nt_1 + nt_2

        assert nt_1.requires_grad
        assert c.requires_grad
        grad_output = self._create_nested_tensor_from_mask(device)
        c.backward(grad_output)

        #  Grad check doesn't work with nested yet.
        # d/dnt_1 (nt + nt_1) = 1*grad_output
        self.assertEqual(nt_1.grad, grad_output)

    def test_backward_for_sub_op(self, device):
        nt_1 = self._create_nested_tensor_from_mask(device)
        nt_2 = self._create_nested_tensor_from_mask(device)

        nt_1.requires_grad_()
        nt_2.requires_grad_()
        c = nt_1 - nt_2

        assert nt_1.requires_grad
        assert nt_2.requires_grad
        assert c.requires_grad
        grad_output = self._create_nested_tensor_from_mask(device)
        c.backward(grad_output)

        self.assertEqual(nt_1.grad, grad_output)
        self.assertEqual(nt_2.grad, -1 * grad_output)

    def test_backward_sub_strided(self, device):
        a = torch.nested.nested_tensor([torch.randn(9, 2, 4), torch.randn(12, 2, 4)], requires_grad=True, device=device)
        b = torch.nested.nested_tensor([torch.randn(9, 4, 2), torch.randn(12, 4, 2)], requires_grad=True, device=device)
        c = a - b.transpose(-1, -2)
        grad_output = c.clone()
        c.backward(grad_output)
        self.assertEqual(a.grad, grad_output)
        self.assertEqual(b.grad, -1 * grad_output.transpose(-1, -2))

    def test_backward_add_strided(self, device):
        a = torch.nested.nested_tensor([torch.randn(9, 2, 4), torch.randn(12, 2, 4)], requires_grad=True, device=device)
        b = torch.nested.nested_tensor([torch.randn(9, 4, 2), torch.randn(12, 4, 2)], requires_grad=True, device=device)
        c = a + b.transpose(-1, -2)
        grad_output = c.clone()
        c.backward(grad_output)
        self.assertEqual(a.grad, grad_output)
        self.assertEqual(b.grad, grad_output.transpose(-1, -2))

    # Test Factory Functions
    def test_nested_tensor_to_padded_tensor(self, device):
        for padding_val in [0, 1]:
            nt = self._create_leaf_nested_tensor_from_list(tensor_device=device, requires_grad=True)

            out = torch.nested.to_padded_tensor(nt, padding_val)
            grad_output = torch.ones(out.shape, device=device)
            out.backward(grad_output)

            self.assertEqual(nt.grad, torch.nested.nested_tensor([torch.ones(1, 2), torch.ones(7, 8)], device=device))

    def test_nested_tensor_from_mask_and_to_padded(self, device):
        N, L, D = 2, 4, 4
        mask = torch.ones(N, L, device=device)
        for i in range(1, N):
            end = torch.randint(1, L - 1, (1,), device=device)
            mask[i, end:] = 0

        mask[0, :] = 1
        mask = mask.bool()

        data = torch.randn(N, L, D, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(inpt):
            nt = torch._nested_tensor_from_mask(inpt, mask)
            # This implicitly tests to_padded_tensor grads
            return torch.nested.to_padded_tensor(nt, 0)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_nested_tensor_from_padded(self, device):
        nested_size = torch.tensor([[1, 2], [2, 2]])
        padded_tensor = torch.randn(2, 2, 2, dtype=torch.float64, device=device)
        padded_tensor[0, 1, :] = 0
        padded_tensor.requires_grad_()

        def grad_test_func(tensor, nested_size):
            nt = torch._nested_from_padded(tensor, nested_size, fuse_transform_0213=False)
            # This implicitly tests to_padded_tensor grads
            return torch.nested.to_padded_tensor(nt, 0)

        data = (padded_tensor, nested_size)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_nested_tensor_from_padded_fused(self, device):
        nested_size = torch.tensor([[1, 8], [2, 8]])
        padded_tensor = torch.randn(2, 2, 2, 4, dtype=torch.float64, device=device)
        padded_tensor[0, 1, :] = 0
        padded_tensor.requires_grad_()

        def grad_test_func(tensor, nested_size):
            nt = torch._nested_from_padded(tensor, nested_size, fuse_transform_0213=True)
            # This implicitly tests to_padded_tensor grads
            return torch.nested.to_padded_tensor(nt, 0)
        data = (padded_tensor, nested_size)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_nested_tensor_from_list(self, device):

        a = torch.randn(1, 2, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(10, 2, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            c = torch.nested.as_nested_tensor([a, b, c])
            # This implictily tests to_padded_tensor grads
            return torch.nested.to_padded_tensor(c, 0)
        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    @decorateIf(
        xfailIfTorchDynamo,
        # only fails in python 3.11. TODO: Debug this!
        lambda params: params["layout"] == torch.jagged and sys.version_info >= (3, 11)
    )
    @parametrize("layout", [torch.strided, torch.jagged], name_fn=layout_name)
    def test_dropout_backward(self, layout):
        if layout == torch.jagged:
            nt = torch.nested.nested_tensor([torch.randn((2, 5)), torch.randn((3, 5))], requires_grad=True, layout=layout)
        else:
            nt = torch.nested.nested_tensor([torch.randn((2, 5)), torch.randn((3, 4))], requires_grad=True, layout=layout)
        p = 0.2
        y = torch.nn.functional.dropout(nt, p)
        y.backward(nt.clone().detach())
        self.assertEqual(nt.grad, y)

    def test_nested_tensor_bmm_gradcheck(self, device):
        a = torch.randn(2, 6, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 6, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(6, 4, requires_grad=True, dtype=torch.float64, device=device)
        d = torch.randn(6, 5, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, d):
            nt0 = torch.nested.as_nested_tensor([a, b])
            nt1 = torch.nested.as_nested_tensor([c, d])
            result = nt0.bmm(nt1)
            return torch.nested.to_padded_tensor(result, 0.0)

        data = (a, b, c, d)
        assert torch.autograd.gradcheck(grad_test_func, inputs=data)

    def test_nested_tensor_bmm_backward(self, device):
        nt0 = torch.nested.nested_tensor([torch.randn((2, 6)), torch.randn((3, 6))], requires_grad=True, device=device)
        nt1 = torch.nested.nested_tensor([torch.randn((6, 4)), torch.randn((6, 5))], requires_grad=True, device=device)
        with torch.no_grad():
            pt0 = torch.nested.to_padded_tensor(nt0, 0.0).requires_grad_(True)
            pt1 = torch.nested.to_padded_tensor(nt1, 0.0).requires_grad_(True)

        ynt = nt0.bmm(nt1)
        ypt = pt0.bmm(pt1)
        ynt.backward(ynt.clone())
        ypt.backward(ypt.clone())

        self.assertEqual(torch.nested.to_padded_tensor(nt0.grad, 0.0), pt0.grad)
        self.assertEqual(torch.nested.to_padded_tensor(nt1.grad, 0.0), pt1.grad)

    def test_nested_tensor_matmul_gradcheck(self, device):
        a = torch.randn(2, 6, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 6, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(6, 4, requires_grad=True, dtype=torch.float64, device=device)
        d = torch.randn(6, 5, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, d):
            nt0 = torch.nested.as_nested_tensor([a, b])
            nt1 = torch.nested.as_nested_tensor([c, d])
            result = torch.matmul(nt0, nt1)
            return torch.nested.to_padded_tensor(result, 0.0)

        data = (a, b, c, d)
        assert torch.autograd.gradcheck(grad_test_func, inputs=data)

    def test_nested_tensor_matmul_backward(self, device):
        nt0 = torch.nested.nested_tensor([torch.randn((7, 2, 6)), torch.randn((7, 3, 6))], requires_grad=True, device=device)
        nt1 = torch.nested.nested_tensor([torch.randn((7, 6, 4)), torch.randn((7, 6, 5))], requires_grad=True, device=device)
        with torch.no_grad():
            pt0 = torch.nested.to_padded_tensor(nt0, 0.0).requires_grad_(True)
            pt1 = torch.nested.to_padded_tensor(nt1, 0.0).requires_grad_(True)

        ynt = torch.matmul(nt0, nt1)
        ypt = torch.matmul(pt0, pt1)
        ynt.backward(ynt.clone())
        ypt.backward(ypt.clone())

        self.assertEqual(torch.nested.to_padded_tensor(nt0.grad, 0.0), pt0.grad)
        self.assertEqual(torch.nested.to_padded_tensor(nt1.grad, 0.0), pt1.grad)

    def test_nested_tensor_transpose_gradcheck(self, device):
        a = torch.randn(2, 5, requires_grad=True, device=device)
        b = torch.randn(3, 4, requires_grad=True, device=device)

        def grad_test_func(a, b):
            nt = torch.nested.as_nested_tensor([a, b])
            result = nt.transpose(-2, -1).transpose(-2, -1)
            return torch.nested.to_padded_tensor(result, 0.0)

        data = (a, b)
        assert torch.autograd.gradcheck(grad_test_func, inputs=data, eps=1e-3)

    def test_nested_tensor_transpose_backward(self, device):
        nt = torch.nested.nested_tensor([torch.randn((2, 5)), torch.randn((3, 4))], requires_grad=True, device=device)
        with torch.no_grad():
            pt = torch.nested.to_padded_tensor(nt, 0.0).requires_grad_(True)

        ynt = nt.transpose(-2, -1)
        ypt = pt.transpose(-2, -1)
        ynt.backward(ynt.clone())
        ypt.backward(ypt.clone())

        self.assertEqual(torch.nested.to_padded_tensor(nt.grad, 0.0), pt.grad)

    def test_nested_tensor_reshape_gradcheck(self, device):
        a = torch.randn(2, 6, requires_grad=True, device=device)
        b = torch.randn(3, 6, requires_grad=True, device=device)

        def grad_test_func(a, b):
            nt = torch.nested.as_nested_tensor([a, b])
            result = nt.reshape(2, -1, 2, 3)
            return torch.nested.to_padded_tensor(result, 0.0)

        data = (a, b)
        assert torch.autograd.gradcheck(grad_test_func, inputs=data, eps=1e-3)

    def test_nested_tensor_reshape_backward(self):
        nt = torch.nested.nested_tensor([torch.randn((2, 6)), torch.randn((3, 6))], requires_grad=True)
        with torch.no_grad():
            pt = torch.nested.to_padded_tensor(nt, 0.0).requires_grad_(True)

        ynt = nt.reshape(2, -1, 2, 3)
        ypt = pt.reshape(2, -1, 2, 3)
        ynt.backward(ynt.clone())
        ypt.backward(ypt.clone())

        self.assertEqual(torch.nested.to_padded_tensor(nt.grad, 0.0), pt.grad)

    def test_nested_tensor_squeeze_backward(self, device):
        nt = torch.nested.nested_tensor([torch.randn((2, 6, 1)), torch.randn((3, 6, 1))], requires_grad=True, device=device)
        with torch.no_grad():
            pt = torch.nested.to_padded_tensor(nt, 0.0).requires_grad_(True)

        ynt = nt.squeeze(-1)
        ypt = pt.squeeze(-1)
        ynt.backward(ynt.clone())
        ypt.backward(ypt.clone())

        self.assertEqual(torch.nested.to_padded_tensor(nt.grad, 0.0), pt.grad)

    def test_nested_tensor_squeeze_gradcheck(self, device):
        a = torch.randn((2, 6, 1), dtype=torch.float64, requires_grad=True, device=device)
        b = torch.randn((3, 6, 1), dtype=torch.float64, requires_grad=True, device=device)

        def grad_test_func(a, b):
            nt = torch.nested.as_nested_tensor([a, b])
            result = nt.squeeze(-1)
            return torch.nested.to_padded_tensor(result, 0.0)

        assert torch.autograd.gradcheck(grad_test_func, inputs=(a, b), eps=1e-3)

    def test_nested_tensor_unsqueeze_backward(self, device):
        nt = torch.nested.nested_tensor([torch.randn((2, 6)), torch.randn((3, 6))], requires_grad=True, device=device)
        with torch.no_grad():
            pt = torch.nested.to_padded_tensor(nt, 0.0).requires_grad_(True)

        ynt = nt.unsqueeze(2)
        ypt = pt.unsqueeze(2)
        ynt.backward(ynt.clone())
        ypt.backward(ypt.clone())

        self.assertEqual(torch.nested.to_padded_tensor(nt.grad, 0.0), pt.grad)

    def test_nested_tensor_unsqueeze_gradcheck(self, device):
        a = torch.randn((2, 6), dtype=torch.float64, requires_grad=True, device=device)
        b = torch.randn((3, 6), dtype=torch.float64, requires_grad=True, device=device)

        def grad_test_func(a, b):
            nt = torch.nested.as_nested_tensor([a, b])
            result = nt.unsqueeze(-1)
            return torch.nested.to_padded_tensor(result, 0.0)

        assert torch.autograd.gradcheck(grad_test_func, inputs=(a, b), eps=1e-3)

    def test_nested_tensor_linear(self, device):

        a = torch.randn(1, 2, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, requires_grad=True, dtype=torch.float64, device=device)

        weight = torch.randn(2, 2, requires_grad=True, dtype=torch.float64, device=device)
        bias = torch.randn(2, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, weight, bias=None):
            nt = torch.nested.as_nested_tensor([a, b, c])
            # This implicitly tests to_padded_tensor grads
            d = torch.functional.F.linear(nt, weight, bias)
            return torch.nested.to_padded_tensor(d, 0)
        data = (a, b, c, weight, bias)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

        # Test linear with no bias added
        data = (a, b, c, weight)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_nested_tensor_linear_plus_transpose(self, device):
        a = torch.randn(1, 2, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, requires_grad=True, dtype=torch.float64, device=device)

        weight = torch.randn(2, 2, requires_grad=True, dtype=torch.float64, device=device)
        bias = torch.randn(2, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, weight, bias=None):
            nt = torch.nested.as_nested_tensor([a, b, c])
            # This implicitly tests to_padded_tensor grads
            d = torch.functional.F.linear(nt, weight, bias)
            d = d.transpose(-1, -2).contiguous()
            return torch.nested.to_padded_tensor(d, 0)
        data = (a, b, c, weight, bias)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

        # Test linear with no bias added
        data = (a, b, c, weight)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_nested_tensor_softmax(self, device):
        a = torch.randn(1, 2, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, dim):
            nt = torch.nested.as_nested_tensor([a, b, c])
            # This implicitly tests to_padded_tensor grads
            d = torch.functional.F.softmax(nt, dim=dim)
            return torch.nested.to_padded_tensor(d, 0)

        # softmax over last dim
        data = (a, b, c, -1)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_nested_tensor_linear_backward(self, device):
        a = torch.randn(1, 2, requires_grad=False, device=device)
        b = torch.randn(2, 2, requires_grad=False, device=device)
        c = torch.randn(3, 2, requires_grad=False, device=device)

        weight = torch.randn(2, 2, requires_grad=True, device=device)
        bias = torch.randn(2, requires_grad=True, device=device)
        nt = torch.nested.as_nested_tensor([a, b, c], device=device)

        out = torch.functional.F.linear(nt, weight, bias)

        out.backward(out.clone())

        assert weight.grad is not None
        assert bias.grad is not None

        assert a.grad is None
        assert b.grad is None
        assert c.grad is None

    def test_values_grad_with_broadcast(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            buffer = nt.values()
            return buffer.sum()

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_to_buffer_series_ops_grad_with_broadcast(self, device):
        a = torch.randn(1, 1, 2, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(1, 1, 2, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(1, 1, 2, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            buffer = nt.values()
            buffer = buffer * 2
            return buffer.exp()

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_unbind_flow_through(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            ntT = nt.transpose(-1, -2)
            unbound = ntT.unbind()
            d = unbound[0]
            d = torch.pow(d, 2)
            return d

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_split_with_sizes_flow_through(self, device):
        a = torch.randn(2, 5, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 5, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 5, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            splits = nt.split_with_sizes([2, 3], dim=-1)
            unbound = splits[1].unbind()
            d = unbound[0]
            d = torch.pow(d, 2)
            return d

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_indexing_backward(self, device):
        x0 = torch.randn((2, 5))
        x1 = torch.randn((3, 4))
        nt = torch.nested.nested_tensor([x0, x1], device=device, requires_grad=True)
        self.assertEqual(nt[0], x0)
        self.assertEqual(nt[-1], x1)
        grad_x0 = torch.randn((2, 5), device=device)
        nt[0].backward(grad_x0)
        expected_grad = torch.nested.nested_tensor([grad_x0, torch.zeros((3, 4), device=device)])
        self.assertEqual(nt.grad, expected_grad)

    def test_masked_fill_backward(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            mask = nt.detach().clone().to(bool)
            out = nt.masked_fill(mask, 0)
            out = torch.nested.to_padded_tensor(out, 0)
            return out
        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_gelu_backward(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            nt_gelu = torch.nn.functional.gelu(nt)
            return torch.nested.to_padded_tensor(nt_gelu, 0)

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_relu_backward(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            nt_relu = torch.nn.functional.relu(nt)
            return torch.nested.to_padded_tensor(nt_relu, 0)

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_selu_backward(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            nt_relu = torch.nn.functional.silu(nt)
            return torch.nested.to_padded_tensor(nt_relu, 0)

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    def test_abs_backward(self, device):
        a = torch.randn(1, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            nt_abs = torch.abs(nt)
            return torch.nested.to_padded_tensor(nt_abs, 0)

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    # Previously would error when input NT doesn't require grad
    # NotImplementedError: Cannot access storage of UndefinedTensorImpl
    def test_layer_norm_backward_edge_case(self, device):
        size = 4
        a = torch.randn(1, 2, size, requires_grad=False, dtype=torch.float64, device=device)
        nt = torch.nested.nested_tensor([a])
        nt_layer_norm = torch.nn.LayerNorm(nt.size(-1), device=device, dtype=torch.float64)
        out = nt_layer_norm(nt)
        out.backward(out.clone())

    def test_accumulate_grad_different_strides(self, device):
        a = torch.rand(1, 4, 2, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.rand(1, 8, 2, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b):
            nt_1 = torch.nested.as_nested_tensor([a, b])
            nt_2 = nt_1.clone()
            out = torch.nn.functional.scaled_dot_product_attention(nt_1, nt_2, nt_2)
            return torch.nested.to_padded_tensor(out, 0)

        data = (a, b)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    # https://github.com/pytorch/pytorch/issues/95562
    @skipIfSlowGradcheckEnv
    @parametrize("size", [1024, 1023, 513, 512, 256, 128, 32, 4, 2])
    def test_layer_norm_backward(self, device, size):
        a = torch.randn(1, 2, size, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(2, 2, size, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(3, 2, size, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            layer_norm = torch.nn.LayerNorm(nt.size(-1), device=device, dtype=torch.float64)
            nt_layer_norm = layer_norm(nt)
            return torch.nested.to_padded_tensor(nt_layer_norm, 0)

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

    # https://github.com/pytorch/pytorch/issues/95562
    @skipIfSlowGradcheckEnv
    # Could either mark slow or reduce size
    @parametrize("size", [128, 32, 4, 2])
    def test_layer_norm_backward_5d(self, device, size):
        a = torch.randn(4, size, size, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(7, size, size, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(10, size, size, 4, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c])
            layer_norm = torch.nn.LayerNorm((size, size, nt.size(-1)), device=device, dtype=torch.float64)
            nt_layer_norm = layer_norm(nt)
            return torch.nested.to_padded_tensor(nt_layer_norm, 0)

        data = (a, b, c)
        assert gradcheck(grad_test_func, inputs=data, check_batched_grad=False)

# Found in torch/testing/_comparison.py
default_atol = {torch.float16: 1e-3, torch.bfloat16: 1e-3, torch.float32: 1e-5}
default_rtol = {torch.float16: 1e-3, torch.bfloat16: 1.6e-2, torch.float32: 1.3e-6}

def get_rtol(true_value: torch.Tensor, computed_value: torch.Tensor) -> float:
    deviation = true_value - computed_value
    deviation = torch.abs(deviation / true_value)
    # Fill in the nans with the default rtol
    torch.nan_to_num_(deviation, nan=default_rtol[computed_value.dtype])
    return deviation.max().item()


def get_atol(true_value: torch.Tensor, computed_value: torch.Tensor) -> float:
    deviation = true_value - computed_value
    atol = torch.abs(deviation).max().item()
    return atol


def get_tolerances(
    true_value: torch.Tensor,
    computed_value: torch.Tensor,
    fudge_factor: Optional[float] = None,
) -> Tuple[float, float]:
    """Returns the absolute and relative tolerances for comparing two tensors."""
    fudge_factor = fudge_factor if fudge_factor is not None else 1.0
    atol = get_atol(true_value, computed_value)
    rtol = get_rtol(true_value, computed_value)

    atol = fudge_factor * max(atol, default_atol[computed_value.dtype])
    rtol = fudge_factor * max(rtol, default_rtol[computed_value.dtype])
    # torch.isclose() has weird behavior around see:
    # https://github.com/pytorch/pytorch/issues/102400
    if rtol > 1e30:
        rtol = default_rtol[computed_value.dtype]
    return atol, rtol

# We can probably parametrizing existing tests instead of having a separate
# test class as we begin to support more ops. Also maybe rewrite with OpInfos.
@markDynamoStrictTest
class TestNestedTensorSubclass(TestCase):
    # TODO: consolidate with the below
    def _get_list_for_jagged_tensor(self, nested_size, device, requires_grad=True):
        Ds = nested_size[1:]
        out = []
        for s in nested_size[0]:
            out.append(
                torch.randn(s, *Ds, requires_grad=requires_grad, device=device, dtype=torch.float64)
            )
        return out

    def _get_example_tensor_lists(self, include_list_of_lists=True, include_requires_grad=True):

        def _make_tensor(*shape, include_requires_grad=include_requires_grad, requires_grad=True):
            return torch.randn(
                *shape,
                requires_grad=(requires_grad if include_requires_grad else False)
            )

        # Purposefully introduce mixed requires_grad settings for the components
        # when include_requires_grad=True.
        example_lists = [
            # (B, *, D) with B=4
            [
                _make_tensor(2, 5),
                _make_tensor(3, 5, requires_grad=False),
                _make_tensor(4, 5, requires_grad=False),
                _make_tensor(6, 5)
            ],
            # (B, *, D_0, D_1) with B=5
            [
                _make_tensor(2, 5, 6),
                _make_tensor(3, 5, 6),
                _make_tensor(4, 5, 6, requires_grad=False),
                _make_tensor(5, 5, 6),
                _make_tensor(6, 5, 6),
            ],
        ]

        if include_list_of_lists:
            example_lists.append(
                # (B, *, D) with B=3 in list form
                [
                    _make_tensor(2, 5, requires_grad=False).tolist(),
                    _make_tensor(3, 5).tolist(),
                    _make_tensor(4, 5).tolist(),
                ])

        return example_lists

    def test_tensor_attributes(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)
        nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
        _offsets = nt.offsets()

        for op in (
            torch.ops.aten.is_non_overlapping_and_dense.default,
            torch.ops.aten.sym_size.default,
            torch.ops.aten.dim.default,
            torch.ops.aten.sym_numel.default,
            torch.ops.aten.sym_stride.default,
            torch.ops.aten.sym_storage_offset.default,
        ):
            op(nt)

        with self.assertRaisesRegex(RuntimeError,
                                    "directly calling torch.ops.aten.size"):
            torch.ops.aten.size.default(nt)

        nested_int = torch.nested._internal.nested_tensor.get_tensor_symint(_offsets, coeff=1)
        self.assertEqual(nt.size(), (3, nested_int, 3))
        self.assertEqual(nt.shape, (3, nested_int, 3))
        self.assertEqual(nt.dim(), 3)
        self.assertEqual(nt.numel(), 27)

    def test_linear(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)
        weight = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, weight):
            nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
            out = torch.nn.functional.linear(nt, weight)
            return out.values()

        gradcheck(grad_test_func, inputs=(a, b, c, weight), check_batched_grad=False)

    def test_unary_pointwise(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
            out = torch.nn.functional.silu(nt.sin().cos())
            return out.values()

        gradcheck(grad_test_func, inputs=(a, b, c), check_batched_grad=False)

    def test_unary_pointwise_transposed_inputs(self, device):
        a, b, c = (
            torch.randn(i + 2, 5, requires_grad=True, dtype=torch.float64, device=device) for i in range(3)
        )

        nt = torch.nested.nested_tensor([a.detach(), b.detach(), c.detach()], layout=torch.jagged)
        nt_t = nt.transpose(1, 2)
        self.assertFalse(nt_t.is_contiguous())
        out = torch.nn.functional.silu(nt_t.sin().cos())
        self.assertEqual(out.is_contiguous(), torch.nn.functional.silu(b.transpose(-1, -2).sin().cos()).is_contiguous())

        self.assertEqual(nt_t.shape, out.shape)

        a, b, c = (
            torch.randn(i + 2, 5, requires_grad=True, dtype=torch.float64, device=device) for i in range(3)
        )

        def grad_test_func(a, b, c):
            nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
            nt_t = nt.transpose(1, 2)
            out = torch.nn.functional.silu(nt_t.sin().cos())
            return out.values()

        gradcheck(grad_test_func, inputs=(a, b, c), check_batched_grad=False)


    def test_binary_pointwise(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)

        # Incorrect usage: shape check will fail if the offsets tensor are not
        #                  the same exact tensor object
        nt1 = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
        nt2 = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)

        self.assertRaisesRegex(
            RuntimeError,
            "cannot call binary pointwise function .* with inputs of shapes",
            lambda: nt1 * nt2)

        # Correct usage: chain the calls using the same offsets tensor object
        def grad_test_func(a, b, c):
            nt1 = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
            # TODO: Switch to public API that takes in (values, offsets) once it exists
            nt2, offsets = jagged_from_list([a, b, c], nt1.offsets())
            out = nt1 * nt2
            return out.values()

        gradcheck(grad_test_func, inputs=(a, b, c), check_batched_grad=False)

    def test_binary_pointwise_transposed(self, device):
        a, b, c = (
            torch.randn(i + 2, 5, dtype=torch.float64, device=device) for i in range(3)
        )

        nt1, offsets = jagged_from_list([a, b, c], None)
        nt2, offsets = jagged_from_list([a, b, c], offsets)

        nt1_t = nt1.transpose(1, 2)
        nt2_t = nt2.transpose(1, 2)

        # out = nt1_t * nt2_t
        # self.assertFalse(nt1_t.is_contiguous())
        # self.assertEqual(out.is_contiguous(), (b.transpose(-1, -2) * b.transpose(-1, -2)).is_contiguous())
        # self.assertEqual(out.shape, nt1_t.shape)

        self.assertRaisesRegex(
            RuntimeError,
            "cannot call binary pointwise function mul.Tensor with inputs of shapes",
            lambda: nt1 * nt2_t,
        )

        a, b, c = (
            torch.randn(i + 2, 5, requires_grad=True, dtype=torch.float64, device=device) for i in range(3)
        )

        # Correct usage: chain the calls using the same offsets tensor object
        def grad_test_func(a, b, c):
            nt1, offsets = jagged_from_list([a, b, c], None)
            nt2, offsets = jagged_from_list([a, b, c], offsets)
            nt1_t = nt1.transpose(1, 2)
            nt2_t = nt2.transpose(1, 2)
            out = nt1_t * nt2_t
            return out.values()

        gradcheck(grad_test_func, inputs=(a, b, c), check_batched_grad=False)

    def test_split(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)

        nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
        out = torch.split(nt, 2, -1)
        self.assertEqual(len(out), 2)
        self.assertEqual(
            out[0],
            torch.nested.as_nested_tensor([a[:, 0:2], b[:, 0:2], c[:, 0:2]], layout=torch.jagged)
        )
        self.assertEqual(
            out[1],
            torch.nested.as_nested_tensor([a[:, 2:], b[:, 2:], c[:, 2:]], layout=torch.jagged)
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"split\(\): not supported for NestedTensor on dim=1",
        ):
            torch.split(nt, 2, 1)

    def test_split_with_sizes(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)

        nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
        out = torch.split(nt, [1, 2], -1)
        self.assertEqual(len(out), 2)
        self.assertEqual(
            out[0],
            torch.nested.as_nested_tensor([a[:, 0:1], b[:, 0:1], c[:, 0:1]], layout=torch.jagged)
        )
        self.assertEqual(
            out[1],
            torch.nested.as_nested_tensor([a[:, 1:], b[:, 1:], c[:, 1:]], layout=torch.jagged)
        )
        with self.assertRaisesRegex(
            RuntimeError,
            r"split_with_sizes\(\): not supported for NestedTensor on dim=1",
        ):
            torch.split(nt, [1, 2], 1)

    def test_views_inherit_ragged_dim(self, device):
        # view
        nt = random_nt_from_dims(
            [4, None, 8, 10], device=device, dtype=torch.float32, layout=torch.jagged)
        # inherit ragged dim via -1
        view = nt.view(4, -1, 80)
        self.assertEqual(nt.shape[1], view.shape[1])
        # inherit batch and ragged dims via -1
        view2 = nt.view(-1, -1, 80)
        self.assertEqual(nt.shape[:2], view2.shape[:2])

        # expand
        nt = random_nt_from_dims(
            [3, None, 1], device=device, dtype=torch.float32, layout=torch.jagged)
        # inherit batch and ragged dims via -1
        view = nt.expand(-1, -1, 5)
        self.assertEqual(nt.shape[:2], view.shape[:2])

    def test_view_ragged_idx_not_one(self, device):
        nt = random_nt_from_dims([2, None, 20], device=device, dtype=torch.float32, layout=torch.jagged)

        view_transposed = nt.transpose(1, 2).view(2, 20, nt.size(1))
        self.assertEqual((2, 20, nt.size(1)), (view_transposed.size()))
        self.assertEqual(view_transposed._base, nt._base)

    def test_unsafe_view(self, device):
        nt = random_nt_from_dims([4, None, 8, 10], device=device, dtype=torch.float32, layout=torch.jagged)
        # basic view
        view1 = torch.ops.aten._unsafe_view(nt, (4, -1, 80))
        self.assertEqual((4, nt.size(1), 80), tuple(view1.size()))
        # _unsafe_view differs from view in that the view information is not tracked
        self.assertTrue(view1._base is None)

        # test an unsafe_view when ragged_idx != 1, currently only supports identity view
        nt_t = nt.transpose(1, 2)
        view2 = torch.ops.aten._unsafe_view(nt_t, (4, 8, nt.size(1), 10))
        self.assertEqual((4, 8, nt.size(1), 10), tuple(view2.size()))
        self.assertTrue(view2._base is None)

    @xfailIfTorchDynamo
    @parametrize("requires_grad", [False, True])
    def test_reshape_decomp(self, device, requires_grad):
        # contiguous NT should result in view.
        nt = random_nt_from_dims(
            [3, None, 10],
            device=device,
            dtype=torch.float32,
            layout=torch.jagged,
        ).detach().requires_grad_(requires_grad)
        view = nt.reshape(-1, -1, 5, 2)
        self.assertEqual(view.shape[:2], nt.shape[:2])
        self.assertTrue(view._is_view() and view._base is nt)
        # make sure gradients flow back
        if requires_grad:
            view.backward(torch.ones_like(view))
            self.assertEqual(nt.grad, torch.ones_like(nt))

        # non-contiguous NT should result in contiguous copy
        nt = random_nt_from_dims(
            [3, None, 5, 2],
            device=device,
            dtype=torch.float32,
            layout=torch.jagged,
            requires_grad=requires_grad
        )
        nt_noncontig = nt.transpose(-1, -2)
        self.assertFalse(nt_noncontig.is_contiguous())
        copy = nt_noncontig.reshape(-1, -1, 10)
        self.assertTrue(copy.is_contiguous())
        self.assertEqual(copy.shape[:2], nt.shape[:2])
        # make sure gradients flow back
        if requires_grad:
            copy.backward(torch.ones_like(copy))
            self.assertEqual(nt.grad, torch.ones_like(nt))

    def test_flatten_decomp(self, device):
        nt = random_nt_from_dims(
            [3, None, 5, 2], device=device, dtype=torch.float32, layout=torch.jagged)
        flattened = nt.flatten(-2, -1)
        self.assertEqual(flattened.shape, nt.view(3, -1, 10).shape)

        nt = random_nt_from_dims(
            [3, None, 5, 2, 6], device=device, dtype=torch.float32, layout=torch.jagged)
        flattened = nt.flatten(-3, -2)
        self.assertEqual(flattened.shape, nt.view(3, -1, 10, 6).shape)

    def test_chunk(self, device):
        # normal case
        D = 30
        B = 8
        nt = random_nt_from_dims([B, None, D], device=device, dtype=torch.float32, layout=torch.jagged)
        NUM_CHUNKS = 3
        chunks = nt.chunk(NUM_CHUNKS, dim=-1)
        self.assertEqual(len(chunks), NUM_CHUNKS)
        for i in range(NUM_CHUNKS):
            self.assertEqual(chunks[i].shape[-1], D // NUM_CHUNKS)

        # chunk on batch dim
        chunks = nt.chunk(NUM_CHUNKS, dim=0)
        self.assertEqual(len(chunks), NUM_CHUNKS)
        chunk_size = math.ceil(B / NUM_CHUNKS)
        for i in range(NUM_CHUNKS):
            if i < NUM_CHUNKS - 1:
                self.assertEqual(chunks[i].shape[0], chunk_size)
            else:
                self.assertEqual(chunks[i].shape[0], B - chunk_size * (NUM_CHUNKS - 1))
            offsets_expected = nt._offsets[i * chunk_size + 1 : (i + 1) * chunk_size + 1] - nt._offsets[i * chunk_size]
            self.assertEqual(chunks[i]._offsets[1:], offsets_expected)
        self.assertEqual(nt._values, torch.cat([x._values for x in chunks], dim=0))

        # chunk on ragged dim not supported
        with self.assertRaisesRegex(RuntimeError, "chunk.* not supported for NestedTensor on dim=1"):
            nt.chunk(2, dim=1)

    def test_squeeze(self, device):
        B = 4
        D = 6
        # squeeze middle dim
        nt = random_nt_from_dims(
            [B, None, 1, D], device=device, dtype=torch.float32, layout=torch.jagged)
        j0 = nt.shape[1]

        for dim_arg in [-2, 2]:
            out = nt.squeeze(dim_arg)
            self.assertEqual(out.shape, (B, j0, D))
            self.assertEqual(out.unsqueeze(-2), nt)

        # squeeze last dim
        nt = random_nt_from_dims(
            [B, None, 1], device=device, dtype=torch.float32, layout=torch.jagged)
        j1 = nt.shape[1]

        for dim_arg in [-1, 2]:
            out = nt.squeeze(dim_arg)
            self.assertEqual(out.shape, (B, j1))
            self.assertEqual(out.unsqueeze(-1), nt)

        # squeeze on batch dim not supported
        with self.assertRaisesRegex(
                RuntimeError, "squeeze.* not supported for NestedTensor on dim=0"):
            nt.squeeze(0)

        # squeeze on ragged dim not supported
        with self.assertRaisesRegex(
                RuntimeError, "squeeze.* not supported for NestedTensor on dim=1"):
            nt.squeeze(1)

    def test_binary_pointwise_broadcasting(self, device):
        # (B, j0, 3, 4)
        ts = self._get_list_for_jagged_tensor(((2, 3, 4), 3, 4), device, requires_grad=True)
        # (B, j0, ?, ?) + (?) -> (B, j0, ?, ?)
        # (B, j0, ?, ?) + (?, ?) -> (B, j0, ?, ?)
        # (B, j0, ?, ?) + (1, ?, ?) -> (B, j0, ?, ?)
        # Unsupported: (B, j0, ?, ?) + (1, 1, 1, ?, ?) -> (1, B, j0, ?, ?)
        t_sizes = (
            (4,),
            (1, 4),
            (3, 1),
            (1, 3, 1),
            (1, 1, 1, 4),
            # (1, 1, 1, 1, 4), (unsupported today)
        )

        def grad_test_func(t, *ts):
            nt = torch.nested.as_nested_tensor(list(ts), layout=torch.jagged)
            out = nt + t
            return out.values()

        for t_size in t_sizes:
            t = torch.rand(t_size, requires_grad=True, device=device, dtype=torch.float64)
            gradcheck(grad_test_func, inputs=(t, *ts), check_batched_grad=False)

    def test_threshold_backward(self, device):
        ts1 = self._get_list_for_jagged_tensor(((2, 3, 4), 16), device=device, requires_grad=False)
        ts2 = self._get_list_for_jagged_tensor(((2, 3, 4), 16), device=device, requires_grad=False)

        nt1, offsets = jagged_from_list(ts1, None)
        nt2, offsets = jagged_from_list(ts2, offsets)
        buf1 = nt1.values().detach().clone()
        buf2 = nt2.values().detach().clone()

        res_nt = torch.ops.aten.threshold_backward(nt1, nt2, 0.0)
        res_dense = torch.ops.aten.threshold_backward(buf1, buf2, 0.0)

        self.assertEqual(res_dense, res_nt.values())


    @parametrize("keepdim", [False, True])
    def test_sum_int_DimList(self, device, keepdim):
        # (B, j0, 3, 4)
        ts = self._get_list_for_jagged_tensor(((2, 3, 4), 3, 4), device=device, requires_grad=True)

        # Check shape correctness
        reduce_dims = (
            # dims, expected shape, expected keepdim shape
            # j0 is represented as None
            ((0, 1), (3, 4), (1, 1, 3, 4)),
            ((1, 2), None, None),
            ((2, 3), (3, None), (3, None, 1, 1)),
            ((0, 1, 3), (3,), (1, 1, 3, 1)),
            ((0, 1, 2), (4,), (1, 1, 1, 4)),
            ((0, 1, 2, 3), tuple(), (1, 1, 1, 1)),
        )
        for rd, ref_shape_no_keepdim, ref_shape_keepdim in reduce_dims:
            if (0 in rd) ^ (1 in rd):
                with self.assertRaisesRegex(
                        RuntimeError,
                        "applying over the ragged dimension, but not the batch dimension"):
                    nt = torch.nested.as_nested_tensor(ts, layout=torch.jagged)
                    out = torch.sum(nt, dim=rd, keepdim=keepdim)
                continue

            nt = torch.nested.as_nested_tensor(ts, layout=torch.jagged)
            out = torch.sum(nt, dim=rd, keepdim=keepdim)
            ref_shape = ref_shape_keepdim if keepdim else ref_shape_no_keepdim
            self.assertEqual(len(out.shape), len(ref_shape))
            for o, r in zip(out.shape, ref_shape):
                if r is not None:
                    self.assertEqual(o, r)
                else:
                    self.assertTrue(isinstance(o, torch.SymInt))

        # Check values correctness
        # raggedness not reduced
        nt = torch.nested.as_nested_tensor(ts, layout=torch.jagged)
        out = torch.sum(nt, dim=(2, 3), keepdim=keepdim)
        out_ref = torch.sum(nt.values(), dim=(1, 2))
        self.assertIsInstance(out, NestedTensor)
        # flatten to avoid having to replicate unsqueeze logic depending on keepdim
        self.assertTrue(torch.allclose(out.values().view(-1), out_ref.view(-1)))

        # raggedness reduced away
        nt = torch.nested.as_nested_tensor(ts, layout=torch.jagged)
        out = torch.sum(nt, dim=(0, 1), keepdim=keepdim)
        out_ref = torch.sum(nt.values(), dim=(0,))
        self.assertNotIsInstance(out, NestedTensor)
        self.assertTrue(torch.allclose(out, out_ref))



    @dtypes(torch.float, torch.double, torch.half)
    @parametrize("requires_grad", [False, True])
    @parametrize("weights_only", [False, True])
    def test_serialization(self, device, dtype, requires_grad, weights_only):

        def compare_metadata(nt1, nt2):
            self.assertEqual(nt1._nested_tensor_size(), nt2._nested_tensor_size())
            self.assertEqual(nt1._nested_tensor_strides(), nt2._nested_tensor_strides())
            self.assertEqual(nt1._nested_tensor_storage_offsets(),
                             nt2._nested_tensor_storage_offsets())

        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7))
        for a in [nt_contiguous, nt_noncontiguous]:
            buffer = io.BytesIO()
            serialized = torch.save(a, buffer)
            buffer.seek(0)
            b = torch.load(buffer, weights_only=weights_only)
            # should be both conceptually equal and metadata equivalent
            self.assertEqual(a, b)
            compare_metadata(a, b)
            # should be conceptually equal but not necessarily metadata equivalent
            self.assertEqual(b, nt_contiguous)
            self.assertEqual(b, nt_noncontiguous)

    @unittest.skipIf(PYTORCH_CUDA_MEMCHECK, "is_pinned uses failure to detect pointer property")
    @onlyCUDA
    def test_pin_memory(self, device):
        nt_contiguous, nt_noncontiguous = random_nt_noncontiguous_pair((2, 3, 6, 7))
        for nt in [nt_contiguous, nt_noncontiguous]:
            self.assertFalse(nt.is_pinned())
            pinned = nt.pin_memory(device)
            self.assertTrue(pinned.is_pinned())
            self.assertEqual(nt, pinned)
            self.assertNotEqual(nt.data_ptr(), pinned.data_ptr())
            # test that pin_memory on already pinned tensor has no effect
            self.assertIs(pinned, pinned.pin_memory())
            self.assertEqual(pinned.data_ptr(), pinned.pin_memory().data_ptr())

    @torch.compiler.disable
    def _validate_nt(self, nt, device, dtype, layout, requires_grad, dim, batch_size, base=None):
        # Validate a bunch of properties after NT construction.
        device = torch.device(device)
        self.assertEqual(nt.dim(), dim)
        self.assertEqual(nt.device, device)
        self.assertEqual(nt.dtype, dtype)
        self.assertEqual(nt.layout, layout)
        self.assertEqual(nt.requires_grad, requires_grad)

        if layout == torch.jagged:
            self.assertEqual(nt._values.device, device)
            self.assertEqual(nt._offsets.device, device)
            self.assertEqual(nt.shape[0], batch_size)
            self.assertTrue(isinstance(nt.shape[1], torch.SymInt))

        if base is not None:
            self.assertTrue(nt._is_view() and nt._base is base)

    @dtypes(torch.float, torch.double, torch.half)
    @parametrize("requires_grad", [False, True])
    @parametrize("components_require_grad", [False, True])
    def test_jagged_layout_construction_nested_tensor(
            self, device, dtype, requires_grad, components_require_grad):
        for tensor_list in self._get_example_tensor_lists(
                include_list_of_lists=True, include_requires_grad=components_require_grad):
            nt = torch.nested.nested_tensor(
                tensor_list,
                device=device,
                dtype=dtype,
                layout=torch.jagged,
                requires_grad=requires_grad)

            expected_dim = torch.as_tensor(tensor_list[0]).dim() + 1
            expected_batch_size = len(tensor_list)
            self._validate_nt(
                nt, device, dtype, torch.jagged, requires_grad, expected_dim, expected_batch_size)

            # Make sure grads -don't- flow back into original tensors for nested_tensor()
            if requires_grad:
                (nt * 2).backward(torch.ones_like(nt))
            for t in tensor_list:
                t = t if isinstance(t, torch.Tensor) else torch.as_tensor(t)
                self.assertTrue(t.grad is None)

    @dtypes(torch.float, torch.double, torch.half)
    @parametrize("components_require_grad", [False, True])
    def test_jagged_layout_construction_as_nested_tensor(
            self, device, dtype, components_require_grad):
        # NB: as_nested_tensor(tensor_list) doesn't support lists of lists for tensor_list
        for tensor_list in self._get_example_tensor_lists(
                include_list_of_lists=False, include_requires_grad=components_require_grad):
            nt = torch.nested.as_nested_tensor(
                tensor_list,
                device=device,
                dtype=dtype,
                layout=torch.jagged)

            # nt.requires_grad=True should be set if at least one component requires grad
            expected_dim = tensor_list[0].dim() + 1
            expected_batch_size = len(tensor_list)
            self._validate_nt(
                nt,
                device,
                dtype,
                torch.jagged,
                components_require_grad,
                expected_dim,
                expected_batch_size)

            # Make sure grads flow back into original tensors for as_nested_tensor()
            if components_require_grad:
                (nt * 2).backward(torch.ones_like(nt))
                for t in tensor_list:
                    if t.requires_grad:
                        self.assertEqual(t.grad, torch.ones_like(t) * 2)
                    else:
                        self.assertTrue(t.grad is None)

    @xfailIfTorchDynamo
    @unittest.skipIf(PYTORCH_CUDA_MEMCHECK, "is_pinned uses failure to detect pointer property")
    @onlyCUDA
    def test_jagged_layout_construction_with_pinned_memory(self, device):
        for tensor_list in self._get_example_tensor_lists():
            nt = torch.nested.nested_tensor(
                tensor_list,
                layout=torch.jagged,
                device="cpu",
                pin_memory=True)

            expected_dim = torch.as_tensor(tensor_list[0]).dim() + 1
            expected_batch_size = len(tensor_list)
            self._validate_nt(
                nt,
                device="cpu",
                dtype=torch.float32,
                layout=torch.jagged,
                requires_grad=False,
                dim=expected_dim,
                batch_size=expected_batch_size)
            self.assertTrue(nt.is_pinned())

    @dtypes(torch.float, torch.double, torch.half)
    @parametrize("requires_grad", [False, True])
    @parametrize("values_is_view", [False, True])
    def test_jagged_view_from_values_offsets(self, device, dtype, requires_grad, values_is_view):
        if values_is_view:
            # make values a view of base
            base = torch.randn(
                2, 3, 4, 5, 6, device=device, dtype=dtype, requires_grad=requires_grad)
            values = base.flatten(0, -2)
        else:
            values = torch.randn(10, 5, device=device, dtype=dtype, requires_grad=requires_grad)
        offsets = torch.tensor([0, 2, 4, 6, 10], device=device, dtype=torch.int64)

        nt = nested_view_from_values_offsets(values, offsets)

        expected_dim = values.dim() + 1
        expected_batch_size = offsets.shape[0] - 1
        expected_base = base if values_is_view else values
        self._validate_nt(
            nt, device, dtype, torch.jagged, requires_grad, expected_dim, expected_batch_size,
            # ensure NT is a proper view
            base=expected_base
        )

        if requires_grad:
            # Make sure grads flow back
            (nt * 2).backward(torch.ones_like(nt))

            @torch.compiler.disable
            def _check_grad(t):
                self.assertTrue(t.grad is not None)
                self.assertEqual(t.grad, torch.ones_like(t) * 2)

            _check_grad(base if values_is_view else values)

    @dtypes(torch.double, torch.half)
    @onlyCUDA
    def test_device_dtype_transfer_maintains_offsets(self, device, dtype):
        for tensor_list in self._get_example_tensor_lists():
            orig_device = torch.device("cpu")
            orig_dtype = torch.float32
            nt = torch.nested.nested_tensor(
                tensor_list,
                layout=torch.jagged,
                device=orig_device,
                dtype=orig_dtype)

            self.assertEqual(torch.int64, nt.offsets().dtype)
            nt = nt.to(device=device).to(dtype=dtype)

            # offsets should still be int64 on the original device
            self.assertEqual(orig_device, nt.offsets().device)
            self.assertEqual(torch.int64, nt.offsets().dtype)

    def test_unbind(self, device):
        for tensor_list in self._get_example_tensor_lists():
            nt = torch.nested.nested_tensor(
                tensor_list,
                layout=torch.jagged,
                device=device)
            out = nt.unbind()
            self.assertEqual(len(out), len(tensor_list))
            for i, t in enumerate(out):
                self.assertEqual(t, tensor_list[i])

    @xfailIfTorchDynamo
    def test_layer_norm_2(self, device):
        test_tensor_list = self._get_list_for_jagged_tensor(
            ((2, 3, 4), 3), device=device, requires_grad=True
        )
        bias = torch.randn(3, requires_grad=False, dtype=torch.float64, device=device)

        def grad_test_func(a, b, c, bias):
            nt = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)
            out = torch.nn.functional.layer_norm(nt, (nt.shape[-1],), bias=bias)
            return out.values()

        gradcheck(
            grad_test_func, inputs=(*test_tensor_list, bias), check_batched_grad=False
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"layer_norm\(\): normalizing over ragged dim not supported for nested tensors",
        ):
            nt = torch.nested.as_nested_tensor(test_tensor_list, layout=torch.jagged)
            _ = torch.nn.functional.layer_norm(nt, (nt.shape[-2], nt.shape[-1]))

    def test_narrow(self, device):
        starts = torch.tensor([0, 1, 2, 3, 4], device=device, dtype=torch.int64)
        lengths = torch.tensor([3, 2, 2, 1, 5], device=device, dtype=torch.int64)
        buffer = (
            torch.arange(0, 10, device=device, dtype=torch.int64)
            .unsqueeze(0).expand(5, -1).clone().detach()
        )
        nt = torch.nested.narrow(
            buffer,
            1,
            starts,
            lengths,
            layout=torch.jagged
        )

        self.assertTrue(nt._is_view() and nt._base is buffer)

        # TODO: Use this approach when unbind is functional
        # unbinded_nt = nt.unbind()
        # for i in range(starts.shape[0]):
        #     self.assertEqual(torch.arange(starts[i], starts[i] + lengths[i], device=device, dtype=torch.int64), unbinded_nt[i])
        for i in range(starts.shape[0]):
            self.assertEqual(
                torch.arange(starts[i], starts[i] + lengths[i], device=device, dtype=torch.int64),
                nt.values()[nt.offsets()[i]:(nt.offsets()[i] + nt.lengths()[i])]
            )

    def test_is_contiguous(self, device):
        a = torch.randn(2, 3, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, requires_grad=True, dtype=torch.float64, device=device)
        nt_contiguous = torch.nested.as_nested_tensor([a, b, c], layout=torch.jagged)

        starts_nc = torch.tensor([0, 1, 2, 3, 4], device=device, dtype=torch.int64)
        lengths_nc = torch.tensor([3, 2, 2, 1, 5], device=device, dtype=torch.int64)
        narrow_base = torch.arange(0, 10, device=device, dtype=torch.int64).unsqueeze(0).expand(5, -1).clone()
        nt_noncontiguous = torch.nested.narrow(
            narrow_base,
            1,
            starts_nc,
            lengths_nc,
            layout=torch.jagged
        )

        starts_c = torch.tensor([1, 0, 0, 0, 0], device=device, dtype=torch.int64)
        lengths_c = torch.tensor([9, 10, 10, 10, 8], device=device, dtype=torch.int64)
        nt_contiguous_narrow = torch.nested.narrow(
            narrow_base,
            1,
            starts_c,
            lengths_c,
            layout=torch.jagged
        )

        # Test contiguous case
        assert nt_contiguous.is_contiguous()

        # Test narrow case
        assert not nt_noncontiguous.is_contiguous()
        assert nt_contiguous_narrow.is_contiguous()

        # Test querying by memory_format
        self.assertTrue(nt_contiguous.is_contiguous(memory_format=torch.contiguous_format))
        self.assertTrue(not nt_noncontiguous.is_contiguous(memory_format=torch.contiguous_format))
        self.assertTrue(nt_contiguous_narrow.is_contiguous(memory_format=torch.contiguous_format))

    @skipIfTorchDynamo("Not a suitable test for TorchDynamo")
    @parametrize("func", [torch.empty_like, torch.randn_like],
                 name_fn=lambda f: f.__name__)
    def test_like_shape(self, func):
        nt = random_nt_from_dims([2, None, 3], torch.device('cpu'), torch.float32, layout=torch.jagged)
        nt_like = func(nt)

        for nt_ub in nt_like.unbind():
            t_like = func(nt_ub)
            self.assertEqual(nt_ub.shape, t_like.shape)

    @skipIfTorchDynamo("Not a suitable test for TorchDynamo")
    @parametrize("func", [torch.ones_like, torch.zeros_like],
                 name_fn=lambda f: f.__name__)
    def test_like_value(self, func):
        nt = random_nt_from_dims([2, None, 3], torch.device('cpu'), torch.float32, layout=torch.jagged)
        nt_like = func(nt)

        for nt_ub in nt_like.unbind():
            t_like = func(nt_ub)
            self.assertEqual(nt_ub, t_like)

    def test_noncontiguous_pointwise(self, device):
        a = torch.randn(2, 3, 4, requires_grad=True, dtype=torch.float64, device=device)
        b = torch.randn(3, 3, 4, requires_grad=True, dtype=torch.float64, device=device)
        c = torch.randn(4, 3, 4, requires_grad=True, dtype=torch.float64, device=device)
        nt = torch.nested.nested_tensor([a, b, c], layout=torch.jagged)
        # transpose ragged dim
        transposed = nt.transpose(1, 2)
        self.assertFalse(transposed.is_contiguous())
        clone = transposed.clone()

        def check_nt_equality(x, y):
            self.assertEqual(x.values(), y.values())
            self.assertEqual(x.offsets(), y.offsets())
            self.assertEqual(x._ragged_idx, y._ragged_idx)
            self.assertEqual(x.shape, y.shape)

        self.assertFalse(clone.is_contiguous())
        check_nt_equality(clone, transposed)

        clone_contig = transposed.clone(memory_format=torch.contiguous_format)
        self.assertTrue(clone_contig.is_contiguous())
        check_nt_equality(clone_contig, transposed)

        detached = transposed.detach()
        self.assertFalse(clone.is_contiguous())
        check_nt_equality(detached, transposed)

    def test_to_copy(self, device):
        nt = torch.nested.nested_tensor(
            [torch.randn(i + 2, 3, 4, requires_grad=True, dtype=torch.float64, device=device)
             for i in range(3)], layout=torch.jagged
        )

        nt_copy_dtype = torch.ops.aten._to_copy(nt, dtype=torch.float16)
        self.assertEqual(torch.float16, nt_copy_dtype.dtype)

        nt_t = nt.transpose(1, 2)
        nt_t_copy_dtype = torch.ops.aten._to_copy(nt_t, dtype=torch.float16)
        self.assertEqual(torch.float16, nt_t_copy_dtype.dtype)

    def test_is_same_size(self, device):
        def get_3_tensors():
            return [torch.randn(i + 2, 3, 4, requires_grad=True, dtype=torch.float64, device=device) for i in range(3)]

        nt1, offsets1 = jagged_from_list(get_3_tensors(), None)
        nt2, offsets1 = jagged_from_list(get_3_tensors(), offsets1)

        nt3, offsets2 = jagged_from_list(get_3_tensors(), None)
        nt4, offsets2 = jagged_from_list(get_3_tensors(), offsets2)

        def check_size(nt1, nt2, nt3, nt4):
            self.assertTrue(torch.ops.aten.is_same_size(nt1, nt2))
            self.assertTrue(torch.ops.aten.is_same_size(nt3, nt4))
            self.assertFalse(torch.ops.aten.is_same_size(nt1, nt3))

        check_size(nt1, nt2, nt3, nt4)

        nt1_t, nt2_t, nt3_t, nt4_t = (x.transpose(1, 2) for x in (nt1, nt2, nt3, nt4))
        check_size(nt1_t, nt2_t, nt3_t, nt4_t)

    # Note 1: Math fallback doesn't work with bfloat16 on CUDA
    # Note 2: ROCm doesn't support flash attention or mem_efficient attention for NT
    @unittest.skipIf(
        TEST_WITH_ROCM,
        "ROCm doesn't support flash attention or mem_efficient attention for NT",
    )
    @parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32] if
                 SM80OrLater else [torch.float16, torch.float32])
    def test_sdpa(self, device, dtype):
        batch_size = 1
        emb_dims = 128
        n_heads = 8
        head_dims = emb_dims // n_heads

        sen1 = torch.randn(11, emb_dims, dtype=dtype, device=device)
        sen2 = torch.randn(13, emb_dims, dtype=dtype, device=device)

        query = torch.nn.Linear(emb_dims, emb_dims, bias=False, device=device, dtype=dtype)
        key = torch.nn.Linear(emb_dims, emb_dims, bias=False, device=device, dtype=dtype)
        value = torch.nn.Linear(emb_dims, emb_dims, bias=False, device=device, dtype=dtype)

        # Simplest case: 1 sentence, no batching
        x_d1 = sen1.unsqueeze(0)
        x_nt = torch.nested.as_nested_tensor([sen1], layout=torch.jagged)

        # See note below for why we detach here.
        q_d1 = query(x_d1).view(batch_size, -1, n_heads, head_dims).detach().requires_grad_(True)
        q_d1_t = q_d1.transpose(1, 2)
        k_d1 = key(x_d1).view(batch_size, -1, n_heads, head_dims).detach().requires_grad_(True)
        k_d1_t = k_d1.transpose(1, 2)
        v_d1 = value(x_d1).view(batch_size, -1, n_heads, head_dims).detach().requires_grad_(True)
        v_d1_t = v_d1.transpose(1, 2)

        q_nt = query(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().requires_grad_(True)
        q_nt_t = q_nt.transpose(1, 2)
        k_nt = key(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().requires_grad_(True)
        k_nt_t = k_nt.transpose(1, 2)
        v_nt = value(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().requires_grad_(True)
        v_nt_t = v_nt.transpose(1, 2)

        # High Precision Math Reference
        q_d1_f32 = q_d1.to(torch.float32)
        k_d1_f32 = k_d1.to(torch.float32)
        v_d1_f32 = v_d1.to(torch.float32)
        q_d1_f32_t = q_d1_f32.transpose(1, 2)
        k_d1_f32_t = k_d1_f32.transpose(1, 2)
        v_d1_f32_t = v_d1_f32.transpose(1, 2)
        out_ref = torch.ops.aten._scaled_dot_product_attention_math(q_d1_f32_t, k_d1_f32_t, v_d1_f32_t)[0]
        grads_ref = torch.autograd.grad(out_ref.sum(), (q_d1_f32, k_d1_f32, v_d1_f32))

        # Low Precision Math Reference
        out_lp_ref = torch.ops.aten._scaled_dot_product_attention_math(q_d1_t, k_d1_t, v_d1_t)[0]
        grads_lp_ref = torch.autograd.grad(out_lp_ref.sum(), (q_d1, k_d1, v_d1))

        # Compute tolerances
        output_ref_atol, output_ref_rtol = get_tolerances(out_ref, out_lp_ref)
        grad_q_ref_atol, grad_q_ref_rtol = get_tolerances(grads_ref[0], grads_lp_ref[0])
        grad_k_ref_atol, grad_k_ref_rtol = get_tolerances(grads_ref[1], grads_lp_ref[1])
        grad_v_ref_atol, grad_v_ref_rtol = get_tolerances(grads_ref[2], grads_lp_ref[2])
        grad_atols = [grad_q_ref_atol, grad_k_ref_atol, grad_v_ref_atol]
        grad_rtols = [grad_q_ref_rtol, grad_k_ref_rtol, grad_v_ref_rtol]

        attn_d1 = torch.nn.functional.scaled_dot_product_attention(q_d1_t, k_d1_t, v_d1_t).transpose(1, 2)
        attn_nt = torch.nn.functional.scaled_dot_product_attention(q_nt_t, k_nt_t, v_nt_t).transpose(1, 2)

        self.assertEqual(attn_d1, attn_nt.unbind()[0].unsqueeze(0), atol=output_ref_atol, rtol=output_ref_rtol)

        # Simple case: 2 sentences, no extra params
        x_d2 = sen2.unsqueeze(0)
        x_nt = torch.nested.as_nested_tensor([sen1, sen2], layout=torch.jagged)

        # NB: we make sure the leaf tensor we compute gradients for is the view-ed tensor before
        # it is transposed. This is because today we cannot backward through view or unbind a
        # transposed tensor.
        q_d2 = query(x_d2).view(batch_size, -1, n_heads, head_dims).detach().requires_grad_(True)
        q_d2_t = q_d2.transpose(1, 2)
        k_d2 = key(x_d2).view(batch_size, -1, n_heads, head_dims).detach().requires_grad_(True)
        k_d2_t = k_d2.transpose(1, 2)
        v_d2 = value(x_d2).view(batch_size, -1, n_heads, head_dims).detach().requires_grad_(True)
        v_d2_t = v_d2.transpose(1, 2)

        q_nt = query(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().requires_grad_(True)
        q_nt_t = q_nt.transpose(1, 2)
        k_nt = key(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().requires_grad_(True)
        k_nt_t = k_nt.transpose(1, 2)
        v_nt = value(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().requires_grad_(True)
        v_nt_t = v_nt.transpose(1, 2)

        attn_d2 = torch.nn.functional.scaled_dot_product_attention(q_d2_t, k_d2_t, v_d2_t).transpose(1, 2)
        d1_grads = torch.autograd.grad(attn_d1.sum(), (q_d1, k_d1, v_d1))
        d2_grads = torch.autograd.grad(attn_d2.sum(), (q_d2, k_d2, v_d2))

        def check_forward_backward():
            attn_nt = torch.nn.functional.scaled_dot_product_attention(q_nt_t, k_nt_t, v_nt_t).transpose(1, 2)

            attn_nts = attn_nt.unbind()
            self.assertEqual(attn_d1, attn_nts[0].unsqueeze(0), atol=output_ref_atol, rtol=output_ref_rtol)
            self.assertEqual(attn_d2, attn_nts[1].unsqueeze(0), atol=output_ref_atol, rtol=output_ref_rtol)

            nt_grads = torch.autograd.grad(attn_nt.values().sum(), (q_nt, k_nt, v_nt))
            for nt_grad, d1_grad, d2_grad, grad_atol, grad_rtol in zip(nt_grads, d1_grads, d2_grads, grad_atols, grad_rtols):
                unbound_nt_grads = nt_grad.unbind()
                self.assertEqual(d1_grad, unbound_nt_grads[0].unsqueeze(0), atol=grad_atol, rtol=grad_rtol)
                self.assertEqual(d2_grad, unbound_nt_grads[1].unsqueeze(0), atol=grad_atol, rtol=grad_rtol)

        # Default
        check_forward_backward()

        # Test dispatcher works by calling only mem-effn and math (as they are safe for all devices)
        with torch.backends.cuda.sdp_kernel(enable_flash=False, enable_mem_efficient=True, enable_math=True):
            check_forward_backward()

        # Test math fallback
        with torch.backends.cuda.sdp_kernel(enable_flash=False, enable_mem_efficient=False, enable_math=True):
            # Math fallback doesn't work with bfloat16 on CUDA because
            # "group_gemm_dispatch" not implemented for 'BFloat16'
            if not (str(device).startswith("cuda") and dtype == torch.bfloat16):
                check_forward_backward()

    @skipIfTorchDynamo("SDPA test compiles internally")
    @unittest.skipIf(IS_WINDOWS, reason="Windows not yet supported for torch.compile")
    @skipCUDAIf(not SM70OrLater, "GPU capability is < SM70")
    # Guarding with sqrt() doesn't work on ROCm?
    @skipCUDAIfRocm
    @onlyCUDA
    @dtypes(*([torch.float16, torch.bfloat16, torch.float32] if SM80OrLater
            else [torch.float16, torch.float32]))
    def test_sdpa_compile(self, device, dtype):
        batch_size = 1
        emb_dims = 1024
        n_heads = 8
        head_dims = emb_dims // n_heads

        sen1 = torch.randn(11, emb_dims, dtype=dtype, device=device)
        sen2 = torch.randn(13, emb_dims, dtype=dtype, device=device)

        query = torch.nn.Linear(emb_dims, emb_dims, bias=False, device=device, dtype=dtype)
        key = torch.nn.Linear(emb_dims, emb_dims, bias=False, device=device, dtype=dtype)
        value = torch.nn.Linear(emb_dims, emb_dims, bias=False, device=device, dtype=dtype)

        # Simplest case: 1 sentence, no batching
        x_d1 = sen1.unsqueeze(0)
        x_d2 = sen2.unsqueeze(0)
        x_nt = torch.nested.as_nested_tensor([sen1, sen2], layout=torch.jagged)

        q_d1 = query(x_d1).view(batch_size, -1, n_heads, head_dims).transpose(1, 2)
        k_d1 = key(x_d1).view(batch_size, -1, n_heads, head_dims).transpose(1, 2)
        v_d1 = value(x_d1).view(batch_size, -1, n_heads, head_dims).transpose(1, 2)
        q_d2 = query(x_d2).view(batch_size, -1, n_heads, head_dims).transpose(1, 2)
        k_d2 = key(x_d2).view(batch_size, -1, n_heads, head_dims).transpose(1, 2)
        v_d2 = value(x_d2).view(batch_size, -1, n_heads, head_dims).transpose(1, 2)

        q_nt = query(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().transpose(1, 2)
        k_nt = key(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().transpose(1, 2)
        v_nt = value(x_nt).view(*x_nt.size()[0:2], n_heads, head_dims).detach().transpose(1, 2)

        # High Precision Math Reference
        q_d1_f32 = q_d1.to(torch.float32)
        k_d1_f32 = k_d1.to(torch.float32)
        v_d1_f32 = v_d1.to(torch.float32)
        out_ref = torch.ops.aten._scaled_dot_product_attention_math(q_d1_f32, k_d1_f32, v_d1_f32)[0]
        # Low Precision Math Reference
        out_lp_ref = torch.ops.aten._scaled_dot_product_attention_math(q_d1, k_d1, v_d1)[0]
        output_ref_atol, output_ref_rtol = get_tolerances(out_ref, out_lp_ref)

        attn_d1 = torch.nn.functional.scaled_dot_product_attention(q_d1, k_d1, v_d1).transpose(1, 2)
        attn_d2 = torch.nn.functional.scaled_dot_product_attention(q_d2, k_d2, v_d2).transpose(1, 2)

        compiled_sdpa = torch.compile(torch.nn.functional.scaled_dot_product_attention)
        attn_nt = compiled_sdpa(q_nt, k_nt, v_nt).transpose(1, 2)

        attn_nts = attn_nt.unbind()
        self.assertEqual(attn_d1, attn_nts[0].unsqueeze(0), atol=output_ref_atol, rtol=output_ref_rtol)
        self.assertEqual(attn_d2, attn_nts[1].unsqueeze(0), atol=output_ref_atol, rtol=output_ref_rtol)

    @dtypes(torch.float32, torch.double, torch.half)
    def test_sdpa_with_constant_sequence_length(self, device, dtype):
        # shape (B, P*, S, D)
        # B: batch size
        # P*: ragged number of prompts
        # S: (constant) sequence length
        # D: embedding size
        query = random_nt_from_dims(
            [4, None, 8, 10], device=device, dtype=dtype, layout=torch.jagged)
        key = random_nt_from_similar(query)
        value = random_nt_from_similar(query)
        output = F.scaled_dot_product_attention(query, key, value)
        self.assertTrue(isinstance(output, NestedTensor))

        # should be equivalent to just running the buffers through
        output_dense = F.scaled_dot_product_attention(query._values, key._values, value._values)
        self.assertEqual(output._values, output_dense)

    @onlyCUDA
    @unittest.skipIf(
        not PLATFORM_SUPPORTS_FUSED_ATTENTION,
        "Platform doesn't support flash or mem-efficient attention"
    )
    @dtypes(*([torch.float16, torch.bfloat16, torch.float32] if SM80OrLater
            else [torch.float16, torch.float32]))
    def test_sdpa_with_packed_in_proj(self, device, dtype):
        # shape (B, *, D)
        input_packed = random_nt_from_dims(
            [5, None, 10], device=device, dtype=dtype, layout=torch.jagged)

        # Do input projection.
        num_heads = 2
        # should be multiple of 4 for efficient kernels (e.g. flash / mem-efficient)
        head_dim = 8
        qkv_linear = torch.nn.Linear(10, num_heads * head_dim * 3).to(device=device, dtype=dtype)

        def in_proj(input_packed, qkv_linear=qkv_linear):
            qkv_post_proj = qkv_linear(input_packed)
            # these are non-contiguous to trigger _is_safe_to_get_storage_as_tensor()
            q, k, v = qkv_post_proj.chunk(3, dim=-1)
            q = q.unflatten(-1, [num_heads, head_dim]).transpose(-2, -3)
            k = k.unflatten(-1, [num_heads, head_dim]).transpose(-2, -3)
            v = v.unflatten(-1, [num_heads, head_dim]).transpose(-2, -3)
            return q, k, v

        q, k, v = in_proj(input_packed)
        output = F.scaled_dot_product_attention(q, k, v, attn_mask=None)

        # compare to individually running unbound components through
        for in_component, out_component in zip(
            input_packed.unbind(),
            output.transpose(-2, -3).unbind()
        ):
            q, k, v = in_proj(in_component)
            out = F.scaled_dot_product_attention(q, k, v).transpose(-2, -3)

            # Low Precision Math Reference
            out_lp_ref = torch.ops.aten._scaled_dot_product_attention_math(
                q, k, v)[0].transpose(-2, -3)
            output_ref_atol, output_ref_rtol = get_tolerances(out, out_lp_ref)

            self.assertEqual(out, out_component, atol=output_ref_atol, rtol=output_ref_rtol)


instantiate_parametrized_tests(TestNestedTensor)
instantiate_device_type_tests(TestNestedTensorDeviceType, globals())
instantiate_device_type_tests(TestNestedTensorAutograd, globals())
instantiate_device_type_tests(TestNestedTensorSubclass, globals())

if __name__ == '__main__':
    common.xpu_test_base.customized_skipper()
    run_tests()
