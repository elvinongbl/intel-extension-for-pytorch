# Owner(s): ["module: inductor"]
import importlib
import os
import sys
import unittest

import torch
import intel_extension_for_pytorch
from torch._inductor.compile_fx import compile_fx
from torch._inductor.test_case import TestCase
from torch.testing._internal.common_utils import (
    IS_CI,
    IS_WINDOWS,
    TEST_WITH_ASAN,
    TEST_WITH_ROCM,
)
from torch.testing._internal.inductor_utils import (
    _check_has_dynamic_shape,
    GPU_TYPE,
    HAS_CPU,
    HAS_GPU,
    HAS_XPU
)

HAS_GPU = HAS_XPU

if IS_WINDOWS and IS_CI:
    sys.stderr.write(
        "Windows CI does not have necessary dependencies for test_torchinductor_codegen_dynamic_shapes yet\n"
    )
    if __name__ == "__main__":
        sys.exit(0)
    raise unittest.SkipTest("requires sympy/functorch/filelock")

importlib.import_module("filelock")

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)
from inductor.test_torchinductor import (
    CommonTemplate,
    copy_tests,
    run_and_get_cpp_code,
    run_and_get_triton_code,
    TestFailure,
)
from inductor.test_torchinductor_dynamic_shapes import make_dynamic_cls


# Checks for patterns in generated C++/Triton code to see if it's dynamic
def check_codegen(
    self: TestCase,
    model,
    example_inputs,
    kwargs=None,
    *,
    is_cpp_code: bool,
):
    kwargs = kwargs or {}

    if is_cpp_code is False:
        if hasattr(model, "to"):
            model = model.to(device=GPU_TYPE)

        def copy_fn(x):
            # preserve strides of the input on the device
            if not isinstance(x, torch.Tensor):
                return x
            return torch.empty_strided(
                x.size(), x.stride(), device=GPU_TYPE, dtype=x.dtype
            ).copy_(x)

        example_inputs = tuple(copy_fn(x) for x in example_inputs)

    torch._dynamo.reset()
    torch._inductor.codecache.FxGraphCache.clear()
    torch._inductor.metrics.reset()

    called = False

    def compile_fx_wrapper(model_, example_inputs_):
        nonlocal called
        called = True
        return compile_fx(model_, example_inputs_)

    def run(*ex, **kwargs):
        return model(*ex, **kwargs)

    run = torch._dynamo.optimize(compile_fx_wrapper, nopython=True)(run)

    if is_cpp_code:
        _, code = run_and_get_cpp_code(run, *example_inputs, **kwargs)
        _check_has_dynamic_shape(self, code)
    else:
        code = run_and_get_triton_code(run, *example_inputs, **kwargs)
        triton_kernel_found = False
        lines = code.split("\n")
        for line in lines:
            if "def triton" in line:
                triton_kernel_found = True
                continue
        self.assertTrue(triton_kernel_found, f"Failed to find triton kernel\n{code}")

    assert called, "Ran graph without calling compile_fx"

    torch._dynamo.reset()


