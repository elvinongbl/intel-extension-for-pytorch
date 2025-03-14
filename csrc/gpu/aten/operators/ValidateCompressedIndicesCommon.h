#pragma once

#include <ATen/Dispatch.h>
#include <ATen/Tensor.h>
#include <ATen/native/TensorIterator.h>

#ifndef AT_PER_OPERATOR_HEADERS
#include <ATen/Functions.h>
#include <ATen/NativeFunctions.h>
#else
#include <ATen/ops/_sparse_coo_tensor_with_dims_and_tensors.h>
#include <ATen/ops/arange.h>
#include <ATen/ops/empty.h>
#include <Loops.h>
#endif

#include "comm/ATDispatch.h"

#if defined(_WIN32) || defined(_WIN64)
// Temporarily disable __restrict on Windows,
// as it turns out not all MSVC versions are aware of it.
// #define RESTRICT __restrict
#define RESTRICT
#else
#define RESTRICT __restrict__
#endif

#define NAME "compressed_index_invariance_checks_dpcpp"

namespace at {
namespace AtenIpexTypeXPU {
using namespace at::native;

namespace {

// NOTE: all the checks but the very last one are designed
// to work with vectors.
// To enable vectorization one would need to write a conversion
// Vec -> bool and make kernel launchers call into vectorized
// execution paths.

// All the invariants are described in
// https://pearu.github.io/bsr_tensor_invariants.html NOTE: in the code we also
// use `cidx/idx` to refer to `compressed_indices/plain_indices` respectively.

void _assert(const bool cond, const char* const message) {
  // TORCH_CHECK(cond, message);
  SYCL_KERNEL_ASSERT(cond && message);
}

enum class CDimName : bool { CRow, CCol };

// Invariant 5.1
// compressed_index[..., 0] == 0.
template <CDimName cdim_name, typename index_t>
void _check_first_cidx_is_zero(const index_t& cidx, const index_t& zero) {
  const bool invariant = cidx == zero;
  if (cdim_name == CDimName::CRow) {
    _assert(invariant, "`crow_indices[..., 0] == 0` is not satisfied.");
  } else {
    _assert(invariant, "`ccol_indices[..., 0] == 0` is not satisfied.");
  }
}

// Invariant 5.2
// compressed_index[..., -1] == nnz.
template <CDimName cdim_name, typename index_t>
void _check_last_cidx_is_nnz(const index_t& cidx, const index_t& nnz) {
  const bool invariant = cidx == nnz;
  if (cdim_name == CDimName::CRow) {
    _assert(invariant, "`crow_indices[..., -1] == nnz` is not satisfied.");
  } else {
    _assert(invariant, "`ccol_indices[..., -1] == nnz` is not satisfied.");
  }
}

// Invariant 5.3
// 0 <= compressed_indices[..., 1:] - compressed_indices[..., :-1] <= plain_dim.
template <CDimName cdim_name, typename index_t>
void _check_cidx_nondecreasing_locally_bounded_sequence(
    const index_t& cidx,
    const index_t& cidx_next,
    const index_t& zero,
    const index_t& dim) {
  const auto s_cidx = cidx_next - cidx;
  const bool invariant = zero <= s_cidx && s_cidx <= dim;
  if (cdim_name == CDimName::CRow) {
    _assert(
        invariant,
        "`0 <= crow_indices[..., 1:] - crow_indices[..., :-1] <= ncols` is not satisfied.");
  } else {
    _assert(
        invariant,
        "`0 <= ccol_indices[..., 1:] - ccol_indices[..., :-1] <= nrows` is not satisfied.");
  }
}

// Invariants 5.4 and 5.5
// 0 <= plain_index < plain_dim.
template <CDimName cdim_name, typename index_t>
void _check_idx_bounds(
    const index_t& idx,
    const index_t& zero,
    const index_t& dim) {
  const bool invariant = zero <= idx && idx < dim;
  if (cdim_name == CDimName::CRow) {
    _assert(invariant, "`0 <= col_indices < ncols` is not satisfied.");
  } else {
    _assert(invariant, "`0 <= row_indices < nrows` is not satisfied.");
  }
}

// Invariant 5.6
// plain_indices[..., compressed_indices[..., i - 1]:compressed_indices[..., i]]
// for all i = 1, ..., compressed_dim
// are sorted and distinct along the last dimension values.
template <CDimName cdim_name, typename index_t>
void _check_idx_sorted_distinct_vals_slices_with_cidx(
    const index_t* RESTRICT ptr_idx_batch,
    const index_t cidx,
    const index_t cidx_next) {
  // Note that ptr_idx_batch = &idx[batch_idx] and is contiguous.
  const auto* RESTRICT slice_begin = ptr_idx_batch + cidx;
  const auto* RESTRICT slice_end = ptr_idx_batch + cidx_next;
  for (auto* RESTRICT curr = slice_begin + 1; curr < slice_end; ++curr) {
    const auto invariant = *(curr - 1) < *curr;
    if (cdim_name == CDimName::CRow) {
      _assert(
          invariant,
          "`col_indices[..., crow_indices[..., i - 1]:crow_indices[..., i]] "
          "for all i = 1, ..., nrows "
          "are sorted and distinct along the last dimension values` "
          "is not satisfied.");
    } else {
      _assert(
          invariant,
          "`row_indices[..., ccol_indices[..., i - 1]:ccol_indices[..., i]] "
          "for all i = 1, ..., ncols "
          "are sorted and distinct along the last dimension values` "
          "is not satisfied.");
    }
  }
}

static inline int64_t indexCount(IntArrayRef sizes) {
  int64_t res = 1;
  for (const auto& s : sizes) {
    res *= s;
  }
  return res;
}

template <typename func_t, typename vec_func_t>
struct EmptyVecKernel {
  static void launch(
      TensorIteratorBase& iter,
      const func_t& f,
      const vec_func_t& vec_f) {}
};

template <typename scalar_t>
using DummyVec = scalar_t;

template <
    template <typename func_t>
    class kernel_t,
    template <typename func_t, typename vec_func_t>
    class vec_kernel_t>
struct KernelLauncher {
  template <typename func_t, typename vec_func_t>
  static void launch(
      TensorIteratorBase& iter,
      const func_t& f,
      const vec_func_t& vec_f) {
    vec_kernel_t<func_t, vec_func_t>::launch(iter, f, vec_f);
  }

