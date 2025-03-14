#include <ATen/ATen.h>
#include <ATen/native/TensorIterator.h>

#include <ATen/xpu/XPUGeneratorImpl.h>
#include "comm/ATDispatch.h"
#include "comm/AccumulateType.h"
#include "comm/RegistrationDeclarations.h"

#include "DistributionTemplates.h"
#include "RandomEngine.h"

namespace at {
namespace AtenIpexTypeXPU {

template <typename scalar_t>
struct bernoulli_functor {
  scalar_t operator()(scalar_t self_float, scalar_t p) const {
    return static_cast<scalar_t>(self_float < p);
  }
};

Tensor& bernoulli_(
    Tensor& self,
    const Tensor& p_,
    c10::optional<Generator> gen_) {
  Tensor self_float;
  auto self_type = self.scalar_type();
  if (!(self_type == at::ScalarType::Float ||
        self_type == at::ScalarType::Double))
    self_float = at::empty(self.sizes(), self.options().dtype(at::kFloat));
  else
    self_float = self;
  at::AtenIpexTypeXPU::uniform_(self_float, 0.0, 1.0, gen_);
  auto p = p_.to(kXPU);
  auto iter = TensorIteratorConfig()
                  .add_output(self)
                  .add_input(self_float)
                  .add_input(p)
                  .check_all_same_dtype(false)
                  .build();

  IPEX_DISPATCH_ALL_TYPES_AND2(
      at::ScalarType::Bool,
      at::ScalarType::BFloat16,
      iter.common_dtype(),
      "bernoulli_tensor_dpcpp_",
      [&] {
        bernoulli_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
  return self;
}

template <typename scalar_t, typename accscalar_t>
struct bernoulli_functor_2 {
  scalar_t operator()(accscalar_t rand) const {
    return static_cast<scalar_t>(rand < static_cast<accscalar_t>(p));
  }

  bernoulli_functor_2(accscalar_t p) : p(p) {}

 private:
  accscalar_t p;
};

Tensor& bernoulli_(Tensor& self, double p_, c10::optional<Generator> gen_) {
  auto iter = TensorIterator::nullary_op(self);
  auto gen = get_generator_or_default<at::XPUGeneratorImpl>(
      gen_, at::xpu::detail::getDefaultXPUGenerator());
  IPEX_DISPATCH_ALL_TYPES_AND3(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      at::ScalarType::Bool,
      iter.dtype(),
      "bernoulli_",
      [&] {
        using accscalar_t = DiscreteDistributionType<scalar_t>::type;
        auto p = static_cast<accscalar_t>(p_);
        bernoulli_functor_2<scalar_t, accscalar_t> bernoulli_func(p);
        uniform_and_transform<scalar_t, accscalar_t, PHILOX_ENGINE_CALLS>(
            iter, gen, bernoulli_func);
      });
  return self;
}

template <typename scalar_t>
struct bernoulli_out_functor {
  scalar_t operator()(scalar_t out, scalar_t p) const {
    return static_cast<scalar_t>(out < p);
  }
};

Tensor& bernoulli_out(
    const Tensor& self,
    c10::optional<Generator> generator,
    Tensor& out) {
  auto out_type = out.scalar_type();
  Tensor out_float;
  if (!(out_type == at::ScalarType::Float ||
        out_type == at::ScalarType::Double))
    out_float = self.to(at::ScalarType::Float);
  else
    out_float = out;
  at::AtenIpexTypeXPU::uniform_(out_float, 0.0, 1.0, generator);
  auto iter = TensorIteratorConfig()
                  .add_output(out)
                  .add_input(out_float)
                  .add_input(self)
                  .check_all_same_dtype(false)
                  .build();
  IPEX_DISPATCH_ALL_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.common_dtype(),
      "bernoulli_tensor_dpcpp_",
      [&] {
        bernoulli_out_functor<scalar_t> f;
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
  return out;
}

} // namespace AtenIpexTypeXPU
} // namespace at