# xfail by default, set is_skip=True to skip
test_failures = {
    #
    # Failed to find dynamic for loop variable (no kernels generated)
    #
    "test_fft_real_input_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_fft_real_input_real_output_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_to_device_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    #
    # Failed to find dynamic for loop variable:
    #
    "test_arange1_dynamic_shapes": TestFailure(("cpu",)),
    "test_arange2_dynamic_shapes": TestFailure(("cpu",)),
    "test_arange3_dynamic_shapes": TestFailure(("cpu",)),
    "test_arange4_dynamic_shapes": TestFailure(("cpu",)),
    "test_arange6_dynamic_shapes": TestFailure(("cpu",)),
    "test_clamp_type_promotion_dynamic_shapes": TestFailure(("cpu",)),
    "test_conv2d_channels_last_dynamic_shapes": TestFailure(("cpu",)),
    "test_conv3d_channels_last_dynamic_shapes": TestFailure(("cpu",)),
    "test_expand_dynamic_shapes": TestFailure(("cpu",)),
    "test_glu_dynamic_shapes": TestFailure(("cpu",)),
    "test_isinf2_dynamic_shapes": TestFailure(("cpu",)),
    "test_linspace1_dynamic_shapes": TestFailure(("cpu",)),
    "test_masked_scatter_dynamic_shapes": TestFailure(("cpu",)),
    "test_stack_dynamic_shapes": TestFailure(("cpu",)),
    "test_tensor2_dynamic_shapes": TestFailure(("cpu",)),
    "test_tensor3_dynamic_shapes": TestFailure(("cpu",)),
    "test_to_device_constant_dynamic_shapes": TestFailure("cpu"),
    "test_upsample_nearest2d_backward_dynamic_shapes": TestFailure(("cpu",)),
    "test_views3_dynamic_shapes": TestFailure(("cpu",)),
    "test_views4_dynamic_shapes": TestFailure(("cpu",)),
    "test_zeros_dynamic_shapes": TestFailure(("cpu",)),
    "test_uint_dynamic_shapes": TestFailure(("cpu",)),
    "test_issue102546_dynamic_shapes": TestFailure(("cpu",)),
    #
    # Failed to find for loop/triton kernel:
    #
    "test_complex_fallback_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_adaptive_avg_pool2d2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_adaptive_max_pool2d2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_fractional_max_pool2d2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_argmax_to_float_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_avg_pool2d7_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_avg_pool2d_backward4_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_baddbmm_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_bmm2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_both_scalars_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_compar_dynamic_shapes": TestFailure(("cpu",)),
    "test_const_int32_to_float_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_conv2d_backward_channels_last_dynamic_shapes": TestFailure(("cpu",)),
    "test_conv_backward_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_conv_functional_bn_fuse_dynamic_shapes": TestFailure(("cpu",), is_skip=True),
    "test_convolution2_dynamic_shapes": TestFailure(("cpu",)),
    "test_cumprod_zero_dim_dynamic_shapes": TestFailure(("cpu",)),
    "test_cumsum_dynamic_shapes": TestFailure(("cpu",)),
    "test_cumsum_no_mask_dynamic_shapes": TestFailure(("cpu",)),
    "test_cumsum_zero_dim_dynamic_shapes": TestFailure(("cpu",)),
    "test_div8_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_embedding_bag_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_empty1_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_empty2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_empty_strided_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_bucketize_dynamic_shapes": TestFailure("cpu"),
    "test_bucketize_default_kwargs_dynamic_shapes": TestFailure("cpu"),
    "test_bucketize_int_dynamic_shapes": TestFailure("cpu"),
    "test_like_rands_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_linspace2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_linspace3_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_logcumsumexp_dynamic_shapes": TestFailure(("cpu",)),
    "test_logcumsumexp_zero_dim_dynamic_shapes": TestFailure(("cpu",)),
    "test_max_pool2d6_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_max_pool2d8_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_max_pool2d_with_indices_backward5_dynamic_shapes": TestFailure(
        ("cpu", "xpu")
    ),
    "test_max_pool2d_with_indices_backward6_dynamic_shapes": TestFailure(
        ("cpu", "xpu")
    ),
    "test_misaligned_address_issue1_dynamic_shapes": TestFailure(("cpu",)),
    "test_mm_views_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_new_empty_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_new_empty_strided_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_new_ones_dynamic_shapes": TestFailure(("cpu",)),
    "test_permute2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_pointwise_airy_ai_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_digamma_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_gammainc_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_gammaincc_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_i0e_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_i1e_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_modified_bessel_k0_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_modified_bessel_k1_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_ndtri_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_polygamma_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_psi_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_scaled_modified_bessel_k0_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_scaled_modified_bessel_k1_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_spherical_bessel_j0_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_zeta_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_chebyshev_polynomial_t_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_chebyshev_polynomial_u_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_chebyshev_polynomial_v_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_chebyshev_polynomial_w_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_shifted_chebyshev_polynomial_t_dynamic_shapes": TestFailure(
        ("xpu",)
    ),
    "test_pointwise_shifted_chebyshev_polynomial_u_dynamic_shapes": TestFailure(
        ("xpu",)
    ),
    "test_pointwise_shifted_chebyshev_polynomial_v_dynamic_shapes": TestFailure(
        ("xpu",)
    ),
    "test_pointwise_shifted_chebyshev_polynomial_w_dynamic_shapes": TestFailure(
        ("xpu",)
    ),
    "test_pointwise_hermite_polynomial_h_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_hermite_polynomial_he_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_laguerre_polynomial_l_dynamic_shapes": TestFailure(("xpu",)),
    "test_pointwise_legendre_polynomial_p_dynamic_shapes": TestFailure(("xpu",)),
    "test_randn_generator_dynamic_shapes": TestFailure(("cpu",)),
    "test_randn_like_empty_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_single_elem_dynamic_shapes": TestFailure(("cpu",)),
    "test_single_elem_indirect_dynamic_shapes": TestFailure(("cpu",)),
    "test_sort_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_split_cumsum_dynamic_shapes": TestFailure(("cpu",)),
    "test_split_cumsum_low_prec_dynamic_shapes": TestFailure(("cpu",)),
    "test_split_cumprod_dynamic_shapes": TestFailure(("cpu",)),
    "test_split_cumprod_low_prec_dynamic_shapes": TestFailure(("cpu",)),
    "test_split_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_topk_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_unbind_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_views5_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_view_detach_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_view_on_aliased_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_linear_float64_dynamic_shapes": TestFailure("cpu"),
    "test_adaptive_avg_pool_with_output_size_0_dynamic_shapes": TestFailure(
        ("cpu", "xpu")
    ),
    "test_zero_element_mutation_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_custom_op_fixed_layout_sequential_dynamic_shapes": TestFailure(
        ("cpu", "xpu")
    ),
    "test_cat_uint8_dynamic_shapes": TestFailure(
        ("cpu",)
    ),  # cat on uint8 input is using aten fallback on cpu
    #
    # Tests not using 'common' or directly calling 'assertEqual':
    #
    "test_arange5_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_cat_inplace_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_cat_of_loops_and_extern_kernel_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    # need to enable CL with dynamic shapes
    "test_conv_inference_heuristics_dynamic_shapes": TestFailure("xpu"),
    "test_scaled_dot_product_efficient_attention_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_dropout_deterministic_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_dropout_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_dtype_mismatch_issue_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_forced_buffer_realize_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_tmp_not_defined_issue3_dynamic_shapes": TestFailure(("cpu",), is_skip=True),
    "test_gather2_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_inplace_add_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_inplace_mixed_dtype_ops_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_input_mutation1_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_input_mutation2_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_input_mutation3_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_input_mutation4_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_kernel_names_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_lerp_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_linear_buffer_reuse_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_list_clearing_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_dropout2_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_dropout3_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_masked_fill_promotion_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_min_max_reduction_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_multi_gpu_recompile_on_index_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_output_strides_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_pow3_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_profiler_mark_wrapper_call_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_rand_like_deterministic_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_repeat_interleave_2_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_slice_mutation2_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_strided_inputs_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_transposed_propagates_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_require_stride_expanded_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_unspec_inputs_dynamic_shapes": TestFailure(("cpu", "xpu"), is_skip=True),
    "test_zero_dim_reductions_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
    "test_sdpa_dynamic_shapes": TestFailure(("cpu",), is_skip=True),
    "test_sdpa_unaligned_mask_dynamic_shapes": TestFailure(("cpu",), is_skip=True),
    #
    # The following tests do not support dynamic shapes yet:
    #
    "test_cudnn_rnn_dynamic_shapes": TestFailure(("xpu",)),
    "test_kwargs_dynamic_shapes": TestFailure(("cpu",)),
    # test_roi_align uses torchvision, which doesn't work with dynamic shapes
    "test_roi_align_dynamic_shapes": TestFailure(("cpu", "xpu")),
    "test_aliased_buffer_reuse_dynamic_shapes": TestFailure(("cpu",)),
    # The input of this case has only 1 elements
    "test_mutations_loop_fusion_dynamic_shapes": TestFailure(
        ("cpu", "xpu"), is_skip=True
    ),
}