  template <typename func_t>
  static void launch(TensorIteratorBase& iter, const func_t& f) {
    kernel_t<func_t>::launch(iter, f);
  }
};

template <CDimName cdim_name, typename index_t>
struct _validate_compressed_sparse_indices_kernel_functor {
  index_t operator()(index_t idx) const {
    _check_idx_bounds<cdim_name, index_t>(idx, zero, dim);
    return 0;
  }

  _validate_compressed_sparse_indices_kernel_functor(
      const int64_t dim,
      index_t zero)
      : dim(dim), zero(zero) {}

 private:
  const int64_t dim;
  index_t zero;
};

template <CDimName cdim_name, typename index_t>
struct _validate_compressed_sparse_indices_kernel_functor_2 {
  index_t operator()(
      index_t cidx_first,
      index_t cidx_last,
      index_t cidx_curr,
      index_t cidx_next,
      index_t batch_idx) const {
    // Invariant 5.1
    _check_first_cidx_is_zero<cdim_name, index_t>(cidx_first, zero);
    // Invariant 5.2
    _check_last_cidx_is_nnz<cdim_name, index_t>(cidx_last, nnz);
    // Invariant 5.3
    _check_cidx_nondecreasing_locally_bounded_sequence<cdim_name, index_t>(
        cidx_curr, cidx_next, zero, dim);
    // Invariant 5.6
    // NOTE: the implementation below is sync-less, but,
    // unfortunately, work is not guaranteed to be well-balanced
    // between different threads. idx is contiguous and of shape
    // (..., nnz), so batches are multiples of nnz apart.
    const auto* RESTRICT ptr_idx_batch = ptr_idx + batch_idx * nnz;
    _check_idx_sorted_distinct_vals_slices_with_cidx<cdim_name, index_t>(
        ptr_idx_batch, cidx_curr, cidx_next);
    return 0;
  }

  _validate_compressed_sparse_indices_kernel_functor_2(
      const int64_t dim,
      const int64_t nnz,
      index_t zero,
      const index_t* RESTRICT ptr_idx)
      : dim(dim), nnz(nnz), zero(zero), ptr_idx(ptr_idx) {}

