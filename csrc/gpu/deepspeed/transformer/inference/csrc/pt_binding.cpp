/*******************************************************************************
 * Copyright 2016-2024 int64_tel Corporation
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *******************************************************************************/

#include <sycl/sycl.hpp>
#include <cmath>
#include <stdexcept>
#include <vector>
#include "context.h"
#include "inference_mkl_wrappers.h"
#include "inference_sycl_layers.h"

#include <ATen/ATen.h>

std::array<int64_t, 3> gemm_algos = std::array<int64_t, 3>({99, 99, 99});

// NOTE: This activation function type enum should be always in sync
// with the python counterpart, otherwise the casting from python binding
// will be incorrect.
enum class ActivationFuncType {
  UNKNOWN = 0,
  GELU = 1,
  ReLU = 2,
  GATED_GELU = 3,
  GATED_SILU = 4
};

enum class NormType { UNKNOWN = 0, LayerNorm = 1, GroupNorm = 2, RMSNorm = 3 };

enum class TransformerType : uint8_t { UNKNOWN = 0, GPTType = 1, BERTType = 2 };

// NOTE: this is a temporary and dodgy solution to distinguish GPT and BERT
// style models based on the dimensions of the corresponding attention mask.
inline auto infer_transformer_type(at::Tensor& attn_mask) -> TransformerType {
  auto attn_mask_num_dims = attn_mask.sizes().size();

  if (attn_mask_num_dims > 2) {
    return TransformerType::GPTType;
  } else if (attn_mask_num_dims == 2) {
    return TransformerType::BERTType;
  } else {
    return TransformerType::UNKNOWN;
  }
}

// infer stride of attention mask memory layout based on the model type.
inline auto get_attn_mask_stride(at::Tensor& attn_mask) -> int {
  auto trnsfrmr_type = infer_transformer_type(attn_mask);

  if (trnsfrmr_type == TransformerType::GPTType) {
    return attn_mask.size(2);
  } else if (trnsfrmr_type == TransformerType::BERTType) {
    // Bert style models have always a mask stride of 1.
    return 1;
  } else if (trnsfrmr_type == TransformerType::UNKNOWN) {
    return 0;
  }

  // this is just to make the compiler happy.
  return 0;
}

template <typename T>
at::Tensor ds_softmax(
    at::Tensor& attn_scores,
    at::Tensor& attn_mask,
    at::Tensor& alibi,
    bool triangular,
    bool recompute,
    bool local_attention,
    int64_t window_size,
    bool async_op,
    double layer_scale,
    int64_t head_offset,
    int64_t mp_size) {
  auto attn_scores_c = attn_scores.contiguous();
  int64_t bsz = attn_scores_c.size(0);

  int64_t seq_len = attn_scores_c.size(1);
  int64_t len = attn_scores_c.sizes().size();
  if (len > 2)
    seq_len = attn_scores_c.size(2);

  int64_t soft_len = attn_scores_c.size(2);
  if (len > 3)
    soft_len = attn_scores_c.size(3);

  int64_t heads = 1;
  if (len > 1)
    heads = attn_scores_c.size(1);

  auto mask_stride = get_attn_mask_stride(attn_mask);

  launch_attn_softmax_v2(
      (T*)attn_scores_c.data_ptr(),
      (attn_mask.sizes().size() > 1 ? (T*)attn_mask.data_ptr() : nullptr),
      (alibi.sizes().size() > 1 ? (T*)alibi.data_ptr() : nullptr),
      layer_scale,
      triangular,
      recompute,
      local_attention,
      window_size,
      bsz,
      heads,
      seq_len,
      soft_len,
      head_offset,
      mask_stride,
      mp_size,
      InferenceContext::Instance().GetCurrentStream(async_op));

  return attn_scores_c;
}

template <typename T>
void allocate_workspace(
    int64_t hidden_dim,
    int64_t num_heads,
    int64_t prompt_length,
    int64_t batch_size,
    int64_t num_layers,
    int64_t mp_size = 1,
    bool external_cache = false,
    int64_t rank = 0,
    int64_t max_out_tokens = 1024,
    int64_t min_out_tokens = 1) {
  InferenceContext::Instance().GenWorkSpace(
      num_layers,
      num_heads,
      batch_size,
      prompt_length,
      hidden_dim,
      mp_size,
      external_cache,
      sizeof(T),
      rank,
      max_out_tokens,
      min_out_tokens);
}

