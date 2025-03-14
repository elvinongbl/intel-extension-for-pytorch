#include <ATen/ATen.h>
#include <ATen/DeviceGuard.h>
#include <ATen/Functions.h>
#include <ATen/native/Activation.h>
#include <ATen/record_function.h>
#include <core/Memory.h>
#include <runtime/Utils.h>
#include <tensor/Tensor.h>
#include <torch/library.h>
#include <utils/DPCPP.h>
#include "comm/AccumulateType.h"
#include "comm/ApplyUtils.h"
#include "comm/Numerics.h"

#include "FusedFunctors.h"
#include "comm/ATDispatch.h"
#include "comm/Numerics.h"

#include <aten/operators/MemoryAccess.h>

using namespace torch_ipex::xpu::dpcpp;
using namespace torch_ipex::xpu::dpcpp::detail;

namespace at {
namespace AtenIpexTypeXPU {
namespace impl {

template <int vec_size, typename scalar_t, typename vec_t, typename vec_mw_t>
struct LaunchVecKernelAdamWMasterWeightFunctor {
  void operator()(sycl::item<1> item) const {
    auto id = item.get_id(0);

    auto remaining = total_element - id * vec_size;

    // for handling remaining tail
    if (remaining < vec_size) {
      for (auto v_index = 0; v_index < remaining; ++v_index) {
        // kick out tail
        auto linear_idx = id * vec_size + v_index;
        // master weight grad should be fp32 to involve in computation to keep
        // acc.
        auto grad_elm = static_cast<float>(grad_ptr[linear_idx]);

        auto master_weight_elem = master_weight_ptr[linear_idx];
        master_weight_elem = master_weight_elem * stepweight_decay;

        // exp_avg
        auto exp_avg_ele = exp_avg_ptr[linear_idx];
        exp_avg_ele =
            exp_avg_ele * beta1_value + grad_elm * exp_avg_ele_coefficient;
        exp_avg_ptr[linear_idx] = exp_avg_ele;

        // exp_avg_sq
        auto exp_avg_sq_ele = exp_avg_sq_ptr[linear_idx];
        exp_avg_sq_ele = exp_avg_sq_ele * beta2_value +
            grad_elm * grad_elm * exp_avg_sq_ele_coefficient;
        exp_avg_sq_ptr[linear_idx] = exp_avg_sq_ele;

        // amsgrad
        if (amsgrad) {
          max_exp_avg_sq_ptr[linear_idx] =
              max_exp_avg_sq_ptr[linear_idx] < exp_avg_sq_ele
              ? exp_avg_sq_ele
              : max_exp_avg_sq_ptr[linear_idx];
          master_weight_elem = master_weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(
                       max_exp_avg_sq_ptr[linear_idx] / bias_correction2) +
                   eps_value);
        } else {
          master_weight_elem = master_weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(exp_avg_sq_ele / bias_correction2) +
                   eps_value);
        }

        // update master weight fp32
        master_weight_ptr[linear_idx] = static_cast<float>(master_weight_elem);

        // update real weight bf16/fp16
        weight_ptr[linear_idx] = static_cast<scalar_t>(master_weight_elem);
      }
    } else {
      // vector read
      vec_mw_t exp_avg_value = exp_avg_vec[id];
      vec_mw_t exp_avg_sq_value = exp_avg_sq_vec[id];
      vec_mw_t master_weight_value = master_weight_vec[id];

      // for vector write back
      vec_t temp_weight;
      vec_mw_t temp_master_weight;
      vec_mw_t temp_exp_avg;
      vec_mw_t temp_exp_avg_sq;

#pragma unroll(vec_size)
      for (auto v_index = 0; v_index < vec_size; ++v_index) {
        // [watch out] here using these methods to read BF16
        // grad vector to avoid omit for read instruction. external JIRA:
        // https://jira.devtools.intel.com/browse/CMPLRLLVM-42194
        auto grad_elm = static_cast<float>(grad_vec[id][v_index]);

        auto master_weight_elem = master_weight_value[v_index];
        master_weight_elem = master_weight_elem * stepweight_decay;

        // exp_avg
        auto exp_avg_ele = exp_avg_value[v_index];
        exp_avg_ele =
            exp_avg_ele * beta1_value + grad_elm * exp_avg_ele_coefficient;
        temp_exp_avg[v_index] = exp_avg_ele;

        // exp_avg_sq
        auto exp_avg_sq_ele = exp_avg_sq_value[v_index];
        exp_avg_sq_ele = exp_avg_sq_ele * beta2_value +
            grad_elm * grad_elm * exp_avg_sq_ele_coefficient;
        temp_exp_avg_sq[v_index] = exp_avg_sq_ele;

        // amsgrad
        if (amsgrad) {
          auto max_exp_avg_sq_ele = max_exp_avg_sq_vec[id][v_index];
          max_exp_avg_sq_ele = max_exp_avg_sq_ele < exp_avg_sq_ele
              ? exp_avg_sq_ele
              : max_exp_avg_sq_ele;
          max_exp_avg_sq_vec[id][v_index] = max_exp_avg_sq_ele;
          master_weight_elem = master_weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(
                       max_exp_avg_sq_ele / bias_correction2) +
                   eps_value);
        } else {
          master_weight_elem = master_weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(exp_avg_sq_ele / bias_correction2) +
                   eps_value);
        }

        // update master weight fp32
        temp_master_weight[v_index] = static_cast<float>(master_weight_elem);

        // update real weight bf16/fp16
        temp_weight[v_index] = static_cast<scalar_t>(master_weight_elem);
      }

      // write back
      // update exp_avg
      exp_avg_vec[id] = temp_exp_avg;

      // update exp_avg_sq
      exp_avg_sq_vec[id] = temp_exp_avg_sq;

      // update master weight fp32
      master_weight_vec[id] = temp_master_weight;

