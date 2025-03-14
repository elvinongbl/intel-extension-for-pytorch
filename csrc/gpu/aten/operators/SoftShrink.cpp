#include <ATen/native/TensorIterator.h>

#include <utils/DPCPP.h>
#include "comm/ATDispatch.h"
#include "comm/Numerics.h"
#include "comm/RegistrationDeclarations.h"

#include "Loops.h"

using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {
namespace impl {

template <typename scalar_t>
struct softshrink_forward_functor {
  scalar_t operator()(scalar_t x) const {
    if (x > lambd_data) {
      return (x - lambd_data);
    } else if (x < -lambd_data) {
      return (x + lambd_data);
    } else {
      return ScalarConvert<int, scalar_t>::to(0);
    }
  }

  softshrink_forward_functor(scalar_t lambd_data) : lambd_data(lambd_data) {}

 private:
  scalar_t lambd_data;
};

static void softshrink_forward(TensorIterator& iter, Scalar lambd) {
  IPEX_DISPATCH_ALL_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.dtype(),
      "softshrink_forward",
      [&]() {
        auto lambd_data = lambd.to<scalar_t>();
        softshrink_forward_functor<scalar_t> f(lambd_data);
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename scalar_t>
struct softshrink_backward_functor {
  scalar_t operator()(scalar_t grad_output, scalar_t input) const {
    if (input > lambd_data || input < -lambd_data) {
      return grad_output;
    } else {
      return ScalarConvert<int, scalar_t>::to(0);
    }
  }

  softshrink_backward_functor(scalar_t lambd_data) : lambd_data(lambd_data) {}

 private:
  scalar_t lambd_data;
};

static void softshrink_backward(TensorIterator& iter, Scalar lambd) {
  IPEX_DISPATCH_ALL_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.dtype(),
      "softshrink_backward",
      [&]() {
        auto lambd_data = lambd.to<scalar_t>();
        softshrink_backward_functor<scalar_t> f(lambd_data);
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

} // namespace impl

Tensor& softshrink_out(const Tensor& self, const Scalar& lambd, Tensor& out) {
  checkBackend("softshrink_forward", {out}, self.options().backend());
  auto iter = TensorIteratorConfig()
                  .set_check_mem_overlap(true)
                  .add_output(out)
                  .add_input(self)
                  .build();
  impl::softshrink_forward(iter, lambd);
  return out;
}

Tensor& softshrink_backward_out(
    const Tensor& grad_output,
    const Tensor& self,
    const Scalar& lambd,
    Tensor& grad_input) {
  checkBackend(
      "softshrink_backward",
      {grad_input, grad_output},
      self.options().backend());
  auto iter = TensorIteratorConfig()
                  .set_check_mem_overlap(true)
                  .add_output(grad_input)
                  .add_input(grad_output)
                  .add_input(self)
                  .build();
  impl::softshrink_backward(iter, lambd);
  return grad_input;
}

} // namespace AtenIpexTypeXPU
} // namespace at