template <typename T>
at::Tensor einsum_sec_sm_ecm(at::Tensor& Q, at::Tensor& W) {
  auto options = at::TensorOptions()
                     .dtype(Q.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  float alpha = 1;
  float gemm_beta = 0.0;

  /*
  // Reallocate memory if we received a new prompt
  if (!workspace || input.size(1) != 1) {
      allocate_workspace<T>(W.size(1),
  InferenceContext::Instance().GetMaxTokenLength(), Q.size(0), 1, head_size);
  workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  }
  */

  auto O = at::from_blob(
      workspace,
      {Q.size(1), Q.size(2), W.size(1)},
      c10::TensorType::contiguousStridesOf({Q.size(1), Q.size(2), W.size(1)}),
      nullptr,
      options,
      Q.device());
  int64_t m = W.size(1);
  int64_t n = Q.size(1) * Q.size(2);
  int64_t k = Q.size(0);
  mkl_gemm_ex(
      InferenceContext::Instance().GetCublasHandle(),
      oneapi::mkl::transpose::nontrans,
      oneapi::mkl::transpose::trans,
      m,
      n,
      k,
      &alpha,
      &gemm_beta,
      (T*)W.data_ptr(),
      (T*)Q.data_ptr(),
      (T*)O.data_ptr(),
      99);
  return O;
}

template <typename T>
void attention_unfused(
    at::Tensor& prev_key_cont,
    at::Tensor& query_cont,
    at::Tensor& attn_mask,
    at::Tensor& prev_value_cont,
    at::Tensor& output,
    int64_t& bsz,
    int64_t& seq_len,
    int64_t& soft_len,
    int64_t& heads,
    double& norm_factor,
    bool triangular,
    bool recompute,
    bool local_attention,
    int64_t window_size) {
  auto options = at::TensorOptions()
                     .dtype(query_cont.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  float alpha = norm_factor;
  float gemm_beta = 0.0;
  auto attn_score = at::empty({bsz, heads, seq_len, soft_len}, options);
  int64_t k = prev_value_cont.size(2) / heads;

  auto mask_stride = get_attn_mask_stride(attn_mask);

  *(InferenceContext::Instance().GetCublasHandle()) =
      *(InferenceContext::Instance().GetCurrentStream());
  mkl_strided_batched_gemm(
      InferenceContext::Instance().GetCublasHandle(),
      soft_len,
      seq_len,
      k,
      &alpha,
      &gemm_beta,
      (T*)prev_key_cont.data_ptr(),
      (T*)query_cont.data_ptr(),
      (T*)attn_score.data_ptr(),
      oneapi::mkl::transpose::nontrans,
      oneapi::mkl::transpose::nontrans,
      soft_len * k,
      seq_len * k,
      seq_len * soft_len,
      bsz * heads,
      99);
  launch_attn_softmax_v2(
      (T*)attn_score.data_ptr(),
      (T*)(attn_mask.sizes().size() > 1 ? attn_mask.data_ptr() : nullptr),
      (T*)nullptr,
      1.0,
      triangular,
      recompute,
      local_attention,
      window_size,
      bsz,
      heads,
      seq_len,
      soft_len,
      0,
      mask_stride,
      1,
      InferenceContext::Instance().GetCurrentStream(false));
  alpha = 1.0;
  mkl_strided_batched_gemm(
      InferenceContext::Instance().GetCublasHandle(),
      k,
      seq_len,
      soft_len,
      &alpha,
      &gemm_beta,
      (T*)prev_value_cont.data_ptr(),
      (T*)attn_score.data_ptr(),
      (T*)output.data_ptr(),
      oneapi::mkl::transpose::nontrans,
      oneapi::mkl::transpose::nontrans,
      soft_len * k,
      seq_len * soft_len,
      seq_len * k,
      bsz * heads,
      99);
}

template <typename T>
std::vector<at::Tensor> ds_softmax_context1(
    at::Tensor& query,
    at::Tensor& prev_key,
    at::Tensor& new_key,
    at::Tensor& attn_mask,
    at::Tensor& prev_value,
    at::Tensor& new_value,
    int64_t heads,
    double norm_factor,
    bool merging,
    bool triangular,
    bool local_attention,
    int64_t window_size,
    bool no_masking) {
  auto query_cont = query.contiguous();
  auto prev_key_cont = prev_key.contiguous();
  auto prev_value_cont = prev_value.contiguous();

  int64_t new_size = (new_value.sizes().size() > 1 ? new_value.size(1) : 0);

  // Attn_Score [ batch Head Sequence-length Softmax-length]

  int64_t bsz = query_cont.size(0);
  int64_t seq_len = query_cont.size(1);
  int64_t soft_len = prev_value.size(1);

  auto options = at::TensorOptions()
                     .dtype(query_cont.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  auto output = at::empty(
      {prev_value.size(0), heads, seq_len, prev_value.size(2) / heads},
      options);
  attention_unfused<T>(
      prev_key_cont,
      query_cont,
      attn_mask, //(no_masking ? nullptr : (T*)attn_mask.data_ptr()),
      prev_value_cont,
      output,
      bsz,
      seq_len,
      soft_len,
      heads,
      norm_factor,
      (triangular && (new_size == 0)),
      (new_size == 0),
      local_attention,
      window_size);

  return {output, prev_key, prev_value};
}

template <typename T>
void ds_softmax_internal(
    T* attn_scores,
    at::Tensor& attn_mask,
    at::Tensor& alibi,
    float& layer_scale,
    bool triangular,
    bool recompute,
    bool local_attention,
    int64_t window_size,
    int64_t bsz,
    int64_t seq_len,
    int64_t soft_len,
    int64_t heads) {
  auto mask_stride = get_attn_mask_stride(attn_mask);

  launch_attn_softmax_v2(
      (T*)attn_scores,
      (attn_mask.sizes().size() > 1 ? (T*)attn_mask.data_ptr() : nullptr),
      (alibi.sizes().size() > 1 ? (T*)alibi.data_ptr() : nullptr),
      layer_scale,
      triangular,
      recompute,
      local_attention,
      window_size,
      bsz,
      heads,
      seq_len,
      soft_len,
      0,
      mask_stride,
      1,
      at::getCurrentSYCLStream());
}

template <typename T>
void attention_unfused(
    T* prev_key_cont,
    T* query_cont,
    at::Tensor& attn_mask,
    T* prev_value_cont,
    T* output,
    int64_t& bsz,
    int64_t& k,
    int64_t& seq_len,
    int64_t& soft_len,
    int64_t& heads,
    double& norm_factor,
    bool triangular,
    bool recompute,
    bool local_attention,
    int64_t window_size,
    at::Tensor& alibi,
    int64_t layer_id) {
  float layer_scale =
      alibi.sizes().size() > 1 ? std::max(1, (int)layer_id) : 1.0;
  float alpha = norm_factor * norm_factor / layer_scale;
  float gemm_beta = 0.0;
  T* workspace =
      (T*)InferenceContext::Instance().GetAttentionUnfusedWorkspace();

  *(InferenceContext::Instance().GetCublasHandle()) =
      *(InferenceContext::Instance().GetCurrentStream());
  mkl_strided_batched_gemm(
      InferenceContext::Instance().GetCublasHandle(),
      soft_len,
      seq_len,
      k,
      &alpha,
      &gemm_beta,
      (T*)prev_key_cont,
      (T*)query_cont,
      workspace,
      oneapi::mkl::transpose::trans,
      oneapi::mkl::transpose::nontrans,
      InferenceContext::Instance().GetMaxTokenLength() * k,
      seq_len * k,
      seq_len * soft_len,
      bsz * heads,
      99);
  ds_softmax_internal<T>(
      workspace,
      attn_mask,
      alibi,
      layer_scale,
      triangular,
      recompute,
      local_attention,
      window_size,
      bsz,
      seq_len,
      soft_len,
      heads);
  alpha = 1.0;
  mkl_strided_batched_gemm(
      InferenceContext::Instance().GetCublasHandle(),
      k,
      seq_len,
      soft_len,
      &alpha,
      &gemm_beta,
      (T*)prev_value_cont,
      workspace,
      (T*)output,
      oneapi::mkl::transpose::nontrans,
      oneapi::mkl::transpose::nontrans,
      InferenceContext::Instance().GetMaxTokenLength() * k,
      seq_len * soft_len,
      seq_len * k,
      bsz * heads,
      99);
}

void reset_cache() {
  InferenceContext::Instance().reset_tokens();
}

template <typename T>
std::vector<at::Tensor> ds_softmax_context(
    at::Tensor& query_key_value,
    at::Tensor& attn_mask,
    int64_t rotary_dim,
    bool rotate_half,
    bool rotate_every_two,
    int64_t heads,
    int64_t num_kv,
    double norm_factor,
    bool triangular,
    bool local_attention,
    int64_t window_size,
    bool no_masking,
    int64_t layer_id,
    int64_t num_layers,
    at::Tensor& alibi,
    double rope_theta) {
  int64_t bsz = query_key_value.size(0);
  int64_t seq_len = query_key_value.size(1);
  int64_t k =
      query_key_value.size(2) / (heads + 2 * (num_kv > 0 ? num_kv : heads));
  int64_t hidden_dim = heads * k;

  bool is_prompt = (seq_len > 1);

  if (is_prompt)
    InferenceContext::Instance().reset_tokens(seq_len);
  int64_t soft_len = InferenceContext::Instance().current_tokens();

  auto options = at::TensorOptions()
                     .dtype(query_key_value.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  size_t buf_size = bsz * seq_len * hidden_dim;
  auto output = at::from_blob(
      workspace + 4 * buf_size,
      {bsz, seq_len, hidden_dim},
      c10::TensorType::contiguousStridesOf({bsz, seq_len, hidden_dim}),
      nullptr,
      options,
      query_key_value.device());

  auto query_cont = workspace + 5 * buf_size;
  size_t offset = 10 *
          (hidden_dim * bsz *
           InferenceContext::Instance().GetMaxTokenLength()) +
      layer_id * 2 * bsz * InferenceContext::Instance().GetMaxTokenLength() *
          hidden_dim;
  int64_t all_tokens = soft_len;
  auto kv_cache = workspace + offset +
      (hidden_dim / heads) * (is_prompt ? 0 : soft_len - 1);
  size_t value_offset =
      bsz * InferenceContext::Instance().GetMaxTokenLength() * hidden_dim;

  T* temp_buf = (T*)output.data_ptr() + at::numel(output);
  launch_bias_add_transform_0213<T>(
      (T*)query_cont,
      kv_cache,
      kv_cache + value_offset,
      (T*)query_key_value.data_ptr(),
      nullptr,
      bsz,
      seq_len,
      (is_prompt ? 0 : soft_len - 1),
      soft_len,
      hidden_dim,
      heads,
      (num_kv > 0 ? num_kv : heads),
      rotary_dim,
      rotate_half,
      rotate_every_two,
      InferenceContext::Instance().GetCurrentStream(),
      3,
      InferenceContext::Instance().GetMaxTokenLength(),
      rope_theta);
  if (rotary_dim > 0 && rotate_half)
    launch_apply_rotary_pos_emb(
        query_cont,
        kv_cache,
        k,
        seq_len,
        rotary_dim,
        (is_prompt ? 0 : soft_len - 1),
        heads,
        bsz,
        rope_theta,
        InferenceContext::Instance().GetCurrentStream(),
        InferenceContext::Instance().GetMaxTokenLength());

  attention_unfused<T>(
      workspace + offset,
      (T*)query_cont,
      attn_mask,
      workspace + offset + value_offset,
      temp_buf,
      bsz,
      k,
      seq_len,
      all_tokens,
      heads,
      norm_factor,
      (triangular && is_prompt),
      is_prompt,
      local_attention,
      window_size,
      alibi,
      layer_id);
  launch_transform4d_0213<T>(
      (T*)output.data_ptr(),
      temp_buf,
      bsz,
      heads,
      seq_len,
      output.size(2),
      InferenceContext::Instance().GetCurrentStream(false),
      1);

  if (layer_id == num_layers - 1)
    InferenceContext::Instance().advance_tokens();
  auto prev_key = at::from_blob(
      workspace + offset,
      {bsz, heads, all_tokens, k},
      {hidden_dim * InferenceContext::Instance().GetMaxTokenLength(),
       k * InferenceContext::Instance().GetMaxTokenLength(),
       k,
       1},
      nullptr,
      options,
      query_key_value.device());

  auto prev_value = at::from_blob(
      workspace + offset + value_offset,
      {bsz, heads, all_tokens, k},
      {hidden_dim * InferenceContext::Instance().GetMaxTokenLength(),
       k * InferenceContext::Instance().GetMaxTokenLength(),
       k,
       1},
      nullptr,
      options,
      query_key_value.device());

  return {output, prev_key, prev_value};
}

template <typename T>
at::Tensor ds_bias_gelu(at::Tensor& input, at::Tensor& bias) {
  auto input_cont = input.contiguous();

  int64_t bsz = input_cont.size(0) * input_cont.size(1);
  int64_t intermediate_size = input_cont.size(2);

  launch_bias_gelu(
      (T*)input_cont.data_ptr(),
      (T*)bias.data_ptr(),
      intermediate_size,
      bsz,
      InferenceContext::Instance().GetCurrentStream());
  return input_cont;
}

#define DISPATCH_GATED_ACT(T_TYPE, C_TYPE)                 \
  if (activation.options().dtype() == at::T_TYPE) {        \
    launch_gated_activation(                               \
        (C_TYPE*)output.data_ptr(),                        \
        (const C_TYPE*)activation.data_ptr(),              \
        (const C_TYPE*)bias.data_ptr(),                    \
        rows,                                              \
        out_channels,                                      \
        channels,                                          \
        activation_type == ActivationFuncType::GATED_GELU, \
        InferenceContext::Instance().GetCurrentStream());  \
  }

at::Tensor ds_gated_activation(
    at::Tensor& activation,
    at::Tensor& bias,
    int64_t actFun) {
  /*
  Used in FF of Stable diffusion
  */

  const ActivationFuncType activation_type =
      static_cast<ActivationFuncType>(actFun);

  assert(
      activation_type == ActivationFuncType::GATED_GELU ||
      activation_type == ActivationFuncType::GATED_SILU);

  const int64_t batch_size = activation.size(0);
  const int64_t seq_len = activation.size(1);
  const int64_t channels = activation.size(2);

  const int64_t rows = batch_size * seq_len;
  // Dimensionality is cut in half
  const int64_t out_channels = channels / 2;

  auto output =
      at::empty({batch_size, seq_len, out_channels}, activation.options());

  DISPATCH_GATED_ACT(kFloat, float);
  DISPATCH_GATED_ACT(kHalf, sycl::half);
#ifdef BF16_AVAILABLE
  DISPATCH_GATED_ACT(kBFloat16, sycl::ext::oneapi::bfloat16);
#endif

  return output;
}

template <typename T>
at::Tensor ds_bias_relu(at::Tensor& input, at::Tensor& bias) {
  auto input_cont = input.contiguous();

  int64_t bsz = input_cont.size(0) * input_cont.size(1);
  int64_t intermediate_size = input_cont.size(2);

  launch_bias_relu(
      (T*)input_cont.data_ptr(),
      (T*)bias.data_ptr(),
      intermediate_size,
      bsz,
      InferenceContext::Instance().GetCurrentStream());
  return input_cont;
}

template <typename T>
at::Tensor ds_bias_add(at::Tensor& input, at::Tensor& bias) {
  auto input_cont = input.contiguous();

  int64_t bsz = input_cont.size(0) * input_cont.size(1);
  int64_t hidden_size = input_cont.size(2);

  launch_bias_add(
      (T*)input_cont.data_ptr(),
      (T*)bias.data_ptr(),
      hidden_size,
      bsz,
      InferenceContext::Instance().GetCurrentStream());
  return input_cont;
}

template <typename T>
at::Tensor ds_bias_residual(
    at::Tensor& input,
    at::Tensor& residual,
    at::Tensor& bias) {
  auto input_cont = input.contiguous();
  auto residual_cont = residual.contiguous();

  int64_t bsz = input_cont.size(0) * input_cont.size(1);
  // launch_bias_residual((T*)input_cont.data_ptr(),
  //                      (T*)residual_cont.data_ptr(),
  //                      (T*)bias.data_ptr(),
  //                      bsz,
  //                      input_cont.size(2),
  //                      (bias.size(0) > 1),
  //                      InferenceContext::Instance().GetCurrentStream());
  return input_cont;
}

#define DISPATCH_LAYER_NORM(T_TYPE, C_TYPE)               \
  if (input.options().dtype() == at::T_TYPE) {            \
    launch_fused_ln(                                      \
        (C_TYPE*)output.data_ptr(),                       \
        (const C_TYPE*)input.data_ptr(),                  \
        (const C_TYPE*)gamma.data_ptr(),                  \
        (const C_TYPE*)beta.data_ptr(),                   \
        epsilon,                                          \
        rows,                                             \
        elems_per_row,                                    \
        InferenceContext::Instance().GetCurrentStream()); \
  }

at::Tensor ds_layer_norm(
    at::Tensor& input,
    at::Tensor& gamma,
    at::Tensor& beta,
    double epsilon) {
  const int64_t rows = input.size(0) * input.size(1);
  const int64_t elems_per_row = input.size(2);
  auto output = at::empty_like(input);

  DISPATCH_LAYER_NORM(kFloat, float);
  DISPATCH_LAYER_NORM(kHalf, sycl::half);
#ifdef BF16_AVAILABLE
  DISPATCH_LAYER_NORM(kBFloat16, sycl::ext::oneapi::bfloat16);
#endif

  return output;
}

#define DISPATCH_RMS_NORM(T_TYPE, C_TYPE)                 \
  if (input.options().dtype() == at::T_TYPE) {            \
    launch_rms_norm(                                      \
        (C_TYPE*)output.data_ptr(),                       \
        (C_TYPE*)nullptr,                                 \
        (const C_TYPE*)input.data_ptr(),                  \
        (const C_TYPE*)nullptr,                           \
        (const C_TYPE*)gamma.data_ptr(),                  \
        epsilon,                                          \
        rows,                                             \
        elems_per_row,                                    \
        InferenceContext::Instance().GetCurrentStream()); \
  }

at::Tensor ds_rms_norm(at::Tensor& input, at::Tensor& gamma, double epsilon) {
  // Get number of dims of tensor
  int64_t num_dims = input.dim();
  const int64_t rows =
      (num_dims == 2) ? input.size(0) : input.size(0) * input.size(1);
  const int64_t elems_per_row = (num_dims == 2) ? input.size(1) : input.size(2);

  auto output = at::empty_like(input);

  DISPATCH_RMS_NORM(kFloat, float);
  DISPATCH_RMS_NORM(kHalf, sycl::half);
#ifdef BF16_AVAILABLE
  DISPATCH_RMS_NORM(kBFloat16, sycl::ext::oneapi::bfloat16);
#endif

  return output;
}

#define DISPATCH_PRE_RMS_NORM(T_TYPE, C_TYPE)             \
  if (input.options().dtype() == at::T_TYPE) {            \
    launch_rms_norm(                                      \
        (C_TYPE*)output.data_ptr(),                       \
        (C_TYPE*)res_out.data_ptr(),                      \
        (const C_TYPE*)input.data_ptr(),                  \
        (const C_TYPE*)residual.data_ptr(),               \
        (const C_TYPE*)gamma.data_ptr(),                  \
        epsilon,                                          \
        rows,                                             \
        elems_per_row,                                    \
        InferenceContext::Instance().GetCurrentStream()); \
  }

std::vector<at::Tensor> ds_pre_rms_norm(
    at::Tensor& input,
    at::Tensor& residual,
    at::Tensor& gamma,
    double epsilon) {
  // Get number of dims of tensor
  int64_t num_dims = input.dim();
  const int64_t rows =
      (num_dims == 2) ? input.size(0) : input.size(0) * input.size(1);
  const int64_t elems_per_row = (num_dims == 2) ? input.size(1) : input.size(2);

  auto output = at::empty_like(input);
  auto res_out = at::empty_like(residual);

  DISPATCH_PRE_RMS_NORM(kFloat, float);
  DISPATCH_PRE_RMS_NORM(kHalf, sycl::half);
#ifdef BF16_AVAILABLE
  DISPATCH_PRE_RMS_NORM(kBFloat16, sycl::ext::oneapi::bfloat16);
#endif

  return {output, res_out};
}

template <typename T>
void ds_layer_norm_internal(
    T* workspace,
    at::Tensor& input,
    at::Tensor& gamma,
    at::Tensor& beta,
    float epsilon) {
  int64_t bsz = input.size(0) * input.size(1);
  launch_fused_ln(
      workspace,
      (const T*)input.data_ptr(),
      (const T*)gamma.data_ptr(),
      (const T*)beta.data_ptr(),
      epsilon,
      bsz,
      input.size(2),
      InferenceContext::Instance().GetCurrentStream());
}

#define DISPATCH_LAYER_NORM_RESIDUAL(T_TYPE, C_TYPE)      \
  if (input.options().dtype() == at::T_TYPE) {            \
    launch_fused_residual_ln(                             \
        (C_TYPE*)output.data_ptr(),                       \
        (const C_TYPE*)input.data_ptr(),                  \
        (const C_TYPE*)residual.data_ptr(),               \
        (const C_TYPE*)bias.data_ptr(),                   \
        (const C_TYPE*)gamma.data_ptr(),                  \
        (const C_TYPE*)beta.data_ptr(),                   \
        epsilon,                                          \
        rows,                                             \
        elems_per_row,                                    \
        InferenceContext::Instance().GetCurrentStream()); \
  }

/* Currently only used in unit testing */
at::Tensor ds_layer_norm_residual(
    at::Tensor& input,
    at::Tensor& bias,
    at::Tensor& residual,
    at::Tensor& gamma,
    at::Tensor& beta,
    double epsilon) {
  const int64_t rows = input.size(0) * input.size(1);
  const int64_t elems_per_row = input.size(2);
  auto output = at::empty_like(input);

  DISPATCH_LAYER_NORM_RESIDUAL(kFloat, float);
  DISPATCH_LAYER_NORM_RESIDUAL(kHalf, sycl::half);
#ifdef BF16_AVAILABLE
  DISPATCH_LAYER_NORM_RESIDUAL(kBFloat16, sycl::ext::oneapi::bfloat16);
#endif

  return output;
}

#define DISPATCH_PRE_LAYER_NORM_RESIDUAL(T_TYPE, C_TYPE)  \
  if (input.options().dtype() == at::T_TYPE) {            \
    launch_fused_residual_ln_store_pre_ln_res(            \
        (C_TYPE*)norm_output.data_ptr(),                  \
        (C_TYPE*)res_output.data_ptr(),                   \
        (const C_TYPE*)input.data_ptr(),                  \
        (const C_TYPE*)residual.data_ptr(),               \
        (const C_TYPE*)bias.data_ptr(),                   \
        (const C_TYPE*)gamma.data_ptr(),                  \
        (const C_TYPE*)beta.data_ptr(),                   \
        epsilon,                                          \
        rows,                                             \
        elems_per_row,                                    \
        InferenceContext::Instance().GetCurrentStream()); \
  }

/* Currently only used in unit testing */
std::vector<at::Tensor> ds_layer_norm_residual_store_pre_ln_res(
    at::Tensor& input,
    at::Tensor& bias,
    at::Tensor& residual,
    at::Tensor& gamma,
    at::Tensor& beta,
    double epsilon) {
  const int64_t rows = input.size(0) * input.size(1);
  const int64_t elems_per_row = input.size(2);
  auto norm_output = at::empty_like(input);
  auto res_output = at::empty_like(input);

  DISPATCH_PRE_LAYER_NORM_RESIDUAL(kFloat, float);
  DISPATCH_PRE_LAYER_NORM_RESIDUAL(kHalf, sycl::half);
#ifdef BF16_AVAILABLE
  DISPATCH_PRE_LAYER_NORM_RESIDUAL(kBFloat16, sycl::ext::oneapi::bfloat16);
#endif

  return {norm_output, res_output};
}

template <typename T>
void quantized_gemm(
    void* output,
    T* input,
    at::Tensor& weight,
    at::Tensor& qscale,
    int64_t groups,
    int64_t bsz,
    int64_t hidden_size) {
  // T* weight16 = (T*)InferenceContext::Instance().GetWorkSpace() + 12 *
  // hidden_size * bsz;

  auto options = at::TensorOptions()
                     .dtype(at::kHalf)
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  auto tmp = at::empty(weight.sizes(), options);
  T* weight16 = (T*)tmp.data_ptr();
  launch_dequantize(
      weight16,
      (int8_t*)weight.data_ptr(),
      (float*)qscale.data_ptr(),
      weight.size(0),
      weight.size(1),
      groups,
      InferenceContext::Instance().GetCurrentStream());

  float alpha = (T)1.0;
  float gemm_beta = (T)0.0;
  mkl_gemm_ex(
      InferenceContext::Instance().GetCublasHandle(),
      oneapi::mkl::transpose::trans,
      oneapi::mkl::transpose::nontrans,
      weight.size(0),
      bsz,
      weight.size(1),
      &alpha,
      &gemm_beta,
      weight16,
      (T*)input,
      (T*)output,
      99);
}

template <typename T>
at::Tensor qkv_unfused_mkl(
    at::Tensor& output,
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& q_scale,
    at::Tensor& bias,
    at::Tensor& gamma,
    at::Tensor& beta,
    const float epsilon,
    bool add_bias,
    bool q_int8,
    bool transposed_mode) {
  int64_t bsz = input.size(0) * input.size(1);
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  workspace += (3 * bsz * input.size(2));
  ds_layer_norm_internal<T>(workspace, input, gamma, beta, epsilon);

  if (q_int8) {
    quantized_gemm<T>(
        output.data_ptr(),
        workspace,
        weight,
        q_scale,
        q_scale.size(0),
        bsz,
        input.size(2));
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;

    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        weight.size(transposed_mode ? 0 : 1),
        bsz,
        input.size(2),
        &alpha,
        &gemm_beta,
        (T*)weight.data_ptr(),
        workspace,
        (T*)output.data_ptr(),
        99);
  }
  if (add_bias)
    launch_bias_add(
        (T*)output.data_ptr(),
        (T*)bias.data_ptr(),
        (transposed_mode || q_int8) ? weight.size(0) : weight.size(1),
        bsz,
        InferenceContext::Instance().GetCurrentStream());
  return at::from_blob(
      workspace,
      input.sizes(),
      c10::TensorType::contiguousStridesOf(input.sizes()),
      nullptr,
      input.options(),
      input.options().device());
}

template <typename T>
std::vector<at::Tensor> ds_rms_qkv(
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& q_scale,
    at::Tensor& gamma,
    const double epsilon,
    bool q_int8,
    bool transposed_mode) {
  const int64_t bsz = input.size(0) * input.size(1);
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  T* rms_norm_ptr = workspace + (3 * bsz * input.size(2));
  int64_t out_size =
      (transposed_mode || q_int8) ? weight.size(0) : weight.size(1);

  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  auto rms_norm = at::from_blob(
      rms_norm_ptr,
      input.sizes(),
      c10::TensorType::contiguousStridesOf(input.sizes()),
      nullptr,
      options,
      input.device());
  auto output = at::from_blob(
      workspace,
      {input.size(0), input.size(1), out_size},
      c10::TensorType::contiguousStridesOf(
          {input.size(0), input.size(1), out_size}),
      nullptr,
      options,
      input.device());

  launch_rms_norm(
      (T*)rms_norm.data_ptr(),
      (T*)nullptr,
      (const T*)input.data_ptr(),
      (const T*)nullptr,
      (const T*)gamma.data_ptr(),
      epsilon,
      bsz,
      input.size(2),
      InferenceContext::Instance().GetCurrentStream());

  if (q_int8) {
    quantized_gemm<T>(
        (T*)output.data_ptr(),
        (T*)rms_norm.data_ptr(),
        weight,
        q_scale,
        q_scale.size(0),
        bsz,
        input.size(2));
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;

    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        weight.size(transposed_mode ? 0 : 1),
        bsz,
        input.size(2),
        &alpha,
        &gemm_beta,
        (T*)weight.data_ptr(),
        (T*)rms_norm.data_ptr(),
        (T*)output.data_ptr(),
        99);
  }

  return {output, rms_norm};
}

template <typename T>
std::vector<at::Tensor> ds_qkv_gemm(
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& q_scale,
    at::Tensor& bias,
    at::Tensor& gamma,
    at::Tensor& beta,
    const double epsilon,
    bool add_bias,
    bool q_int8,
    bool transposed_mode) {
  int64_t bsz = input.size(0) * input.size(1);
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  int64_t out_size =
      (transposed_mode || q_int8) ? weight.size(0) : weight.size(1);

  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  auto output = at::from_blob(
      workspace,
      {input.size(0), input.size(1), out_size},
      c10::TensorType::contiguousStridesOf(
          {input.size(0), input.size(1), out_size}),
      nullptr,
      options,
      input.device());
  auto inp_norm = qkv_unfused_mkl<T>(
      output,
      input,
      weight,
      q_scale,
      bias,
      gamma,
      beta,
      epsilon,
      add_bias,
      q_int8,
      transposed_mode);

  return {output, inp_norm};
}

template <typename T>
void quantized_gemm(
    at::Tensor& output,
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& qscale,
    int64_t groups,
    int64_t merge_count) {
  int64_t bsz = input.size(0) * input.size(1);
  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  auto weight16 = at::empty({weight.size(0), weight.size(1)}, options);

  launch_dequantize(
      (T*)weight16.data_ptr(),
      (int8_t*)weight.data_ptr(),
      (float*)qscale.data_ptr(),
      weight.size(0),
      weight.size(1),
      groups,
      merge_count,
      InferenceContext::Instance().GetCurrentStream());

  float alpha = (T)1.0;
  float gemm_beta = (T)0.0;
  mkl_gemm_ex(
      InferenceContext::Instance().GetCublasHandle(),
      oneapi::mkl::transpose::trans,
      oneapi::mkl::transpose::nontrans,
      weight.size(0),
      bsz,
      input.size(2),
      &alpha,
      &gemm_beta,
      (T*)weight16.data_ptr(),
      (T*)input.data_ptr(),
      (T*)output.data_ptr(),
      99);
}

template <typename T>
at::Tensor ds_linear_layer(
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& bias,
    bool add_bias,
    bool do_flash_attn,
    int64_t num_heads,
    bool transposed_mode,
    double rope_theta) {
  auto input_cont = input.contiguous();
  auto options = at::TensorOptions()
                     .dtype(input_cont.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  int64_t head_size = input_cont.size(2) / num_heads;
  int64_t bsz = input.size(0) * input.size(1);
  int64_t out_size = transposed_mode ? weight.size(0) : weight.size(1);
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  auto output = at::from_blob(
      workspace,
      {input.size(0), input.size(1), out_size},
      c10::TensorType::contiguousStridesOf(
          {input.size(0), input.size(1), out_size}),
      nullptr,
      options,
      input.device());

  float alpha = (T)1.0;
  float gemm_beta = (T)0.0;
  *(InferenceContext::Instance().GetCublasHandle()) =
      *(InferenceContext::Instance().GetCurrentStream());

  mkl_gemm_ex(
      InferenceContext::Instance().GetCublasHandle(),
      (transposed_mode ? oneapi::mkl::transpose::trans
                       : oneapi::mkl::transpose::nontrans),
      oneapi::mkl::transpose::nontrans,
      weight.size(transposed_mode ? 0 : 1),
      bsz,
      input_cont.size(2),
      &alpha,
      &gemm_beta,
      (T*)weight.data_ptr(),
      (T*)input_cont.data_ptr(),
      (T*)output.data_ptr(),
      99);
  if (add_bias)
    launch_bias_add(
        (T*)output.data_ptr(),
        (T*)bias.data_ptr(),
        weight.size(transposed_mode ? 0 : 1),
        bsz,
        InferenceContext::Instance().GetCurrentStream());
  bool add_padding =
      (head_size % 32 != 0 && head_size < 64) || (head_size % 64 != 0);
  if (do_flash_attn) {
    if (add_padding) {
      int64_t padded_head_size =
          head_size < 32 ? 32 : (head_size < 64 ? 64 : 128);
      auto padded_output = workspace + output.numel();
      auto final_output = padded_output +
          (input.size(0) * input.size(1) * 3 * num_heads * padded_head_size);
      pad_data(
          padded_output,
          workspace,
          3 * bsz * num_heads,
          head_size,
          padded_head_size,
          InferenceContext::Instance().GetCurrentStream());

      launch_bias_add_transform_0213<T>(
          final_output,
          final_output +
              (input.size(0) * input.size(1) * num_heads * padded_head_size),
          final_output +
              (input.size(0) * input.size(1) * 2 * num_heads *
               padded_head_size),
          padded_output,
          nullptr,
          input.size(0),
          input.size(1),
          0,
          input.size(1),
          (num_heads * padded_head_size),
          num_heads,
          -1,
          -1,
          false,
          false,
          InferenceContext::Instance().GetCurrentStream(),
          3,
          input.size(1),
          rope_theta);
      return at::from_blob(
          final_output,
          {3, input.size(0), num_heads, input.size(1), padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {3, input.size(0), num_heads, input.size(1), padded_head_size}),
          nullptr,
          options,
          input.device());
      // return at::from_blob(padded_output, {input.size(0) * input.size(1), 3,
      // num_heads, padded_head_size}, options);
    } else {
      auto final_output = workspace + output.numel();
      launch_bias_add_transform_0213<T>(
          final_output,
          final_output + (input.size(0) * input.size(1) * input_cont.size(2)),
          final_output +
              (input.size(0) * input.size(1) * 2 * input_cont.size(2)),
          workspace,
          nullptr,
          input.size(0),
          input.size(1),
          0,
          input.size(1),
          input_cont.size(2),
          num_heads,
          -1,
          -1,
          false,
          false,
          InferenceContext::Instance().GetCurrentStream(),
          3,
          input.size(1),
          rope_theta);
      return at::from_blob(
          final_output,
          {3, input.size(0), num_heads, input.size(1), head_size},
          c10::TensorType::contiguousStridesOf(
              {3, input.size(0), num_heads, input.size(1), head_size}),
          nullptr,
          options,
          input.device());
      // return at::from_blob(workspace, {input.size(0) * input.size(1), 3,
      // num_heads, head_size}, options);
    }

  } else
    return output;
}

template <typename T>
std::vector<at::Tensor> add_padding(
    at::Tensor& query,
    at::Tensor& key,
    at::Tensor& value) {
  int64_t head_size = query.size(3);
  int64_t padded_head_size = head_size < 32 ? 32 : (head_size < 64 ? 64 : 128);
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  T* key_pad_ptr = workspace +
      padded_head_size * query.size(0) * query.size(1) * query.size(2);
  T* value_pad_ptr =
      key_pad_ptr + padded_head_size * query.size(0) * query.size(1) * 128;
  pad_head_seq(
      workspace,
      (T*)query.data_ptr(),
      query.size(0) * query.size(1),
      query.size(2),
      query.size(2),
      head_size,
      padded_head_size,
      InferenceContext::Instance().GetCurrentStream());
  pad_head_seq(
      key_pad_ptr,
      (T*)key.data_ptr(),
      query.size(0) * query.size(1),
      key.size(2),
      128,
      head_size,
      padded_head_size,
      InferenceContext::Instance().GetCurrentStream());
  pad_head_seq(
      value_pad_ptr,
      (T*)value.data_ptr(),
      query.size(0) * query.size(1),
      key.size(2),
      128,
      head_size,
      padded_head_size,
      InferenceContext::Instance().GetCurrentStream());
  return {
      at::from_blob(
          workspace,
          {query.size(0), query.size(1), query.size(2), padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {query.size(0), query.size(1), query.size(2), padded_head_size}),
          nullptr,
          query.options(),
          query.options().device()),
      at::from_blob(
          key_pad_ptr,
          {query.size(0), query.size(1), 128, padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {query.size(0), query.size(1), 128, padded_head_size}),
          nullptr,
          query.options(),
          query.options().device()),
      at::from_blob(
          value_pad_ptr,
          {query.size(0), query.size(1), 128, padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {query.size(0), query.size(1), 128, padded_head_size}),
          nullptr,
          query.options(),
          query.options().device())};
}

template <typename T>
std::vector<at::Tensor> padd_add_transform(
    at::Tensor& query,
    at::Tensor& key,
    at::Tensor& value,
    int64_t heads,
    bool add_padding) {
  int64_t head_size = query.size(2) / heads;
  int64_t key_value_length = add_padding ? 128 : key.size(1);
  int64_t padded_head_size = add_padding
      ? (head_size < 32 ? 32 : (head_size < 64 ? 64 : 128))
      : head_size;
  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  T* key_pad_ptr =
      workspace + padded_head_size * query.size(0) * heads * query.size(1);
  T* value_pad_ptr =
      key_pad_ptr + padded_head_size * query.size(0) * heads * key_value_length;
  launch_pad_add_transform_0213(
      workspace,
      (T*)query.data_ptr(),
      query.size(0),
      query.size(2),
      query.size(1),
      query.size(1),
      heads,
      padded_head_size,
      InferenceContext::Instance().GetCurrentStream());
  launch_pad_add_transform_0213(
      key_pad_ptr,
      (T*)key.data_ptr(),
      key.size(0),
      key.size(2),
      key.size(1),
      key_value_length,
      heads,
      padded_head_size,
      InferenceContext::Instance().GetCurrentStream());
  launch_pad_add_transform_0213(
      value_pad_ptr,
      (T*)value.data_ptr(),
      value.size(0),
      value.size(2),
      value.size(1),
      key_value_length,
      heads,
      padded_head_size,
      InferenceContext::Instance().GetCurrentStream());
  return {
      at::from_blob(
          workspace,
          {query.size(0), heads, query.size(1), padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {query.size(0), heads, query.size(1), padded_head_size}),
          nullptr,
          query.options(),
          query.options().device()),
      at::from_blob(
          key_pad_ptr,
          {query.size(0), heads, key_value_length, padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {query.size(0), heads, key_value_length, padded_head_size}),
          nullptr,
          query.options(),
          query.options().device()),
      at::from_blob(
          value_pad_ptr,
          {query.size(0), heads, key_value_length, padded_head_size},
          c10::TensorType::contiguousStridesOf(
              {query.size(0), heads, key_value_length, padded_head_size}),
          nullptr,
          query.options(),
          query.options().device())};
}

template <typename T>
at::Tensor ds_vector_matmul(
    at::Tensor& input,
    at::Tensor& weight,
    bool async_op,
    at::Tensor& q_scale,
    bool q_int8,
    bool transposed_mode) {
  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  int64_t out_size =
      (q_int8 || transposed_mode) ? weight.size(0) : weight.size(1);
  int64_t bsz = input.size(0) * input.size(1);

  T* workspace = (T*)InferenceContext::Instance().GetWorkSpace();
  auto output = at::from_blob(
      workspace,
      {input.size(0), input.size(1), out_size},
      c10::TensorType::contiguousStridesOf(
          {input.size(0), input.size(1), out_size}),
      nullptr,
      options,
      input.device());
  if (q_int8) {
    quantized_gemm<T>(
        output.data_ptr(),
        (T*)input.data_ptr(),
        weight,
        q_scale,
        q_scale.size(0),
        bsz,
        input.size(2));
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;
    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream(async_op));
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        weight.size(transposed_mode ? 0 : 1),
        bsz,
        input.size(2),
        &alpha,
        &gemm_beta,
        (T*)weight.data_ptr(),
        (T*)input.data_ptr(),
        (T*)output.data_ptr(),
        99);
  }
  return output;
}

template <typename T>
at::Tensor ds_vector_matmul_int8(
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& q_scale,
    int64_t groups,
    int64_t merge_count) {
  auto input_cont = input.contiguous();
  auto options = at::TensorOptions()
                     .dtype(input_cont.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  auto output = at::empty(
      {input_cont.size(0), input_cont.size(1), weight.size(1)}, options);

  quantized_gemm<T>(output, input_cont, weight, q_scale, groups, merge_count);
  return output;
}

template <typename T>
at::Tensor mlp_unfused_mkl(
    at::Tensor& output,
    at::Tensor& input,
    at::Tensor& residual,
    at::Tensor& input_bias,
    at::Tensor& weight,
    at::Tensor& weight1,
    at::Tensor& bias,
    at::Tensor& gamma,
    at::Tensor& beta,
    const float epsilon,
    bool preLayerNorm,
    bool mlp_after_attn,
    at::Tensor& q_scale,
    at::Tensor& q_scale1,
    bool q_int8,
    ActivationFuncType act_func_type,
    bool transposed_mode) {
  int64_t bsz = input.size(0) * input.size(1);
  T* inp_norm = (T*)InferenceContext::Instance().GetWorkSpace() +
      at::numel(input) + at::numel(output);
  T* intermediate = inp_norm + at::numel(input);

  if (mlp_after_attn) {
    launch_fused_residual_ln(
        (T*)inp_norm,
        (const T*)input.data_ptr(),
        (const T*)residual.data_ptr(),
        (const T*)input_bias.data_ptr(),
        (const T*)gamma.data_ptr(),
        (const T*)beta.data_ptr(),
        epsilon,
        bsz,
        input.size(2),
        InferenceContext::Instance().GetCurrentStream());
  } else {
    ds_layer_norm_internal(inp_norm, input, gamma, beta, epsilon);
  }
  if (q_int8) {
    quantized_gemm<T>(
        intermediate,
        inp_norm,
        weight,
        q_scale,
        q_scale.size(0),
        bsz,
        input.size(2));
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;
    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        weight.size(transposed_mode ? 0 : 1),
        bsz,
        input.size(2),
        &alpha,
        &gemm_beta,
        (T*)weight.data_ptr(),
        inp_norm,
        intermediate,
        99);
  }
  if (act_func_type == ActivationFuncType::GELU) {
    launch_bias_gelu(
        intermediate,
        (T*)bias.data_ptr(),
        (transposed_mode || q_int8) ? weight.size(0) : weight.size(1),
        bsz,
        InferenceContext::Instance().GetCurrentStream());
  } else if (act_func_type == ActivationFuncType::ReLU) {
    launch_bias_relu(
        intermediate,
        (T*)bias.data_ptr(),
        (transposed_mode || q_int8) ? weight.size(0) : weight.size(1),
        bsz,
        InferenceContext::Instance().GetCurrentStream());
  }

  if (q_int8) {
    quantized_gemm<T>(
        output.data_ptr(),
        intermediate,
        weight1,
        q_scale1,
        q_scale1.size(0),
        bsz,
        input.size(2));
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;
    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        weight1.size(transposed_mode ? 0 : 1),
        bsz,
        weight1.size(transposed_mode ? 1 : 0),
        &alpha,
        &gemm_beta,
        (T*)weight1.data_ptr(),
        intermediate,
        (T*)output.data_ptr(),
        99);
  }

  return at::from_blob(
      inp_norm,
      input.sizes(),
      c10::TensorType::contiguousStridesOf(input.sizes()),
      nullptr,
      input.options(),
      input.options().device());
}