      // update real weight bf16/fp16
      weight_vec[id] = temp_weight;
    }
  }
  LaunchVecKernelAdamWMasterWeightFunctor(
      const bool amsgrad_,
      const float exp_avg_ele_coefficient_,
      const float exp_avg_sq_ele_coefficient_,
      const float beta1_value_,
      const float beta2_value_,
      const float bias_correction1_,
      const float bias_correction2_,
      const float step_size_,
      const float stepweight_decay_,
      const float eps_value_,
      const int64_t total_element_,
      const int64_t global_range_,
      float* master_weight_ptr_,
      scalar_t* weight_ptr_,
      scalar_t* grad_ptr_,
      float* exp_avg_ptr_,
      float* exp_avg_sq_ptr_,
      float* max_exp_avg_sq_ptr_,
      vec_t* grad_vec_,
      vec_t* weight_vec_,
      vec_mw_t* master_weight_vec_,
      vec_mw_t* exp_avg_vec_,
      vec_mw_t* exp_avg_sq_vec_,
      vec_mw_t* max_exp_avg_sq_vec_)
      : amsgrad(amsgrad_),
        exp_avg_ele_coefficient(exp_avg_ele_coefficient_),
        exp_avg_sq_ele_coefficient(exp_avg_sq_ele_coefficient_),
        beta1_value(beta1_value_),
        beta2_value(beta2_value_),
        bias_correction1(bias_correction1_),
        bias_correction2(bias_correction2_),
        step_size(step_size_),
        stepweight_decay(stepweight_decay_),
        eps_value(eps_value_),
        total_element(total_element_),
        global_range(global_range_),
        master_weight_ptr(master_weight_ptr_),
        weight_ptr(weight_ptr_),
        grad_ptr(grad_ptr_),
        exp_avg_ptr(exp_avg_ptr_),
        exp_avg_sq_ptr(exp_avg_sq_ptr_),
        max_exp_avg_sq_ptr(max_exp_avg_sq_ptr_),
        grad_vec(grad_vec_),
        weight_vec(weight_vec_),
        master_weight_vec(master_weight_vec_),
        exp_avg_vec(exp_avg_vec_),
        exp_avg_sq_vec(exp_avg_sq_vec_),
        max_exp_avg_sq_vec(max_exp_avg_sq_vec_) {}

 private:
  const bool amsgrad;
  const float exp_avg_ele_coefficient;
  const float exp_avg_sq_ele_coefficient;
  const float beta1_value;
  const float beta2_value;
  const float bias_correction1;
  const float bias_correction2;
  const float step_size;
  const float stepweight_decay;
  const float eps_value;
  const int64_t total_element;
  const int64_t global_range;
  float* master_weight_ptr;
  scalar_t* weight_ptr;
  scalar_t* grad_ptr;
  float* exp_avg_ptr;
  float* exp_avg_sq_ptr;
  float* max_exp_avg_sq_ptr;
  vec_t* grad_vec;
  vec_t* weight_vec;
  vec_mw_t* master_weight_vec;
  vec_mw_t* exp_avg_vec;
  vec_mw_t* exp_avg_sq_vec;
  vec_mw_t* max_exp_avg_sq_vec;
};

template <int vec_size, typename scalar_t>
void launch_vec_kernel_AdamWMasterWeight(
    Tensor& master_weight,
    Tensor& weight,
    Tensor& grad,
    Tensor& avg,
    Tensor& avg_sq,
    Tensor& max_avg_sq,
    const bool amsgrad,
    const float exp_avg_ele_coefficient,
    const float exp_avg_sq_ele_coefficient,
    const float beta1_value,
    const float beta2_value,
    const float bias_correction1,
    const float bias_correction2,
    const float step_size,
    const float stepweight_decay,
    const float eps_value,
    const int64_t total_element,
    const int64_t global_range) {
  auto& queue = dpcppGetCurrentQueue();

  auto master_weight_ptr = master_weight.data_ptr<float>();
  auto weight_ptr = weight.data_ptr<scalar_t>();
  auto grad_ptr = grad.data_ptr<scalar_t>();
  auto exp_avg_ptr = avg.data_ptr<float>();
  auto exp_avg_sq_ptr = avg_sq.data_ptr<float>();
  auto max_exp_avg_sq_ptr = amsgrad ? max_avg_sq.data_ptr<float>() : nullptr;

  // vec_t is used for vectorization weight and grad
  using vec_t = at::native::Memory::aligned_vector_loop<scalar_t, vec_size>;
  // vec_mw_t is used for vectorization master weight, exp, exp_sq and
  // max_exp_sq
  using vec_mw_t = at::native::Memory::aligned_vector_loop<float, vec_size>;

  // cast grad, weight and other memory using vector
  vec_t* grad_vec = reinterpret_cast<vec_t*>(grad_ptr);
  vec_t* weight_vec = reinterpret_cast<vec_t*>(weight_ptr);
  vec_mw_t* master_weight_vec = reinterpret_cast<vec_mw_t*>(master_weight_ptr);
  vec_mw_t* exp_avg_vec = reinterpret_cast<vec_mw_t*>(exp_avg_ptr);
  vec_mw_t* exp_avg_sq_vec = reinterpret_cast<vec_mw_t*>(exp_avg_sq_ptr);

  // if amsgrad is false, max_exp_avg_sq_vec is nullptr
  vec_mw_t* max_exp_avg_sq_vec =
      amsgrad ? reinterpret_cast<vec_mw_t*>(max_exp_avg_sq_ptr) : nullptr;

  auto cgf = DPCPP_Q_CGF(cgh) {
    LaunchVecKernelAdamWMasterWeightFunctor<vec_size, scalar_t, vec_t, vec_mw_t>
        kfn(amsgrad,
            exp_avg_ele_coefficient,
            exp_avg_sq_ele_coefficient,
            beta1_value,
            beta2_value,
            bias_correction1,
            bias_correction2,
            step_size,
            stepweight_decay,
            eps_value,
            total_element,
            global_range,
            master_weight_ptr,
            weight_ptr,
            grad_ptr,
            exp_avg_ptr,
            exp_avg_sq_ptr,
            max_exp_avg_sq_ptr,
            grad_vec,
            weight_vec,
            master_weight_vec,
            exp_avg_vec,
            exp_avg_sq_vec,
            max_exp_avg_sq_vec);
    cgh.parallel_for<decltype(kfn)>(sycl::range<1>{global_range}, kfn);
  };

  DPCPP_Q_SUBMIT(queue, cgf);
}

