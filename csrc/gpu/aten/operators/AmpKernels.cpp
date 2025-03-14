#include <ATen/ATen.h>
#include <core/Memory.h>
#include <runtime/Utils.h>
#include "comm/ATDispatch.h"
#include "comm/Numerics.h"

#include "ForeachFunctors.h"
#include "Loops.h"

using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {

template <typename scalar_t, typename opmath_t>
struct _amp_non_finite_check_and_unscale_dpcpp_functor {
  scalar_t operator()(scalar_t val_in) const {
    auto val = static_cast<opmath_t>(val_in);
    if (Numerics<opmath_t>::isinf(val) || Numerics<opmath_t>::isnan(val)) {
      *found_inf_ptr = 1.f;
    }
    // Every thread accesses inv_scale, but it will hit in cache.
    const auto inv_scale_val = *inv_scale_ptr;
    return static_cast<scalar_t>(
        inv_scale_val == 1.f ? val : val * inv_scale_val);
  }

  _amp_non_finite_check_and_unscale_dpcpp_functor(
      float* found_inf_ptr,
      float* inv_scale_ptr)
      : found_inf_ptr(found_inf_ptr), inv_scale_ptr(inv_scale_ptr) {}

 private:
  float* found_inf_ptr;
  float* inv_scale_ptr;
};

// Single-tensor fallback for _amp_foreach_non_finite_check_and_unscale_dpcpp_.
// Handles individual tensors that are acceptable to unscale but not MTA-safe.
void _amp_non_finite_check_and_unscale_dpcpp_(
    Tensor& scaled_grad,
    Tensor& found_inf,
    const Tensor& inv_scale) {
  // The only way we reach this function is through
  // _amp_foreach_non_finite_check_and_unscale_dpcpp_, so no input checks.

  // Acts on scaled_grad in place.
  auto iter = TensorIterator::unary_op(scaled_grad, scaled_grad);

  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.dtype(),
      "_amp_non_finite_check_and_unscale_dpcpp",
      [&iter, &found_inf, &inv_scale] {
        auto* found_inf_ptr = found_inf.data_ptr<float>();
        auto* inv_scale_ptr = inv_scale.data_ptr<float>();

        using opmath_t = at::opmath_type<scalar_t>;
        _amp_non_finite_check_and_unscale_dpcpp_functor<scalar_t, opmath_t> f(
            found_inf_ptr, inv_scale_ptr);
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename opmath_t>
struct _amp_foreach_non_finite_check_and_unscale_functor {
  opmath_t operator()(opmath_t val) const {
    // There is a slight asymmetry here with the TensorIterator kernel
    // above. MTA Functors ensure val comes in as opmath_t rather than
    // scalar_t.
    if (Numerics<opmath_t>::isinf(val) || Numerics<opmath_t>::isnan(val)) {
      *found_inf_ptr = 1.f;
    }
    // Every thread accesses inv_scale, but it will hit in cache.
    const auto inv_scale_val = *inv_scale_ptr;
    return static_cast<opmath_t>(
        inv_scale_val == 1.f ? val : val * inv_scale_val);
  }
  _amp_foreach_non_finite_check_and_unscale_functor(
      float* found_inf_ptr,
      float* inv_scale_ptr)
      : found_inf_ptr(found_inf_ptr), inv_scale_ptr(inv_scale_ptr) {}

 private:
  float* found_inf_ptr;
  float* inv_scale_ptr;
};

// Multiplies each tensor in scaled_grads by inv_scale in-place.
// If any element of any tensor in scaled_grads is inf or NaN, sets found_inf
// to 1.0. Uses multi tensor apply (MTA) to process all MTA-safe tensors.
//
// Args:
// scaled_grads:  A TensorList of scaled gradient tensors.  May contain infs or
// NaNs. found_inf:  A single-element float tensor to which 1.0 will be written
// if any gradient contain infs/nans.
//             Pre-zeroing found_inf, if appropriate, is the responsibility of
//             the caller.
// inv_scale:  The inverse of the scale factor by which scaled_grads are
// currently multiplied.
void _amp_foreach_non_finite_check_and_unscale_(
    TensorList scaled_grads,
    Tensor& found_inf,
    const Tensor& inv_scale) {
  if (scaled_grads.size() == 0) {
    return;
  }

  TORCH_CHECK(inv_scale.is_xpu(), "inv_scale must be a XPU tensor.");
  TORCH_CHECK(found_inf.is_xpu(), "found_inf must be a XPU tensor.");
  TORCH_CHECK(inv_scale.numel() == 1, "inv_scale must be a 1-element tensor.");
  TORCH_CHECK(found_inf.numel() == 1, "found_inf must be a 1-element tensor.");
  TORCH_CHECK(
      inv_scale.scalar_type() == at::ScalarType::Float,
      "inv_scale must be a float tensor.");
  TORCH_CHECK(
      found_inf.scalar_type() == at::ScalarType::Float,
      "found_inf must be a float tensor.");

  // Ensures client code (GradScaler) filtered scaled_grads by dtype.
  at::native::check_foreach_api_restrictions(scaled_grads);

  std::vector<std::vector<at::Tensor>> tensor_lists;

  // is_non_overlapping_and_dense() is not available in Python.
  // GradScaler can't filter for it. We need to filter here.
  if (at::native::can_use_fast_route(scaled_grads)) {
    // Hopefully common case.
    // can_use_fast_route is true, which confirms:
    //  - all scaled_grads are strided
    //  - all scaled_grads are non overlapping and dense
    //  - all scaled_grads are on the same device
    //  - all scaled_grads are of the same dtype
    TORCH_CHECK(scaled_grads[0].is_xpu(), "scaled_grads must be XPU tensors.");
    // Sets up MTA launch to use scaled_grads as-is.
    tensor_lists.emplace_back(scaled_grads.vec());
  } else {
    // Hopefully uncommon case.
    // can_use_fast_route is an all-or-nothing check.  In this path it was
    // false, so any of the above confirmations could have gone wrong. We filter
    // MTA-safe tensors into an MTA-able list. If a tensor is acceptable but not
    // MTA-safe, we fall back to the TensorIterator kernel. If a tensor is
    // unacceptable, we throw an error to blame GradScaler.
    tensor_lists.resize(1);
    tensor_lists[0].reserve(scaled_grads.size());
    auto expected_device = scaled_grads[0].device();
    const auto expected_dtype = scaled_grads[0].scalar_type();
    for (const Tensor& t : scaled_grads) {
      // Ensures GradScaler filtered scaled_grads by device.
      TORCH_CHECK(t.is_xpu(), "one of scaled_grads was not a XPU tensor.");
      TORCH_CHECK(
          t.device() == expected_device,
          "scaled_grads must be on the same device.");
      TORCH_CHECK(
          t.layout() == at::kStrided,
          "one of scaled_grads was not a strided tensor.");
      if (!t.is_non_overlapping_and_dense() ||
          t.scalar_type() != expected_dtype) {
        // t is acceptable but not MTA-safe.  Falls back to single-tensor
        // TensorIterator kernel.
        _amp_non_finite_check_and_unscale_dpcpp_(
            const_cast<Tensor&>(t), found_inf, inv_scale);
      } else {
        tensor_lists[0].push_back(t);
      }
    }
    if (tensor_lists[0].size() == 0) {
      return;
    }
  }

  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      tensor_lists[0][0].scalar_type(),
      "_amp_foreach_non_finite_check_and_unscale_dpcpp",
      [&tensor_lists, &found_inf, &inv_scale] {
        auto* found_inf_ptr = found_inf.data_ptr<float>();
        auto* inv_scale_ptr = inv_scale.data_ptr<float>();

        using opmath_t = at::opmath_type<scalar_t>;

        // multi_tensor_apply guards onto tensor_lists[0][0], no need to guard
        // explicitly.
        _amp_foreach_non_finite_check_and_unscale_functor<opmath_t> f(
            found_inf_ptr, inv_scale_ptr);
        multi_tensor_apply<1>(
            tensor_lists,
            UnaryOpFunctor<
                scalar_t,
                /* depth */ 1,
                /* r_args_depth */ 1,
                /* res_arg_index */ 0>(),
            f);
      });
}

struct AmpUpdateScaleKernelFunctor {
  void operator()() const {
    if (*found_inf) {
      *current_scale *= backoff_factor;
      *growth_tracker = 0;
    } else {
      // Entering this branch means we just carried out a successful step,
      // so growth_tracker is incremented before comparing to growth_interval.
      auto successful = (*growth_tracker) + 1;
      if (successful == growth_interval) {
        *current_scale *= growth_factor;
        *growth_tracker = 0;
      } else {
        *growth_tracker = successful;
      }
    }
  }
  AmpUpdateScaleKernelFunctor(
      float* current_scale_,
      int* growth_tracker_,
      float* found_inf_,
      double growth_factor_,
      double backoff_factor_,
      int growth_interval_)
      : current_scale(current_scale_),
        growth_tracker(growth_tracker_),
        found_inf(found_inf_),
        growth_factor(growth_factor_),
        backoff_factor(backoff_factor_),
        growth_interval(growth_interval_) {}

 private:
  float* current_scale;
  int* growth_tracker;
  float* found_inf;
  double growth_factor;
  double backoff_factor;
  int growth_interval;
};

// _amp_update_scale_kernel is launched with a single work-item to compute the
// new scale. The scale factor is maintained and updated on the XPU
// asynchronously.
void _amp_update_scale_kernel(
    float* current_scale,
    int* growth_tracker,
    float* found_inf,
    double growth_factor,
    double backoff_factor,
    int growth_interval) {
  auto& dpcpp_queue = dpcppGetCurrentQueue();
  auto cgf = DPCPP_Q_CGF(cgf) {
    AmpUpdateScaleKernelFunctor kfn(
        current_scale,
        growth_tracker,
        found_inf,
        growth_factor,
        backoff_factor,
        growth_interval);
    cgf.single_task<decltype(kfn)>(kfn);
  };

  DPCPP_Q_SUBMIT(dpcpp_queue, cgf);
}

// _amp_update_scale_ asynchronously updates the scale tensor in place.
//
// Args:
// current_scale:  A one-element xpu float tensor containing the scale value.
// growth_tracker:  A one-element torch.xpu.IntTensor containing the number of
// recent consecutive unskipped steps.
// found_inf:  A one-element xpu float tensor. If > 0, indicates that infs/nans
// were found by the relevant prior _amp_non_finite_check_and_unscale_ call,
// and 0 if no infs/nans were found.
// growth_factor:  Multiplier if no infs/NaNs were found (typically slightly
// >1). backoff_factor:  Multiplier if infs/NaNs were found (typically 0.5).
// growth_interval:  Number of consecutive unskipped steps that must occur for
// current_scale to be multiplied by growth_factor.
//
// Returns:
// current_scale:  updated in place.
Tensor& _amp_update_scale_(
    Tensor& current_scale,
    Tensor& growth_tracker,
    const Tensor& found_inf,
    double growth_factor,
    double backoff_factor,
    int64_t growth_interval) {
  TORCH_CHECK(growth_tracker.is_xpu(), "growth_tracker must be a XPU tensor.");
  TORCH_CHECK(current_scale.is_xpu(), "current_scale must be a XPU tensor.");
  TORCH_CHECK(found_inf.is_xpu(), "found_inf must be a XPU tensor.");
  TORCH_CHECK(
      growth_tracker.numel() == 1,
      "growth_tracker must be a 1-element tensor.");
  TORCH_CHECK(
      current_scale.numel() == 1, "current_scale must be a 1-element tensor.");
  TORCH_CHECK(found_inf.numel() == 1, "found_inf must be a 1-element tensor.");
  TORCH_CHECK(
      growth_tracker.scalar_type() == at::ScalarType::Int,
      "growth_tracker must be an int tensor.");
  TORCH_CHECK(
      current_scale.scalar_type() == at::ScalarType::Float,
      "current_scale must be a float tensor.");
  TORCH_CHECK(
      found_inf.scalar_type() == at::ScalarType::Float,
      "found_inf must be a float tensor.");

  _amp_update_scale_kernel(
      current_scale.data_ptr<float>(),
      growth_tracker.data_ptr<int>(),
      found_inf.data_ptr<float>(),
      growth_factor,
      backoff_factor,
      growth_interval);

  return current_scale;
}

} // namespace AtenIpexTypeXPU
} // namespace at