template <typename T>
std::vector<at::Tensor> ds_mlp_gemm(
    at::Tensor& input,
    at::Tensor& residual,
    at::Tensor& input_bias,
    at::Tensor& weight_interm,
    at::Tensor& weight_out,
    at::Tensor& bias,
    at::Tensor& gamma,
    at::Tensor& beta,
    const double epsilon,
    bool preLayerNorm,
    bool mlp_after_attn,
    at::Tensor& q_scale,
    at::Tensor& q_scale1,
    bool q_int8,
    int64_t activation_type,
    bool transposed_mode) {
  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  int64_t out_size =
      (q_int8 || transposed_mode) ? weight_out.size(0) : weight_out.size(1);
  auto output = at::from_blob(
      (T*)InferenceContext::Instance().GetWorkSpace() + at::numel(input),
      {input.size(0), input.size(1), out_size},
      c10::TensorType::contiguousStridesOf(
          {input.size(0), input.size(1), out_size}),
      nullptr,
      options,
      input.device());
  int64_t bsz = input.size(0) * input.size(1);

  auto act_func_type = static_cast<ActivationFuncType>(activation_type);
  auto res_add = mlp_unfused_mkl<T>(
      output,
      mlp_after_attn ? input : residual,
      residual,
      input_bias,
      weight_interm,
      weight_out,
      bias,
      gamma,
      beta,
      epsilon,
      preLayerNorm,
      mlp_after_attn,
      q_scale,
      q_scale1,
      q_int8,
      act_func_type,
      transposed_mode);

  return {output, res_add};
}