template <int vec_size, typename vec_t>
struct LaunchVecKernelAdamWFunctor {
  void operator()(sycl::item<1> item) const {
    auto id = item.get_id(0);

    auto remaining = total_element - id * vec_size;

    // for handling remaining tail
    if (remaining < vec_size) {
      for (auto v_index = 0; v_index < remaining; ++v_index) {
        // kick out tail
        auto linear_idx = id * vec_size + v_index;
        // master weight grad should be fp32 to involve in computation to keep
        // acc.
        auto grad_elm = grad_ptr[linear_idx];
        auto weight_elem = weight_ptr[linear_idx];
        weight_elem = weight_elem * stepweight_decay;

        // exp_avg
        auto exp_avg_ele = exp_avg_ptr[linear_idx];
        exp_avg_ele =
            exp_avg_ele * beta1_value + grad_elm * exp_avg_ele_coefficient;
        exp_avg_ptr[linear_idx] = exp_avg_ele;

        // exp_avg_sq
        auto exp_avg_sq_ele = exp_avg_sq_ptr[linear_idx];
        exp_avg_sq_ele = exp_avg_sq_ele * beta2_value +
            grad_elm * grad_elm * exp_avg_sq_ele_coefficient;
        exp_avg_sq_ptr[linear_idx] = exp_avg_sq_ele;

        // amsgrad
        if (amsgrad) {
          max_exp_avg_sq_ptr[linear_idx] =
              max_exp_avg_sq_ptr[linear_idx] < exp_avg_sq_ele
              ? exp_avg_sq_ele
              : max_exp_avg_sq_ptr[linear_idx];
          weight_elem = weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(
                       max_exp_avg_sq_ptr[linear_idx] / bias_correction2) +
                   eps_value);
        } else {
          weight_elem = weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(exp_avg_sq_ele / bias_correction2) +
                   eps_value);
        }

        // update real weight
        weight_ptr[linear_idx] = static_cast<float>(weight_elem);
      }
    } else {
      // vector read
      vec_t weight_value = weight_vec[id];
      vec_t grad_value = grad_vec[id];
      vec_t exp_avg_value = exp_avg_vec[id];
      vec_t exp_avg_sq_value = exp_avg_sq_vec[id];

      // for vector write back
      vec_t temp_weight;
      vec_t temp_exp_avg;
      vec_t temp_exp_avg_sq;

#pragma unroll
      for (auto v_index = 0; v_index < vec_size; ++v_index) {
        auto grad_elm = grad_value[v_index];

        auto weight_elem = weight_value[v_index];
        weight_elem = weight_elem * stepweight_decay;

        // exp_avg
        auto exp_avg_ele = exp_avg_value[v_index];
        exp_avg_ele =
            exp_avg_ele * beta1_value + grad_elm * exp_avg_ele_coefficient;
        temp_exp_avg[v_index] = exp_avg_ele;

        // exp_avg_sq
        auto exp_avg_sq_ele = exp_avg_sq_value[v_index];
        exp_avg_sq_ele = exp_avg_sq_ele * beta2_value +
            grad_elm * grad_elm * exp_avg_sq_ele_coefficient;
        temp_exp_avg_sq[v_index] = exp_avg_sq_ele;

        // amsgrad
        if (amsgrad) {
          auto max_exp_avg_sq_ele = max_exp_avg_sq_vec[id][v_index];
          max_exp_avg_sq_ele = max_exp_avg_sq_ele < exp_avg_sq_ele
              ? static_cast<float>(exp_avg_sq_ele)
              : max_exp_avg_sq_ele;
          max_exp_avg_sq_vec[id][v_index] = max_exp_avg_sq_ele;
          weight_elem = weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(
                       max_exp_avg_sq_ele / bias_correction2) +
                   eps_value);
        } else {
          weight_elem = weight_elem -
              step_size * exp_avg_ele /
                  (Numerics<float>::sqrt(exp_avg_sq_ele / bias_correction2) +
                   eps_value);
        }

        // update real weight fp32
        temp_weight[v_index] = static_cast<float>(weight_elem);
      }

      // write back
      // update exp_avg
      exp_avg_vec[id] = temp_exp_avg;

      // update exp_avg_sq
      exp_avg_sq_vec[id] = temp_exp_avg_sq;

      // update weight fp32
      weight_vec[id] = temp_weight;
    }
  }
  LaunchVecKernelAdamWFunctor(
      const bool amsgrad_,
      const float exp_avg_ele_coefficient_,
      const float exp_avg_sq_ele_coefficient_,
      const float beta1_value_,
      const float beta2_value_,
      const float bias_correction1_,
      const float bias_correction2_,
      const float step_size_,
      const float stepweight_decay_,
      const float eps_value_,
      const int64_t total_element_,
      const int64_t global_range_,
      float* weight_ptr_,
      float* grad_ptr_,
      float* exp_avg_ptr_,
      float* exp_avg_sq_ptr_,
      float* max_exp_avg_sq_ptr_,
      vec_t* grad_vec_,
      vec_t* weight_vec_,
      vec_t* exp_avg_vec_,
      vec_t* exp_avg_sq_vec_,
      vec_t* max_exp_avg_sq_vec_)
      : amsgrad(amsgrad_),
        exp_avg_ele_coefficient(exp_avg_ele_coefficient_),
        exp_avg_sq_ele_coefficient(exp_avg_sq_ele_coefficient_),
        beta1_value(beta1_value_),
        beta2_value(beta2_value_),
        bias_correction1(bias_correction1_),
        bias_correction2(bias_correction2_),
        step_size(step_size_),
        stepweight_decay(stepweight_decay_),
        eps_value(eps_value_),
        total_element(total_element_),
        global_range(global_range_),
        weight_ptr(weight_ptr_),
        grad_ptr(grad_ptr_),
        exp_avg_ptr(exp_avg_ptr_),
        exp_avg_sq_ptr(exp_avg_sq_ptr_),
        max_exp_avg_sq_ptr(max_exp_avg_sq_ptr_),
        grad_vec(grad_vec_),
        weight_vec(weight_vec_),
        exp_avg_vec(exp_avg_vec_),
        exp_avg_sq_vec(exp_avg_sq_vec_),
        max_exp_avg_sq_vec(max_exp_avg_sq_vec_) {}

 private:
  const bool amsgrad;
  const float exp_avg_ele_coefficient;
  const float exp_avg_sq_ele_coefficient;
  const float beta1_value;
  const float beta2_value;
  const float bias_correction1;
  const float bias_correction2;
  const float step_size;
  const float stepweight_decay;
  const float eps_value;
  const int64_t total_element;
  const int64_t global_range;
  float* weight_ptr;
  float* grad_ptr;
  float* exp_avg_ptr;
  float* exp_avg_sq_ptr;
  float* max_exp_avg_sq_ptr;
  vec_t* grad_vec;
  vec_t* weight_vec;
  vec_t* exp_avg_vec;
  vec_t* exp_avg_sq_vec;
  vec_t* max_exp_avg_sq_vec;
};

