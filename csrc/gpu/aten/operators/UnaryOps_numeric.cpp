#include <ATen/ATen.h>
#include <ATen/OpMathType.h>
#include <ATen/native/TensorIterator.h>
#include <oneDNN/oneDNN.h>
#include <utils/DPCPP.h>

#include "comm/AccumulateType.h"
#include "comm/LoopsMeta.h"
#include "comm/Numerics.h"
#include "comm/Pairwise.h"
#include "comm/Pointwise.h"
#include "comm/RegistrationDeclarations.h"

#include "Loops.h"
#include "LoopsTemplates.h"
#include "Resize.h"
#include "utils/logging.h"

using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {
namespace impl {

template <typename func_t>
static inline Tensor& unary_op_impl_with_complex_to_float_out(
    Tensor& result,
    const Tensor& self,
    const func_t& fn,
    bool promotes_integer_to_float) {
  if (self.is_complex() && !result.is_complex()) {
    // Checks if the corresponding float type can be cast to the desired dtype
    const auto float_type = c10::toRealValueType(self.scalar_type());
    TORCH_CHECK(
        canCast(float_type, result.scalar_type()),
        "result type ",
        float_type,
        " can't be cast to the desired output type ",
        result.scalar_type());

    // Runs the function complex->complex, as TensorIterator expects
    Tensor complex_result = at::empty({0}, self.options());
    auto self_ = at::AtenIpexTypeXPU::to_plain_if_needed(self);
    auto iter = TensorIterator::unary_op(complex_result, self_);
    fn(iter);

    // Copies the complex result to the actual result and returns it
    resize_output(result, complex_result.sizes());
    result.copy_(at::real(complex_result));
    return result;
  }

  if (promotes_integer_to_float) {
    result = at::AtenIpexTypeXPU::to_plain_if_needed_(result);
    auto self_ = at::AtenIpexTypeXPU::to_plain_if_needed(self);
    auto iter = TensorIterator::unary_float_op(result, self_);
    fn(iter);
    iter.cast_outputs();
    return result;
  }

  // abs kernel
  return unary_out_with_onednn_and_loops<dnnl::algorithm::eltwise_abs>(
      TensorIterator::unary_op, result, self, [=](TensorIteratorBase& iter) {
        fn(iter);
      });
}

template <typename T>
static T abs_impl(T v) {
  return Numerics<T>::abs(v);
}

template <typename scalar_t>
struct abs_kernel_functor {
  scalar_t operator()(scalar_t a) const {
    return abs_impl<scalar_t>(a);
  }
};

void abs_kernel(TensorIteratorBase& iter) {
  auto dtype = iter.dtype();
  if (at::isComplexType(dtype)) {
    IPEX_DISPATCH_COMPLEX_TYPES_AND(kComplexHalf, dtype, "abs", [&]() {
      using opmath_t = at::opmath_type<scalar_t>;
      abs_kernel_functor<opmath_t> f;
      dpcpp_kernel_for_tensor_iter(iter, f);
    });
  } else {
    IPEX_DISPATCH_ALL_TYPES_AND3(
        ScalarType::Half,
        ScalarType::BFloat16,
        ScalarType::Bool,
        iter.dtype(),
        "abs",
        [&]() {
          abs_kernel_functor<scalar_t> f;
          dpcpp_kernel_for_tensor_iter(iter, f);
        });
  }
}

template <typename scalar_t>
struct angle_kernel_functor {
  scalar_t operator()(scalar_t a) const {
    return at::AtenIpexTypeXPU::angle_impl(a);
  }
};

void angle_kernel(TensorIteratorBase& iter) {
  IPEX_DISPATCH_FLOATING_AND_COMPLEX_TYPES_AND2(
      kBFloat16, kHalf, iter.common_dtype(), "angle", [&]() {
        angle_kernel_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename scalar_t>
struct nan_to_num_kernel_functor {
  scalar_t operator()(scalar_t a) const {
    // TODO: evaluate the root cause of strange behavior with +inf/-inf on
    // Windows only: When a is inf, a ==
    // std::numeric_limits<scalar_t>::infinity() evaluates incorrectly to
    // false, while std::isinf(a) evaluates correctly to true. As a
    // workaround, we use 'Numerics<scalar_t>::isinf(a) && a<0' to check
    // if a is -inf, and 'Numerics<scalar_t>::isinf(a)' to check if a is
    // +inf.
    return (
        at::_isnan(a)
            ? nan_replacement
            : (Numerics<scalar_t>::isinf(a) && a < 0
                   ? neg_inf_replacement
                   : (Numerics<scalar_t>::isinf(a) ? pos_inf_replacement : a)));
  }
  nan_to_num_kernel_functor(
      scalar_t nan_replacement,
      scalar_t pos_inf_replacement,
      scalar_t neg_inf_replacement)
      : nan_replacement(nan_replacement),
        pos_inf_replacement(pos_inf_replacement),
        neg_inf_replacement(neg_inf_replacement) {}

 private:
  scalar_t nan_replacement;
  scalar_t pos_inf_replacement;
  scalar_t neg_inf_replacement;
};

void nan_to_num_kernel(
    TensorIteratorBase& iter,
    c10::optional<double> nan,
    c10::optional<double> pos_inf,
    c10::optional<double> neg_inf) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      kHalf, kBFloat16, iter.dtype(), "nan_to_num_dpcpp", [&]() {
        scalar_t nan_replacement = static_cast<scalar_t>(nan.value_or(0.));
        scalar_t pos_inf_replacement = pos_inf.has_value()
            ? static_cast<scalar_t>(pos_inf.value())
            : std::numeric_limits<scalar_t>::max();
        scalar_t neg_inf_replacement = neg_inf.has_value()
            ? static_cast<scalar_t>(neg_inf.value())
            : std::numeric_limits<scalar_t>::lowest();
        nan_to_num_kernel_functor<scalar_t> f(
            nan_replacement, pos_inf_replacement, neg_inf_replacement);
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

} // namespace impl

Tensor& abs_out(const Tensor& self, Tensor& result) {
  return impl::unary_op_impl_with_complex_to_float_out(
      result, self, impl::abs_kernel, /*promotes_integer_to_float=*/false);
}

Tensor& angle_out(const Tensor& self, Tensor& result) {
  return impl::unary_op_impl_with_complex_to_float_out(
      result, self, impl::angle_kernel, /*promotes_integer_to_float=*/true);
}

Tensor angle(const Tensor& self) {
  if (self.is_complex()) {
    const auto float_type = c10::toRealValueType(self.scalar_type());
    Tensor result = at::empty({0}, self.options().dtype(float_type));
    return at::angle_out(result, self);
  }
  Tensor result;
  auto iter = TensorIterator::unary_float_op(result, self);
  impl::angle_kernel(iter);
  return iter.output();
}

Tensor& nan_to_num_out(
    const Tensor& self,
    c10::optional<double> nan,
    c10::optional<double> pos_inf,
    c10::optional<double> neg_inf,
    Tensor& result) {
  TORCH_CHECK(
      self.scalar_type() == result.scalar_type(),
      "nan_to_num: dtype of out: ",
      result.scalar_type(),
      " should be same as input: ",
      self.scalar_type());

  if (c10::isIntegralType(self.scalar_type(), /*includeBool=*/true)) {
    resize_output(result, self.sizes());
    result.copy_(self);
    return result;
  }

  auto iter = TensorIterator::unary_op(result, self);
  impl::nan_to_num_kernel(iter, nan, pos_inf, neg_inf);

  return result;
}

Tensor nan_to_num(
    const Tensor& self,
    c10::optional<double> nan,
    c10::optional<double> pos_inf,
    c10::optional<double> neg_inf) {
  auto result = at::empty_like(self);
  return at::nan_to_num_out(result, self, nan, pos_inf, neg_inf);
}

Tensor& nan_to_num_(
    Tensor& self,
    c10::optional<double> nan,
    c10::optional<double> pos_inf,
    c10::optional<double> neg_inf) {
  return at::nan_to_num_out(self, self, nan, pos_inf, neg_inf);
}

} // namespace AtenIpexTypeXPU
} // namespace at