template <typename T>
std::vector<at::Tensor> ds_rms_mlp_gemm(
    at::Tensor& input,
    at::Tensor& residual,
    at::Tensor& weight_interm,
    at::Tensor& weight_out,
    at::Tensor& gamma,
    const double epsilon,
    at::Tensor& q_scale,
    at::Tensor& q_scale1,
    bool q_int8,
    int64_t activation_type,
    bool transposed_mode) {
  const int64_t bsz = input.size(0) * input.size(1);
  const size_t input_neurons = input.size(2);
  const int64_t mlp_1_out_neurons =
      transposed_mode ? weight_interm.size(0) : weight_interm.size(1);
  const size_t mlp_2_in_neurons =
      transposed_mode ? weight_out.size(1) : weight_out.size(0);

  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  T* output_ptr =
      (T*)InferenceContext::Instance().GetWorkSpace() + at::numel(input);
  T* inp_norm_ptr = output_ptr + at::numel(input);
  T* intermediate_ptr = inp_norm_ptr + at::numel(input);

  auto output = at::from_blob(
      output_ptr,
      input.sizes(),
      c10::TensorType::contiguousStridesOf(input.sizes()),
      nullptr,
      options,
      input.device());
  auto inp_norm = at::from_blob(
      inp_norm_ptr,
      input.sizes(),
      c10::TensorType::contiguousStridesOf(input.sizes()),
      nullptr,
      options,
      input.device());
  auto intermediate_gemm = at::from_blob(
      intermediate_ptr,
      {input.size(0), input.size(1), mlp_1_out_neurons},
      c10::TensorType::contiguousStridesOf(
          {input.size(0), input.size(1), mlp_1_out_neurons}),
      nullptr,
      options,
      input.device());

  auto act_func_type = static_cast<ActivationFuncType>(activation_type);

  // RMS Norm, we'll update the residual in-place
  launch_rms_norm(
      (T*)inp_norm.data_ptr(),
      (T*)residual.data_ptr(),
      (const T*)input.data_ptr(),
      (const T*)residual.data_ptr(),
      (const T*)gamma.data_ptr(),
      epsilon,
      bsz,
      input_neurons,
      InferenceContext::Instance().GetCurrentStream());

  if (q_int8) {
    quantized_gemm<T>(
        intermediate_ptr,
        (T*)inp_norm.data_ptr(),
        weight_interm,
        q_scale,
        q_scale.size(0),
        bsz,
        input_neurons);
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;
    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        mlp_1_out_neurons,
        bsz,
        input_neurons,
        &alpha,
        &gemm_beta,
        (T*)weight_interm.data_ptr(),
        (T*)inp_norm.data_ptr(),
        intermediate_ptr,
        99);
  }

  if (act_func_type == ActivationFuncType::GELU) {
    launch_bias_gelu(
        intermediate_ptr,
        (T*)nullptr,
        mlp_1_out_neurons,
        bsz,
        InferenceContext::Instance().GetCurrentStream());
  } else if (act_func_type == ActivationFuncType::ReLU) {
    launch_bias_relu(
        intermediate_ptr,
        (T*)nullptr,
        mlp_1_out_neurons,
        bsz,
        InferenceContext::Instance().GetCurrentStream());
  } else if (act_func_type == ActivationFuncType::GATED_GELU) {
    launch_gated_activation(
        intermediate_ptr,
        (const T*)intermediate_ptr,
        (const T*)nullptr,
        bsz,
        mlp_1_out_neurons,
        mlp_1_out_neurons,
        true,
        InferenceContext::Instance().GetCurrentStream());
  } else if (act_func_type == ActivationFuncType::GATED_SILU) {
    launch_gated_activation(
        intermediate_ptr,
        (const T*)intermediate_ptr,
        (const T*)nullptr,
        bsz,
        mlp_1_out_neurons,
        mlp_1_out_neurons,
        false,
        InferenceContext::Instance().GetCurrentStream());
  }

  if (q_int8) {
    quantized_gemm<T>(
        output.data_ptr(),
        intermediate_ptr,
        weight_out,
        q_scale1,
        q_scale1.size(0),
        bsz,
        input.size(2));
  } else {
    float alpha = (T)1.0;
    float gemm_beta = (T)0.0;
    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        input_neurons,
        bsz,
        mlp_2_in_neurons,
        &alpha,
        &gemm_beta,
        (T*)weight_out.data_ptr(),
        intermediate_ptr,
        (T*)output.data_ptr(),
        99,
        mlp_1_out_neurons);
  }

  return {output, residual};
}