// Here is the fused AdamW Kernel
// no master weight
template <int vec_size>
void launch_vec_kernel_AdamW(
    Tensor& weight,
    Tensor& grad,
    Tensor& avg,
    Tensor& avg_sq,
    Tensor& max_avg_sq,
    const bool amsgrad,
    const float exp_avg_ele_coefficient,
    const float exp_avg_sq_ele_coefficient,
    const float beta1_value,
    const float beta2_value,
    const float bias_correction1,
    const float bias_correction2,
    const float step_size,
    const float stepweight_decay,
    const float eps_value,
    const int64_t total_element,
    const int64_t global_range) {
  auto& queue = dpcppGetCurrentQueue();

  auto weight_ptr = weight.data_ptr<float>();
  auto grad_ptr = grad.data_ptr<float>();
  auto exp_avg_ptr = avg.data_ptr<float>();
  auto exp_avg_sq_ptr = avg_sq.data_ptr<float>();
  auto max_exp_avg_sq_ptr = amsgrad ? max_avg_sq.data_ptr<float>() : nullptr;

  // vec_t is used for vectorization weight and grad
  using vec_t = at::native::Memory::aligned_vector_loop<float, vec_size>;

  // cast grad, weight and other memory using vector
  vec_t* weight_vec = reinterpret_cast<vec_t*>(weight_ptr);
  vec_t* grad_vec = reinterpret_cast<vec_t*>(grad_ptr);
  vec_t* exp_avg_vec = reinterpret_cast<vec_t*>(exp_avg_ptr);
  vec_t* exp_avg_sq_vec = reinterpret_cast<vec_t*>(exp_avg_sq_ptr);

  // if amsgrad is false, max_exp_avg_sq_vec is nullptr
  vec_t* max_exp_avg_sq_vec =
      amsgrad ? reinterpret_cast<vec_t*>(max_exp_avg_sq_ptr) : nullptr;

  auto cgf = DPCPP_Q_CGF(cgh) {
    LaunchVecKernelAdamWFunctor<vec_size, vec_t> kfn(
        amsgrad,
        exp_avg_ele_coefficient,
        exp_avg_sq_ele_coefficient,
        beta1_value,
        beta2_value,
        bias_correction1,
        bias_correction2,
        step_size,
        stepweight_decay,
        eps_value,
        total_element,
        global_range,
        weight_ptr,
        grad_ptr,
        exp_avg_ptr,
        exp_avg_sq_ptr,
        max_exp_avg_sq_ptr,
        grad_vec,
        weight_vec,
        exp_avg_vec,
        exp_avg_sq_vec,
        max_exp_avg_sq_vec);
    cgh.parallel_for<decltype(kfn)>(sycl::range<1>{global_range}, kfn);
  };

  DPCPP_Q_SUBMIT(queue, cgf);
}

