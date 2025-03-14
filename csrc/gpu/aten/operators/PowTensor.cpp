#include "Loops.h"
#include "comm/ATDispatch.h"
#include "comm/ApplyUtils.h"
#include "comm/Numerics.h"
#include "comm/Pointwise.h"
#include "comm/RegistrationDeclarations.h"
#include "comm/ScalarOps.h"

using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {

void pow_tensor_scalar_kernel(TensorIterator& iter, const Scalar& exp_scalar);

template <typename scalar_t>
struct pow_tensor_tensor_kernel_functor {
  scalar_t operator()(scalar_t exp) const {
    return Numerics<scalar_t>::pow(base, exp);
  }

  pow_tensor_tensor_kernel_functor(scalar_t base) : base(base) {}

 private:
  scalar_t base;
};

template <typename scalar_t>
struct pow_tensor_tensor_kernel_functor_2 {
  scalar_t operator()(scalar_t base, scalar_t exp) const {
    return Numerics<scalar_t>::pow(base, exp);
  }
};

void pow_tensor_tensor_kernel(TensorIterator& iter) {
  IPEX_DISPATCH_ALL_TYPES_AND_COMPLEX_AND2(
      kHalf, kBFloat16, iter.common_dtype(), "pow_xpu", [&]() {
        if (iter.is_cpu_scalar(1)) {
          const auto base = iter.scalar_value<scalar_t>(1);
          iter.remove_operand(1);
          pow_tensor_tensor_kernel_functor<scalar_t> f(base);
          dpcpp_kernel_for_tensor_iter(iter, f);
        } else if (iter.is_cpu_scalar(2)) {
          const auto exp = iter.scalar_value<scalar_t>(2);
          iter.remove_operand(2);
          at::AtenIpexTypeXPU::pow_tensor_scalar_kernel(iter, exp);
        } else {
          pow_tensor_tensor_kernel_functor_2<scalar_t> f;
          dpcpp_kernel_for_tensor_iter(iter, f);
        }
      });
}

Tensor& pow_out(const Tensor& base, const Tensor& exp, Tensor& result) {
  auto iter = TensorIterator::binary_op(result, base, exp);
  pow_tensor_tensor_kernel(iter);
  return result;
}

Tensor& pow_out(const Scalar& base, const Tensor& exp, Tensor& result) {
  if (base.isComplex() && base.toComplexDouble() == 1.0) {
    result.resize_as_(exp).fill_(1);
  } else if (!base.isComplex() && base.toDouble() == 1.0) {
    result.resize_as_(exp).fill_(1);
  } else {
    at::AtenIpexTypeXPU::pow_out(wrapped_scalar_tensor(base), exp, result);
  }
  return result;
}

Tensor pow(const Tensor& base, const Tensor& exp) {
  auto dtype = at::result_type(base, exp);
  Tensor result = at::empty({0}, base.options().dtype(dtype));
  return at::AtenIpexTypeXPU::pow_out(base, exp, result);
}

} // namespace AtenIpexTypeXPU
} // namespace at