template <typename T>
at::Tensor fused_gemm_gelu(
    at::Tensor& input,
    at::Tensor& weight,
    at::Tensor& weight_scale,
    at::Tensor& bias,
    at::Tensor& weight_out,
    at::Tensor& weight_out_scale,
    bool q_int8,
    bool transposed_mode) {
  auto options = at::TensorOptions()
                     .dtype(input.options().dtype())
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);

  int64_t intm_dim =
      (transposed_mode || q_int8) ? weight.size(0) : weight.size(1);

  // auto output = at::from_blob((T*)InferenceContext::Instance().GetWorkSpace()
  // + at::numel(input),
  //                            {input.size(0), input.size(1), out_size},
  //                            options);
  // T* intermediate = (T*)input.data_ptr() + at::numel(input);
  auto intermediate =
      at::empty({input.size(0), input.size(1), intm_dim}, options);

  int64_t bsz = input.size(0) * input.size(1);

  float alpha = (T)1.0;
  float gemm_beta = (T)0.0;
  if (q_int8) {
    quantized_gemm<T>(
        intermediate.data_ptr(),
        (T*)input.data_ptr(),
        weight,
        weight_scale,
        weight_scale.size(0),
        bsz,
        input.size(2));
  } else {
    *(InferenceContext::Instance().GetCublasHandle()) =
        *(InferenceContext::Instance().GetCurrentStream());
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        intm_dim,
        bsz,
        input.size(2),
        &alpha,
        &gemm_beta,
        (T*)weight.data_ptr(),
        (T*)input.data_ptr(),
        (T*)intermediate.data_ptr(),
        99);
  }
  launch_bias_gelu(
      (T*)intermediate.data_ptr(),
      (T*)bias.data_ptr(),
      intm_dim,
      bsz,
      InferenceContext::Instance().GetCurrentStream());

  int64_t out_size =
      (transposed_mode || q_int8) ? weight_out.size(0) : weight_out.size(1);
  auto output = at::empty({input.size(0), input.size(1), out_size}, options);
  if (q_int8) {
    quantized_gemm<T>(
        output.data_ptr(),
        (T*)intermediate.data_ptr(),
        weight_out,
        weight_out_scale,
        weight_out_scale.size(0),
        bsz,
        input.size(2));
  } else {
    mkl_gemm_ex(
        InferenceContext::Instance().GetCublasHandle(),
        (transposed_mode ? oneapi::mkl::transpose::trans
                         : oneapi::mkl::transpose::nontrans),
        oneapi::mkl::transpose::nontrans,
        out_size,
        bsz,
        intm_dim,
        &alpha,
        &gemm_beta,
        (T*)weight_out.data_ptr(),
        (T*)intermediate.data_ptr(),
        (T*)output.data_ptr(),
        99);
  }
  return output;
}