// Here is the AdamW Master Weight kernel
template <typename scalar_t>
static void ComputeAdamWKernelMasterWeight(
    Tensor& master_weight,
    Tensor& avg,
    Tensor& avg_sq,
    Tensor& max_avg_sq,
    Tensor& grad,
    Tensor& weight,
    const bool amsgrad,
    const float exp_avg_ele_coefficient,
    const float exp_avg_sq_ele_coefficient,
    const float beta1_value,
    const float beta2_value,
    const float bias_correction1,
    const float bias_correction2,
    const float step_size,
    const float stepweight_decay,
    const float eps_value) {
  auto& queue = dpcppGetCurrentQueue();

  auto vec_size_master_weight =
      at::native::Memory::can_vectorize_up_to_loop<float>(
          dpcppGetDeviceIdOfCurrentQueue(),
          reinterpret_cast<char*>(master_weight.data_ptr<float>()));

  auto vec_size_weight = at::native::Memory::can_vectorize_up_to_loop<scalar_t>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(weight.data_ptr<scalar_t>()));

  auto vec_size_grad = at::native::Memory::can_vectorize_up_to_loop<scalar_t>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(grad.data_ptr<scalar_t>()));

  auto vec_size_avg = at::native::Memory::can_vectorize_up_to_loop<float>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(avg.data_ptr<float>()));

  auto vec_size_avg_sq = at::native::Memory::can_vectorize_up_to_loop<float>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(avg_sq.data_ptr<float>()));

  auto vec_size_max_avg_sq = vec_size_avg_sq;
  if (amsgrad) {
    vec_size_max_avg_sq = at::native::Memory::can_vectorize_up_to_loop<float>(
        dpcppGetDeviceIdOfCurrentQueue(),
        reinterpret_cast<char*>(max_avg_sq.data_ptr<float>()));
  }

  auto vec_size = vec_size_master_weight;
  if (!master_weight.is_non_overlapping_and_dense() ||
      !weight.is_non_overlapping_and_dense() ||
      !grad.is_non_overlapping_and_dense() ||
      !avg.is_non_overlapping_and_dense() ||
      !avg_sq.is_non_overlapping_and_dense() ||
      (amsgrad && !max_avg_sq.is_non_overlapping_and_dense())) {
    vec_size = 1;
  } else {
    vec_size = std::min(
        {vec_size_master_weight,
         vec_size_weight,
         vec_size_grad,
         vec_size_avg,
         vec_size_avg_sq,
         vec_size_max_avg_sq});
  }

  auto total_element = master_weight.numel();

  auto global_range = (total_element % vec_size == 0)
      ? (total_element / vec_size)
      : (total_element / vec_size + 1);

// launch vector kernel for AdamW, code pass according to vector size
#define VEC_ADAMWMW_KERNEL(vec_size)                         \
  {                                                          \
    launch_vec_kernel_AdamWMasterWeight<vec_size, scalar_t>( \
        master_weight,                                       \
        weight,                                              \
        grad,                                                \
        avg,                                                 \
        avg_sq,                                              \
        max_avg_sq,                                          \
        amsgrad,                                             \
        exp_avg_ele_coefficient,                             \
        exp_avg_sq_ele_coefficient,                          \
        beta1_value,                                         \
        beta2_value,                                         \
        bias_correction1,                                    \
        bias_correction2,                                    \
        step_size,                                           \
        stepweight_decay,                                    \
        eps_value,                                           \
        total_element,                                       \
        global_range);                                       \
  }

  switch (vec_size) {
    case 8: {
      VEC_ADAMWMW_KERNEL(8);
      break;
    }
    case 4: {
      VEC_ADAMWMW_KERNEL(4);
      break;
    }
    case 2: {
      VEC_ADAMWMW_KERNEL(2);
      break;
    }
    case 1: {
      VEC_ADAMWMW_KERNEL(1);
      break;
    }
    default:
      TORCH_INTERNAL_ASSERT(
          false,
          "Unexpected vectorization size for AdamW Master Weight kernel. vec size ",
          vec_size);
  }
#undef VEC_ADAMWMW_KERNEL
}

// no master weight, all tensors are fp32
static void ComputeAdamWKernel(
    Tensor& weight,
    Tensor& avg,
    Tensor& avg_sq,
    Tensor& max_avg_sq,
    Tensor& grad,
    const bool amsgrad,
    const float exp_avg_ele_coefficient,
    const float exp_avg_sq_ele_coefficient,
    const float beta1_value,
    const float beta2_value,
    const float bias_correction1,
    const float bias_correction2,
    const float step_size,
    const float stepweight_decay,
    const float eps_value) {
  auto& queue = dpcppGetCurrentQueue();

  auto vec_size_weight = at::native::Memory::can_vectorize_up_to_loop<float>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(weight.data_ptr<float>()));

  auto vec_size_grad = at::native::Memory::can_vectorize_up_to_loop<float>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(grad.data_ptr<float>()));

  auto vec_size_avg = at::native::Memory::can_vectorize_up_to_loop<float>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(avg.data_ptr<float>()));

  auto vec_size_avg_sq = at::native::Memory::can_vectorize_up_to_loop<float>(
      dpcppGetDeviceIdOfCurrentQueue(),
      reinterpret_cast<char*>(avg_sq.data_ptr<float>()));

  auto vec_size_max_avg_sq = vec_size_avg_sq;
  if (amsgrad) {
    vec_size_max_avg_sq = at::native::Memory::can_vectorize_up_to_loop<float>(
        dpcppGetDeviceIdOfCurrentQueue(),
        reinterpret_cast<char*>(max_avg_sq.data_ptr<float>()));
  }

  auto vec_size = vec_size_weight;
  if (!weight.is_non_overlapping_and_dense() ||
      !grad.is_non_overlapping_and_dense() ||
      !avg.is_non_overlapping_and_dense() ||
      !avg_sq.is_non_overlapping_and_dense() ||
      (amsgrad && !max_avg_sq.is_non_overlapping_and_dense())) {
    vec_size = 1;
  } else {
    vec_size = std::min(
        {vec_size_weight,
         vec_size_grad,
         vec_size_avg,
         vec_size_avg_sq,
         vec_size_max_avg_sq});
  }

  auto total_element = weight.numel();

  auto global_range = (total_element % vec_size == 0)
      ? (total_element / vec_size)
      : (total_element / vec_size + 1);