 private:
  const int64_t dim;
  const int64_t nnz;
  index_t zero;
  const index_t* RESTRICT ptr_idx;
};

template <
    CDimName cdim_name,
    template <typename func_t>
    class kernel_t,
    template <typename func_t, typename vec_func_t>
    class vec_kernel_t = EmptyVecKernel,
    template <typename scalar_t> class Vec = DummyVec>
void _validate_compressed_sparse_indices_kernel(
    const Tensor& cidx,
    const Tensor& idx,
    const int64_t cdim,
    const int64_t dim,
    const int64_t nnz) {
  if (cdim_name == CDimName::CRow) {
    TORCH_CHECK(
        cidx.size(-1) == cdim + 1,
        "crow_indices have wrong shape: ",
        "crow_indices.shape[-1] = ",
        cidx.size(-1),
        " is not equal to ",
        "nrows + 1 = ",
        cdim + 1);
    TORCH_CHECK(
        idx.size(-1) == nnz,
        "col_indices have wrong shape: ",
        "col_indices.shape[-1] = ",
        idx.size(-1),
        " is not equal to ",
        "nnz = ",
        nnz);
  } else {
    TORCH_CHECK(
        cidx.size(-1) == cdim + 1,
        "ccol_indices have wrong shape: ",
        "ccol_indices.shape[-1] = ",
        cidx.size(-1),
        " is not equal to ",
        "ncols + 1 = ",
        cdim + 1);
    TORCH_CHECK(
        idx.size(-1) == nnz,
        "row_indices have wrong shape: ",
        "row_indices.shape[-1] = ",
        idx.size(-1),
        " is not equal to ",
        "nnz = ",
        nnz);
  }

  using KernelLauncher = KernelLauncher<kernel_t, vec_kernel_t>;

  // For TensorIterator's output: no void lambdas.
  const auto dummy = at::empty({1}, cidx.options());

  // Invariants 5.4 and 5.5
  {
    auto iter = TensorIteratorConfig()
                    .set_check_mem_overlap(false)
                    .add_owned_output(dummy.expand_as(idx))
                    .add_input(idx)
                    .build();

    IPEX_DISPATCH_INDEX_TYPES(idx.scalar_type(), NAME, [&iter, dim]() {
      const auto zero = index_t{0};
      _validate_compressed_sparse_indices_kernel_functor<cdim_name, index_t> f(
          dim, zero);
      KernelLauncher::launch(iter, f);
    });
  }

  // Invariants 5.1, 5.2, 5.3, 5.6
  {
    const auto cidx_first = cidx.slice(-1, 0, 1);
    const auto cidx_last = cidx.slice(-1, cdim, cdim + 1);

    const auto cidx_curr = cidx.slice(-1, 0, cdim);
    const auto cidx_next = cidx.slice(-1, 1, cdim + 1);

    const auto batch_dims = cidx.sizes().slice(0, cidx.dim() - 1);
    const auto batch_count = indexCount(batch_dims);
    const auto batch_idx =
        at::arange(batch_count, cidx.options()).view(batch_dims).unsqueeze_(-1);

    auto iter = TensorIteratorConfig()
                    .set_check_mem_overlap(false)
                    .add_owned_output(dummy.expand_as(cidx_curr))
                    .add_input(cidx_first)
                    .add_input(cidx_last)
                    .add_input(cidx_curr)
                    .add_input(cidx_next)
                    .add_input(batch_idx)
                    .build();

    IPEX_DISPATCH_INDEX_TYPES(
        idx.scalar_type(), NAME, [&iter, &idx, dim, nnz]() {
          const auto* RESTRICT ptr_idx = idx.data_ptr<index_t>();
          const auto zero = index_t{0};
          _validate_compressed_sparse_indices_kernel_functor_2<
              cdim_name,
              index_t>
              f(dim, nnz, zero, ptr_idx);
          KernelLauncher::launch(iter, f);
        });
  }
}

template <
    template <typename func_t>
    class kernel_t,
    template <typename func_t, typename vec_func_t>
    class vec_kernel_t = EmptyVecKernel,
    template <typename scalar_t> class Vec = DummyVec>
void validate_compressed_sparse_indices_kernel(
    const bool is_crow,
    const Tensor& cidx,
    const Tensor& idx,
    const int64_t cdim,
    const int64_t dim,
    const int64_t nnz) {
  if (is_crow) {
    _validate_compressed_sparse_indices_kernel<
        CDimName::CRow,
        kernel_t,
        vec_kernel_t,
        Vec>(cidx, idx, cdim, dim, nnz);
  } else {
    _validate_compressed_sparse_indices_kernel<
        CDimName::CCol,
        kernel_t,
        vec_kernel_t,
        Vec>(cidx, idx, cdim, dim, nnz);
  }
}

} // namespace

} // namespace AtenIpexTypeXPU
} // namespace at