template <typename T>
at::Tensor& residual_add_bias(
    at::Tensor& hidden_state,
    at::Tensor& residual,
    const at::Tensor& attention_output,
    const at::Tensor& attention_bias,
    const at::Tensor& final_bias,
    const int64_t mp_size,
    const bool mlp_after_attn,
    const bool add_bias,
    const bool preln) {
  int64_t bsz = residual.size(0) * residual.size(1);
  int64_t hidden_size = residual.size(2);
  if (mlp_after_attn)
    launch_bias_residual(
        static_cast<T*>(residual.data_ptr()),
        static_cast<T*>(hidden_state.data_ptr()),
        static_cast<T*>(attention_output.data_ptr()),
        static_cast<T*>(final_bias.data_ptr()),
        static_cast<T*>(attention_bias.data_ptr()),
        bsz,
        hidden_size,
        mp_size,
        preln,
        InferenceContext::Instance().GetCurrentStream());
  else
    launch_gptj_residual_add<T>(
        static_cast<T*>(residual.data_ptr()),
        static_cast<T*>(hidden_state.data_ptr()),
        static_cast<T*>(attention_output.data_ptr()),
        static_cast<T*>(final_bias.data_ptr()),
        static_cast<T*>((add_bias ? attention_bias.data_ptr() : nullptr)),
        hidden_size,
        bsz,
        mp_size,
        InferenceContext::Instance().GetCurrentStream());
  return residual;
}