#define VEC_ADAMW_KERNEL(vec_size)     \
  {                                    \
    launch_vec_kernel_AdamW<vec_size>( \
        weight,                        \
        grad,                          \
        avg,                           \
        avg_sq,                        \
        max_avg_sq,                    \
        amsgrad,                       \
        exp_avg_ele_coefficient,       \
        exp_avg_sq_ele_coefficient,    \
        beta1_value,                   \
        beta2_value,                   \
        bias_correction1,              \
        bias_correction2,              \
        step_size,                     \
        stepweight_decay,              \
        eps_value,                     \
        total_element,                 \
        global_range);                 \
  }

  switch (vec_size) {
    case 4: {
      VEC_ADAMW_KERNEL(4);
      break;
    }
    case 2: {
      VEC_ADAMW_KERNEL(2);
      break;
    }
    case 1: {
      VEC_ADAMW_KERNEL(1);
      break;
    }
    default:
      TORCH_INTERNAL_ASSERT(
          false,
          "Unexpected vectorization size for AdamW kernel. vec size ",
          vec_size);
  }
#undef VEC_ADAMW_KERNEL
} // namespace AtenIpexTypeXPU
} // namespace impl

// fusing adamw kernel and using vector load/store
void adamw_fused_step(
    at::Tensor& param_,
    at::Tensor& exp_avg_,
    at::Tensor& exp_avg_sq_,
    at::Tensor& max_exp_avg_sq_,
    at::Tensor& grad_,
    at::Tensor& param2_,
    const bool amsgrad,
    const double step,
    const double beta1,
    const double beta2,
    const double learning_rate,
    const double weight_decay,
    const double eps) {
  TORCH_CHECK(
      learning_rate >= 0, "Expect learning rate >= 0.0, got ", learning_rate);
  TORCH_CHECK(eps >= 0, "Expect eps >= 0.0, got ", eps);
  TORCH_CHECK(beta1 >= 0 && beta1 < 1, "Expect 0.0 <= beta1 < 1.0, got", beta1);
  TORCH_CHECK(beta2 >= 0 && beta2 < 1, "Expect 0.0 <= beta2 < 1.0, got", beta2);
  TORCH_CHECK(
      weight_decay >= 0, "Expect weight_decay >= 0.0, got ", weight_decay);

  TORCH_CHECK(
      param_.sizes() == grad_.sizes(),
      "Expect param and grad have the same sizes, param sizes: ",
      param_.sizes(),
      "; grad sizes: ",
      grad_.sizes());
  TORCH_CHECK(
      param_.sizes() == exp_avg_.sizes(),
      "Expect param and exp_avg have the same sizes, param sizes: ",
      param_.sizes(),
      "; exp_avg sizes: ",
      exp_avg_.sizes());
  TORCH_CHECK(
      param_.sizes() == exp_avg_sq_.sizes(),
      "Expect param and exp_avg_sq_ have the same sizes, param sizes: ",
      param_.sizes(),
      "; exp_avg_sq sizes: ",
      exp_avg_sq_.sizes());
  if (amsgrad) {
    TORCH_CHECK(
        param_.sizes() == max_exp_avg_sq_.sizes(),
        "Expect param and max_exp_avg_sq_ have the same sizes, param sizes: ",
        param_.sizes(),
        "; max_exp_avg_sq sizes: ",
        max_exp_avg_sq_.sizes());
  }
  TORCH_CHECK(
      param2_.numel() == 0 || param_.sizes() == param2_.sizes(),
      "Expect param and param2_ have the same sizes, param sizes: ",
      param_.sizes(),
      "; param2_ sizes: ",
      param2_.sizes());
  RECORD_FUNCTION(
      "adamw_fused_step",
      std::vector<c10::IValue>(
          {param_, exp_avg_, exp_avg_sq_, max_exp_avg_sq_, grad_, param2_}));
  const OptionalDeviceGuard device_guard(device_of(param_));

  // after inference, the model weight in the next training epoch maybe cached
  // in block layout, so to plain now if needed
  param_ = to_plain_if_needed_(param_);
  grad_ = to_plain_if_needed_(grad_);

  // support contiguous and channels_last contiguous
  auto memory_format = param_.suggest_memory_format();
  param_ = param_.contiguous(memory_format);
  exp_avg_ = exp_avg_.contiguous(memory_format);
  exp_avg_sq_ = exp_avg_sq_.contiguous(memory_format);
  if (amsgrad) {
    max_exp_avg_sq_ = max_exp_avg_sq_.contiguous(memory_format);
  }
  grad_ = grad_.contiguous(memory_format);

  // pre calculate scalar on host side
  const auto beta1_value = static_cast<float>(beta1);
  const auto beta2_value = static_cast<float>(beta2);
  const auto exp_avg_ele_coefficient = static_cast<float>(1.0 - beta1_value);
  const auto exp_avg_sq_ele_coefficient = static_cast<float>(1.0 - beta2_value);
  const auto bias_correction1 =
      static_cast<float>(1.0 - std::pow(beta1_value, step));
  const auto bias_correction2 =
      static_cast<float>(1.0 - std::pow(beta2_value, step));
  const auto step_size = static_cast<float>(learning_rate / bias_correction1);
  const auto stepweight_decay =
      static_cast<float>(1.0 - learning_rate * weight_decay);
  const auto eps_value = static_cast<float>(eps);

  if (param2_.numel()) {
    // master weight mode
    param2_ = to_plain_if_needed_(param2_);
    param2_ = param2_.contiguous(memory_format);
    IPEX_DISPATCH_FLOATING_TYPES_AND2(
        at::ScalarType::Half,
        at::ScalarType::BFloat16,
        param2_.scalar_type(),
        "adamw_fused_step",
        [&] {
          impl::ComputeAdamWKernelMasterWeight<scalar_t>(
              param_,
              exp_avg_,
              exp_avg_sq_,
              max_exp_avg_sq_,
              grad_,
              param2_,
              amsgrad,
              exp_avg_ele_coefficient,
              exp_avg_sq_ele_coefficient,
              beta1_value,
              beta2_value,
              bias_correction1,
              bias_correction2,
              step_size,
              stepweight_decay,
              eps_value);
        });
  } else {
    // normal mode, no master weight, all tensors are fp32
    impl::ComputeAdamWKernel(
        param_,
        exp_avg_,
        exp_avg_sq_,
        max_exp_avg_sq_,
        grad_,
        amsgrad,
        exp_avg_ele_coefficient,
        exp_avg_sq_ele_coefficient,
        beta1_value,
        beta2_value,
        bias_correction1,
        bias_correction2,
        step_size,
        stepweight_decay,
        eps_value);
  }
}

