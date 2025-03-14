#include <ATen/ATen.h>
#include <ATen/native/Resize.h>
#include <ATen/native/TensorIterator.h>
#include "comm/AccumulateType.h"

#include <core/Memory.h>
#include <runtime/Utils.h>
#include <utils/oneMKLUtils.h>
#include "comm/ATDispatch.h"
#include "comm/Math.h"
#include "comm/Numerics.h"
#include "comm/RegistrationDeclarations.h"

#include "Loops.h"

using namespace torch_ipex::xpu::dpcpp::detail;
using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {
namespace impl {

template <typename scalar_t>
struct digamma_kernel_xpu_functor {
  scalar_t operator()(scalar_t a) const {
    return calc_digamma(a);
  }
};

void digamma_kernel_xpu(TensorIterator& iter) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.common_dtype(),
      "digamma_xpu",
      [&]() {
        digamma_kernel_xpu_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename scalar_t>
struct igamma_kernel_xpu_functor {
  scalar_t operator()(scalar_t a, scalar_t b) const {
    return calc_igamma(a, b);
  }
};

void igamma_kernel_xpu(TensorIterator& iter) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.common_dtype(),
      "igamma_xpu",
      [&]() {
        igamma_kernel_xpu_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename scalar_t>
struct igammac_kernel_xpu_functor {
  scalar_t operator()(scalar_t a, scalar_t b) const {
    return calc_igammac(a, b);
  }
};

void igammac_kernel_xpu(TensorIterator& iter) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.common_dtype(),
      "igammac_xpu",
      [&]() {
        igammac_kernel_xpu_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename scalar_t>
struct trigamma_kernel_xpu_functor {
  scalar_t operator()(scalar_t a) const {
    return calc_trigamma(a);
  }
};

void trigamma_kernel_xpu(TensorIterator& iter) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.common_dtype(),
      "trigamma_xpu",
      [&]() {
        trigamma_kernel_xpu_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename scalar_t>
struct polygamma_kernel_xpu_functor {
  scalar_t operator()(scalar_t a) const {
    return calc_polygamma(a, n);
  }

  polygamma_kernel_xpu_functor(int64_t n) : n(n) {}

 private:
  int64_t n;
};

void polygamma_kernel_xpu(TensorIterator& iter, int64_t n) {
  if (n == 0) {
    digamma_kernel_xpu(iter);
  } else if (n == 1) {
    trigamma_kernel_xpu(iter);
  } else {
    IPEX_DISPATCH_FLOATING_TYPES_AND2(
        at::ScalarType::Half,
        at::ScalarType::BFloat16,
        iter.common_dtype(),
        "polygamma_xpu",
        [&]() {
          polygamma_kernel_xpu_functor<scalar_t> f(n);
          dpcpp_kernel_for_tensor_iter(iter, f);
        });
  }
}

} // namespace impl

static inline void lgamma_check(const Tensor& self) {
  TORCH_INTERNAL_ASSERT(
      at::isFloatingType(self.scalar_type()),
      "Only support floating data type for now.");
}

template <typename scalar_t>
struct lgamma_out_functor {
  scalar_t operator()(scalar_t a) const {
    return Numerics<scalar_t>::lgamma(a);
  }
};

Tensor& lgamma_out(const Tensor& self, Tensor& out) {
  auto iter = TensorIterator::unary_float_op(out, self);
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      kHalf, kBFloat16, iter.common_dtype(), "lgamma", [&]() {
        lgamma_out_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
  return out;
}

Tensor& mvlgamma_out(const Tensor& self, int64_t p, Tensor& out) {
  auto output = self.mvlgamma(p);
  TORCH_CHECK(
      at::can_cast(output.scalar_type(), out.scalar_type()),
      "mvlgamma: result type ",
      self.scalar_type(),
      " can't be cast to the desired output type ",
      output.scalar_type());
  at::native::resize_output(out, output.sizes());
  return out.copy_(output);
}

Tensor digamma(const Tensor& self) {
  Tensor result = !at::isFloatingType(self.scalar_type())
      ? at::empty(self.sizes(), self.options().dtype(at::kFloat))
      : at::empty_like(self);
  auto iter = TensorIterator::unary_float_op(result, self);
  impl::digamma_kernel_xpu(iter);
  return result;
}

Tensor& digamma_(Tensor& self) {
  auto iter = TensorIterator::unary_float_op(self, self);
  impl::digamma_kernel_xpu(iter);
  return self;
}

Tensor& digamma_out(const Tensor& self, Tensor& out) {
  auto iter = TensorIterator::unary_float_op(out, self);
  impl::digamma_kernel_xpu(iter);
  return out;
}

Tensor& igamma_out(const Tensor& self, const Tensor& other, Tensor& out) {
  auto iter = TensorIterator::binary_float_op(out, self, other);
  impl::igamma_kernel_xpu(iter);
  return out;
}

Tensor& igammac_out(const Tensor& self, const Tensor& other, Tensor& out) {
  auto iter = TensorIterator::binary_float_op(out, self, other);
  impl::igammac_kernel_xpu(iter);
  return out;
}

Tensor polygamma(int64_t n, const Tensor& self) {
  TORCH_CHECK(n >= 0, "polygamma(n, x) does not support negative n.");
  Tensor result = !at::isFloatingType(self.scalar_type())
      ? at::empty(self.sizes(), self.options().dtype(at::kFloat))
      : at::empty_like(self);
  auto iter = TensorIterator::unary_float_op(result, self);
  impl::polygamma_kernel_xpu(iter, n);
  return result;
}

Tensor& polygamma_(Tensor& self, int64_t n) {
  auto iter = TensorIterator::unary_float_op(self, self);
  impl::polygamma_kernel_xpu(iter, n);
  return self;
}

Tensor& polygamma_out(int64_t n, const Tensor& self, Tensor& out) {
  auto iter = TensorIterator::unary_float_op(out, self);
  impl::polygamma_kernel_xpu(iter, n);
  return out;
}

} // namespace AtenIpexTypeXPU
} // namespace at