#define DISPATCH_VECTOR_ADD(T_TYPE, C_TYPE)               \
  if (a.scalar_type() == at::k##T_TYPE) {                 \
    launch_vector_add<C_TYPE>(                            \
        (C_TYPE*)(a.data_ptr()),                          \
        (const C_TYPE*)(a.data_ptr()),                    \
        (const C_TYPE*)(b.data_ptr()),                    \
        gamma,                                            \
        total_elems,                                      \
        InferenceContext::Instance().GetCurrentStream()); \
  }

at::Tensor& _vector_add(at::Tensor& a, at::Tensor& b, double gamma) {
  const int64_t total_elems = a.numel();

  DISPATCH_VECTOR_ADD(Float, float)
  DISPATCH_VECTOR_ADD(Half, sycl::half)
#ifdef BF16_AVAILABLE
  DISPATCH_VECTOR_ADD(BFloat16, sycl::ext::oneapi::bfloat16)
#endif

  return a;
}

std::vector<at::Tensor> apply_rotary_pos_emb(
    at::Tensor& mixed_query,
    at::Tensor& key_layer,
    int64_t rotary_dim,
    int64_t offset,
    int64_t num_heads,
    bool rotate_half,
    double rope_theta) {
  auto query_cont = mixed_query.contiguous();
  auto key_cont = key_layer.contiguous();

  int64_t bsz = mixed_query.size(0);
  int64_t head_size = mixed_query.size(2) / num_heads;
  int64_t seq_len = mixed_query.size(1);

  if (mixed_query.scalar_type() == at::kFloat)
    launch_apply_rotary_pos_emb<float>(
        (float*)query_cont.data_ptr(),
        (float*)key_cont.data_ptr(),
        head_size,
        seq_len,
        rotary_dim,
        offset,
        num_heads,
        bsz,
        rope_theta,
        InferenceContext::Instance().GetCurrentStream(),
        InferenceContext::Instance().GetMaxTokenLength());
  else
    launch_apply_rotary_pos_emb<sycl::half>(
        (sycl::half*)query_cont.data_ptr(),
        (sycl::half*)key_cont.data_ptr(),
        head_size,
        seq_len,
        rotary_dim,
        offset,
        num_heads,
        bsz,
        rope_theta,
        InferenceContext::Instance().GetCurrentStream(),
        InferenceContext::Instance().GetMaxTokenLength());
  return {query_cont, key_cont};
}