void _fused_adamw_impl_(
    at::TensorList params,
    at::TensorList grads,
    at::TensorList exp_avgs,
    at::TensorList exp_avg_sqs,
    at::TensorList state_steps,
    const double lr,
    const double beta1,
    const double beta2,
    const double weight_decay,
    const double eps,
    const bool maximize,
    const c10::optional<at::Tensor>& grad_scale,
    const c10::optional<at::Tensor>& found_inf) {
  std::vector<std::vector<at::Tensor>> tensor_lists{
      params.vec(), grads.vec(), exp_avgs.vec(), exp_avg_sqs.vec()};

  if (grad_scale.has_value())
    TORCH_WARN(" grad_scale not supported with ipex fused_adam.")
  if (found_inf.has_value() && found_inf->data_ptr<float>()[0] == true)
    TORCH_WARN(" found_inf not supported with ipex fused_adam.")

  const float beta1_value = static_cast<float>(beta1);
  const float beta2_value = static_cast<float>(beta2);
  const float weight_decay_value = static_cast<float>(weight_decay);
  const float eps_value = static_cast<float>(eps);

  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      kHalf, kBFloat16, params[0].scalar_type(), "fused_adam_kernel", [&]() {
        multi_tensor_apply_for_fused_optimizer<4>(
            tensor_lists,
            state_steps,
            FusedAdamMathFunctor<scalar_t, 4, ADAM_MODE::ADAMW, false>(),
            (const float*)nullptr,
            lr,
            beta1_value,
            beta2_value,
            weight_decay_value,
            eps_value,
            maximize);
      });
}

void _fused_adamw_amsgrad_impl_(
    at::TensorList params,
    at::TensorList grads,
    at::TensorList exp_avgs,
    at::TensorList exp_avg_sqs,
    at::TensorList max_exp_avg_sqs,
    at::TensorList state_steps,
    const double lr,
    const double beta1,
    const double beta2,
    const double weight_decay,
    const double eps,
    const bool maximize,
    const c10::optional<at::Tensor>& grad_scale,
    const c10::optional<at::Tensor>& found_inf) {
  std::vector<std::vector<at::Tensor>> tensor_lists{
      params.vec(),
      grads.vec(),
      exp_avgs.vec(),
      exp_avg_sqs.vec(),
      max_exp_avg_sqs.vec()};

  if (grad_scale.has_value())
    TORCH_WARN(" grad_scale not supported with ipex fused_adam.")
  if (found_inf.has_value() && found_inf->data_ptr<float>()[0] == true)
    TORCH_WARN(" found_inf not supported with ipex fused_adam.")

  const float beta1_value = static_cast<float>(beta1);
  const float beta2_value = static_cast<float>(beta2);
  const float weight_decay_value = static_cast<float>(weight_decay);
  const float eps_value = static_cast<float>(eps);

  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      kHalf,
      kBFloat16,
      params[0].scalar_type(),
      "fused_adam_amsgrad_kernel",
      [&]() {
        multi_tensor_apply_for_fused_optimizer<5>(
            tensor_lists,
            state_steps,
            FusedAdamMathFunctor<scalar_t, 5, ADAM_MODE::ADAMW, true>(),
            (const float*)nullptr,
            lr,
            beta1_value,
            beta2_value,
            weight_decay_value,
            eps_value,
            maximize);
      });
}

void _fused_adamw_impl_(
    at::TensorList params,
    at::TensorList grads,
    at::TensorList exp_avgs,
    at::TensorList exp_avg_sqs,
    at::TensorList state_steps,
    const Tensor& lr,
    const double beta1,
    const double beta2,
    const double weight_decay,
    const double eps,
    const bool maximize,
    const c10::optional<at::Tensor>& grad_scale,
    const c10::optional<at::Tensor>& found_inf) {
  std::vector<std::vector<at::Tensor>> tensor_lists{
      params.vec(), grads.vec(), exp_avgs.vec(), exp_avg_sqs.vec()};

  if (grad_scale.has_value())
    TORCH_WARN(" grad_scale not supported with ipex fused_adam.")
  if (found_inf.has_value() && found_inf->data_ptr<float>()[0] == true)
    TORCH_WARN(" found_inf not supported with ipex fused_adam.")

  const float beta1_value = static_cast<float>(beta1);
  const float beta2_value = static_cast<float>(beta2);
  const float weight_decay_value = static_cast<float>(weight_decay);
  const float eps_value = static_cast<float>(eps);
  float* lr_ptr = lr.data_ptr<float>();

  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      kHalf, kBFloat16, params[0].scalar_type(), "fused_adam_kernel", [&]() {
        multi_tensor_apply_for_fused_optimizer<4>(
            tensor_lists,
            state_steps,
            FusedAdamMathFunctor<scalar_t, 4, ADAM_MODE::ADAMW, false>(),
            lr_ptr,
            0.0f,
            beta1_value,
            beta2_value,
            weight_decay_value,
            eps_value,
            maximize);
      });
}