if TEST_WITH_ROCM:
    test_failures.update(
        {
            "test_split_cumsum_dynamic_shapes": TestFailure(("cpu", "xpu")),
            "test_split_cumsum_low_prec_dynamic_shapes": TestFailure(("cpu", "xpu")),
            "test_split_cumprod_dynamic_shapes": TestFailure(("cpu", "xpu")),
            "test_split_cumprod_low_prec_dynamic_shapes": TestFailure(("cpu", "xpu")),
        }
    )

DynamicShapesCodegenCommonTemplate = make_dynamic_cls(
    CommonTemplate, xfail_prop="_expected_failure_codegen_dynamic"
)


if HAS_CPU:

    class DynamicShapesCodegenCpuTests(TestCase):
        maxDiff = None
        device = "cpu"

        def common(self: TestCase, model, example_inputs, kwargs=None, **_rest):
            return check_codegen(
                self=self,
                model=model,
                example_inputs=example_inputs,
                kwargs=kwargs,
                is_cpp_code=True,
            )

    copy_tests(
        DynamicShapesCodegenCommonTemplate,
        DynamicShapesCodegenCpuTests,
        "cpu",
        test_failures,
    )


if HAS_GPU and not TEST_WITH_ASAN:

    class DynamicShapesCodegenGPUTests(TestCase):
        maxDiff = None
        device = GPU_TYPE

        def common(self: TestCase, model, example_inputs, kwargs=None, **_rest):
            return check_codegen(
                self=self,
                model=model,
                example_inputs=example_inputs,
                kwargs=kwargs,
                is_cpp_code=False,
            )

    copy_tests(
        DynamicShapesCodegenCommonTemplate,
        DynamicShapesCodegenGPUTests,
        GPU_TYPE,
        test_failures,
    )


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if HAS_CPU or HAS_GPU:
        run_tests(needs="filelock")