#define DISPATCH_MOE_RESIDUAL(T_TYPE, C_TYPE)             \
  if (moe_res.scalar_type() == at::T_TYPE) {              \
    launch_moe_res_matmul<C_TYPE>(                        \
        (C_TYPE*)moe_res.data_ptr(),                      \
        (C_TYPE*)coef.data_ptr(),                         \
        (C_TYPE*)output.data_ptr(),                       \
        M,                                                \
        N,                                                \
        InferenceContext::Instance().GetCurrentStream()); \
  }

at::Tensor moe_res_matmul(
    at::Tensor& moe_res,
    at::Tensor& coef,
    at::Tensor& output) {
  int64_t M = moe_res.size(0) * moe_res.size(1);
  int64_t N = moe_res.size(2);

  DISPATCH_MOE_RESIDUAL(kFloat, float)
  DISPATCH_MOE_RESIDUAL(kHalf, sycl::half)
#ifdef BF16_AVAILABLE
  DISPATCH_MOE_RESIDUAL(kBFloat16, sycl::ext::oneapi::bfloat16)
#endif

  return output;
}

void ds_release_workspace() {
  InferenceContext::Instance().release_workspace();
}

bool ds_retake_workspace() {
  return InferenceContext::Instance().retake_workspace();
}

template <typename T>
at::Tensor ds_dequantize(
    at::Tensor& weight,
    at::Tensor& qscale,
    int64_t groups) {
  auto options = at::TensorOptions()
                     .dtype(at::kHalf)
                     .layout(at::kStrided)
                     .device(at::kXPU)
                     .requires_grad(false);
  auto weight16 = at::empty({weight.size(0), weight.size(1)}, options);

  launch_dequantize(
      (T*)weight16.data_ptr(),
      (int8_t*)weight.data_ptr(),
      (float*)qscale.data_ptr(),
      weight.size(0),
      weight.size(1),
      groups,
      InferenceContext::Instance().GetCurrentStream());

  return weight16;
}

DS_LIBRARY_FRAGMENT() {
  DS_OP_REGISTER(
      "softmax_context_int8",
      ds_softmax_context1<sycl::half>,
      c10::DispatchKey::AutogradXPU);

  // The following functions handle type dispatching internally
  DS_OP_REGISTER(
      "gated_activation", ds_gated_activation, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER("layer_norm", ds_layer_norm, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "_layer_norm_residual",
      ds_layer_norm_residual,
      c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "layer_norm_residual_store_pre_ln_res",
      ds_layer_norm_residual_store_pre_ln_res,
      c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER("ds_rms_norm", ds_rms_norm, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "ds_pre_rms_norm", ds_pre_rms_norm, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER("_vector_add", _vector_add, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "apply_rotary_pos_emb",
      apply_rotary_pos_emb,
      c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "moe_res_matmul", moe_res_matmul, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER("reset_cache", reset_cache, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "release_workspace", ds_release_workspace, c10::DispatchKey::AutogradXPU);
  DS_OP_REGISTER(
      "retake_workspace", ds_retake_workspace, c10::DispatchKey::AutogradXPU);

  // The following functions are templated and need to be explicitly
  // instantiated and bound to different python methods
#define DEF_OPS(_name, _dtype)                                                 \
  DS_OP_REGISTER(                                                              \
      "softmax_" #_name, ds_softmax<_dtype>, c10::DispatchKey::AutogradXPU);   \
  DS_OP_REGISTER(                                                              \
      "softmax_context_" #_name,                                               \
      ds_softmax_context<_dtype>,                                              \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "bias_gelu_" #_name,                                                     \
      ds_bias_gelu<_dtype>,                                                    \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "bias_add_" #_name, ds_bias_add<_dtype>, c10::DispatchKey::AutogradXPU); \
  DS_OP_REGISTER(                                                              \
      "bias_relu_" #_name,                                                     \
      ds_bias_relu<_dtype>,                                                    \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "bias_residual_" #_name,                                                 \
      ds_bias_residual<_dtype>,                                                \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "qkv_gemm_" #_name, ds_qkv_gemm<_dtype>, c10::DispatchKey::AutogradXPU); \
  DS_OP_REGISTER(                                                              \
      "rms_qkv_gemm_" #_name,                                                  \
      ds_rms_qkv<_dtype>,                                                      \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "mlp_gemm_" #_name, ds_mlp_gemm<_dtype>, c10::DispatchKey::AutogradXPU); \
  DS_OP_REGISTER(                                                              \
      "rms_mlp_gemm_" #_name,                                                  \
      ds_rms_mlp_gemm<_dtype>,                                                 \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "vector_matmul_" #_name,                                                 \
      ds_vector_matmul<_dtype>,                                                \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "linear_layer_" #_name,                                                  \
      ds_linear_layer<_dtype>,                                                 \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "fused_gemm_gelu_" #_name,                                               \
      fused_gemm_gelu<_dtype>,                                                 \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "residual_add_bias_" #_name,                                             \
      residual_add_bias<_dtype>,                                               \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "einsum_sec_sm_ecm_" #_name,                                             \
      einsum_sec_sm_ecm<_dtype>,                                               \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "add_padding_" #_name,                                                   \
      add_padding<_dtype>,                                                     \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "pad_transform_" #_name,                                                 \
      padd_add_transform<_dtype>,                                              \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "allocate_workspace_" #_name,                                            \
      allocate_workspace<_dtype>,                                              \
      c10::DispatchKey::AutogradXPU);                                          \
  DS_OP_REGISTER(                                                              \
      "ti_dequantize_" #_name,                                                 \
      ds_dequantize<_dtype>,                                                   \
      c10::DispatchKey::AutogradXPU)

  DEF_OPS(fp32, float);
  DEF_OPS(fp16, sycl::half);
#ifdef BF16_AVAILABLE
  DEF_OPS(bf16, sycl::ext::oneapi::bfloat16);
#endif
}