void _fused_adamw_amsgrad_impl_(
    at::TensorList params,
    at::TensorList grads,
    at::TensorList exp_avgs,
    at::TensorList exp_avg_sqs,
    at::TensorList max_exp_avg_sqs,
    at::TensorList state_steps,
    const Tensor& lr,
    const double beta1,
    const double beta2,
    const double weight_decay,
    const double eps,
    const bool maximize,
    const c10::optional<at::Tensor>& grad_scale,
    const c10::optional<at::Tensor>& found_inf) {
  std::vector<std::vector<at::Tensor>> tensor_lists{
      params.vec(),
      grads.vec(),
      exp_avgs.vec(),
      exp_avg_sqs.vec(),
      max_exp_avg_sqs.vec()};

  if (grad_scale.has_value())
    TORCH_WARN(" grad_scale not supported with ipex fused_adam.")
  if (found_inf.has_value() && found_inf->data_ptr<float>()[0] == true)
    TORCH_WARN(" found_inf not supported with ipex fused_adam.")

  const float beta1_value = static_cast<float>(beta1);
  const float beta2_value = static_cast<float>(beta2);
  const float weight_decay_value = static_cast<float>(weight_decay);
  const float eps_value = static_cast<float>(eps);
  float* lr_ptr = lr.data_ptr<float>();

  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      kHalf,
      kBFloat16,
      params[0].scalar_type(),
      "fused_adam_amsgrad_kernel",
      [&]() {
        multi_tensor_apply_for_fused_optimizer<5>(
            tensor_lists,
            state_steps,
            FusedAdamMathFunctor<scalar_t, 5, ADAM_MODE::ADAMW, true>(),
            lr_ptr,
            0.0f,
            beta1_value,
            beta2_value,
            weight_decay_value,
            eps_value,
            maximize);
      });
}

// overload with tensor lr(single element tensor) input
void _fused_adamw_(
    at::TensorList params,
    at::TensorList grads,
    at::TensorList exp_avgs,
    at::TensorList exp_avg_sqs,
    at::TensorList max_exp_avg_sqs,
    at::TensorList state_steps,
    const Tensor& lr,
    const double beta1,
    const double beta2,
    const double weight_decay,
    const double eps,
    const bool amsgrad,
    const bool maximize,
    const c10::optional<at::Tensor>& grad_scale,
    const c10::optional<at::Tensor>& found_inf) {
  if (lr.is_cpu()) {
    _fused_adamw_(
        params,
        grads,
        exp_avgs,
        exp_avg_sqs,
        max_exp_avg_sqs,
        state_steps,
        lr.item<double>(),
        beta1,
        beta2,
        weight_decay,
        eps,
        amsgrad,
        maximize,
        grad_scale,
        found_inf);
    return;
  }
  Device param_device = params[0].device();
  TORCH_CHECK(
      lr.device() == param_device,
      "lr must be on the same GPU device as the params");
  if (amsgrad) {
    TORCH_CHECK(
        at::native::check_fast_path_restrictions(
            {params, grads, exp_avgs, exp_avg_sqs, max_exp_avg_sqs}),
        "params, grads, exp_avgs, exp_avg_sqs, and max_exp_avg_sqs must have same dtype, device, and layout");
    _fused_adamw_amsgrad_impl_(
        params,
        grads,
        exp_avgs,
        exp_avg_sqs,
        max_exp_avg_sqs,
        state_steps,
        lr,
        beta1,
        beta2,
        weight_decay,
        eps,
        maximize,
        grad_scale,
        found_inf);
  } else {
    TORCH_CHECK(
        at::native::check_fast_path_restrictions(
            {params, grads, exp_avgs, exp_avg_sqs}),
        "params, grads, exp_avgs, and exp_avg_sqs must have same dtype, device, and layout");
    _fused_adamw_impl_(
        params,
        grads,
        exp_avgs,
        exp_avg_sqs,
        state_steps,
        lr,
        beta1,
        beta2,
        weight_decay,
        eps,
        maximize,
        grad_scale,
        found_inf);
  }
}

void _fused_adamw_(
    at::TensorList params,
    at::TensorList grads,
    at::TensorList exp_avgs,
    at::TensorList exp_avg_sqs,
    at::TensorList max_exp_avg_sqs,
    at::TensorList state_steps,
    const double lr,
    const double beta1,
    const double beta2,
    const double weight_decay,
    const double eps,
    const bool amsgrad,
    const bool maximize,
    const c10::optional<at::Tensor>& grad_scale,
    const c10::optional<at::Tensor>& found_inf) {
  if (amsgrad) {
    TORCH_CHECK(
        at::native::check_fast_path_restrictions(
            {params, grads, exp_avgs, exp_avg_sqs, max_exp_avg_sqs}),
        "params, grads, exp_avgs, exp_avg_sqs, and max_exp_avg_sqs must have same dtype, device, and layout");
    _fused_adamw_amsgrad_impl_(
        params,
        grads,
        exp_avgs,
        exp_avg_sqs,
        max_exp_avg_sqs,
        state_steps,
        lr,
        beta1,
        beta2,
        weight_decay,
        eps,
        maximize,
        grad_scale,
        found_inf);
  } else {
    TORCH_CHECK(
        at::native::check_fast_path_restrictions(
            {params, grads, exp_avgs, exp_avg_sqs}),
        "params, grads, exp_avgs, and exp_avg_sqs must have same dtype, device, and layout");
    _fused_adamw_impl_(
        params,
        grads,
        exp_avgs,
        exp_avg_sqs,
        state_steps,
        lr,
        beta1,
        beta2,
        weight_decay,
        eps,
        maximize,
        grad_scale,
        found_inf);
  }
}

} // namespace AtenIpexTypeXPU
} // namespace at

namespace {
TORCH_LIBRARY_FRAGMENT(torch_ipex, m) {
  m.def(
      "adamw_fused_step(Tensor param_, Tensor exp_avg_, Tensor exp_avg_sq_, Tensor max_exp_avg_sq_, "
      "Tensor grad_, Tensor param2_, bool amsgrad, float step, float beta1, float beta2, "
      "float learning_rate, float weight_decay, float eps) -> ()");
  m.impl(
      "adamw_fused_step",
      c10::DispatchKey::XPU,
      at::AtenIpexTypeXPU::adamw_fused_step);
}
} // namespace
