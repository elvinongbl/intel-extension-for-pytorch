#include <ATen/ATen.h>

#include <core/Memory.h>
#include <runtime/Utils.h>
#include <utils/DPCPP.h>
#include <iterator>
#include <tuple>

#include "BitonicMergeSort.h"
#include "PSTLFunctions.h"
#include "comm/ATDispatch.h"
#include "comm/RegistrationDeclarations.h"

using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {
namespace impl {

template <typename index_t>
struct compute_inverse_lt_functor {
  auto operator()(index_t a, index_t b) const {
    return Numerics<index_t>::lt(a, b);
  }
};

template <typename input_t, typename index_t, typename not_equal_t>
Tensor compute_inverse(
    const Tensor& sorted,
    int64_t num_inp,
    const Tensor& sorted_indices,
    const bool return_inverse,
    TensorOptions index_options,
    not_equal_t not_equal) {
  // inverse indices
  Tensor inverse_indices;
  input_t* data = sorted.data_ptr<input_t>();
  auto data_begin = data;
  if (!return_inverse) {
    inverse_indices = at::empty({0}, index_options);
  } else {
    TORCH_CHECK(
        sorted_indices.defined(),
        "compute_inverse is invoked, but sorted_indices is undefined. Send a bug report!");
    index_t* sorted_indices_ptr = sorted_indices.data_ptr<index_t>();
    auto sorted_indices_begin = sorted_indices_ptr;
    Tensor inv_loc = at::empty({num_inp}, index_options);
    inverse_indices = at::empty({num_inp}, index_options);
    index_t* inv_loc_ptr = inv_loc.data_ptr<index_t>();
    auto inv_loc_begin = inv_loc_ptr;
    torch_ipex::xpu::pstl::adjacent_difference<index_t>(
        data_begin, data_begin + num_inp, inv_loc_begin, not_equal);
    inv_loc[0] = 0;
    at::AtenIpexTypeXPU::scan<INCLUSIVE_TYPE, index_t, index_t>(
        inv_loc,
        inv_loc,
        /*dim*/ 0,
        ScalarConvert<float, index_t>::to(0.0),
        AddOp<index_t>());
    compute_inverse_lt_functor<index_t> f;
    torch_ipex::xpu::pstl::sort<index_t, index_t>(
        sorted_indices_ptr, inv_loc_ptr, sorted_indices.size(0), f);
    inverse_indices = inv_loc;
  }

  return inverse_indices;
}

template <typename input_t, typename index_t, typename equal_t>
std::tuple<Tensor, index_t> compute_unique(
    input_t* data,
    int64_t num_inp,
    const Tensor& sorted_indices,
    const bool return_counts,
    TensorOptions index_options,
    equal_t equal) {
  auto data_begin = data;
  // unique and count
  Tensor counts = at::empty({0}, index_options);
  int64_t num_out;
  if (!return_counts) {
    num_out = torch_ipex::xpu::pstl::unique<input_t, index_t>(
                  data_begin, data_begin + num_inp, equal) -
        data_begin;
  } else {
    Tensor range = at::empty({num_inp + 1}, index_options);
    index_t* range_begin = range.data_ptr<index_t>();
    torch_ipex::xpu::pstl::iota(
        range_begin, range_begin + num_inp + 1, (index_t)0);
    auto data_end = data_begin;
    auto range_end = range_begin;
    std::tie(data_end, range_end) =
        torch_ipex::xpu::pstl::unique_with_zip<input_t, index_t, index_t>(
            data_begin, data_begin + num_inp, range_begin, equal);
    num_out = std::distance(data_begin, data_end);
    range[num_out] = num_inp;
    counts.resize_(num_out);
    int64_t* counts_ptr = counts.data_ptr<index_t>();
    auto counts_begin = counts_ptr;
    torch_ipex::xpu::pstl::adjacent_difference<index_t>(
        range_begin + 1, range_begin + num_out + 1, counts_begin);
  }

  return std::tuple<Tensor, index_t>(counts, num_out);
}

struct unique_template_cmp_functor {
  template <typename T>
  bool operator()(T lhs, T rhs) const {
    if (lhs != rhs) {
      return true;
    }
    return false;
  }
};

struct unique_template_cmp_functor_2 {
  template <typename T>
  bool operator()(T lhs, T rhs) const {
    if (lhs != rhs) {
      return false;
    }
    return true;
  }
};

template <typename scalar_t>
std::tuple<Tensor, Tensor, Tensor> unique_template(
    const Tensor& self,
    const bool consecutive,
    const bool return_inverse,
    const bool return_counts) {
  auto& dpcpp_queue = dpcppGetCurrentQueue();

  auto index_options = self.options().dtype(kLong);
  auto self_c = self.contiguous();
  Tensor output = self_c.clone().reshape(-1);
  int64_t num_inp = output.numel();
  Tensor sorted_indices = at::empty({num_inp}, index_options);
  Tensor inverse_indices = at::empty({num_inp}, index_options);
  Tensor counts = at::empty({num_inp}, index_options);
  auto sorted_indices_begin = sorted_indices.data_ptr<int64_t>();
  torch_ipex::xpu::pstl::iota(
      sorted_indices_begin, sorted_indices_begin + num_inp, (int64_t)0);

  if (num_inp > 0) {
    if (!consecutive) {
      torch_ipex::xpu::pstl::sort<scalar_t, int64_t>(
          self_c.data_ptr<scalar_t>(),
          output.data_ptr<scalar_t>(),
          sorted_indices.data_ptr<int64_t>(),
          num_inp,
          false);
    }

    int64_t num_out;

    scalar_t* output_data = output.data_ptr<scalar_t>();
    unique_template_cmp_functor cmp_functor;
    inverse_indices = compute_inverse<scalar_t, int64_t>(
        output,
        num_inp,
        sorted_indices,
        return_inverse,
        index_options,
        cmp_functor);

    unique_template_cmp_functor_2 cmp_functor_2;
    std::tie(counts, num_out) = compute_unique<scalar_t, int64_t>(
        output_data,
        num_inp,
        sorted_indices,
        return_counts,
        index_options,
        cmp_functor_2);
    output.resize_(num_out);
  }

  if (return_inverse) {
    inverse_indices.resize_(self.sizes());
  }

  return std::tuple<Tensor, Tensor, Tensor>(output, inverse_indices, counts);
}

template <typename scalar_t>
struct unique_dim_template_less_comp_functor {
  bool operator()(int64_t a, int64_t b) const {
    // this is a must to bypass padding comparision in bitonic sort.
    if (a >= num_inp || b >= num_inp)
      return a < b;
    // calculate the dictionary order
    for (int64_t i = 0; i < n; ++i) {
      scalar_t lhs = input_flat_ptr[i + a * n];
      scalar_t rhs = input_flat_ptr[i + b * n];
      if (lhs < rhs) {
        return true;
      } else if (lhs > rhs) {
        return false;
      }
    }
    return false;
  }

  unique_dim_template_less_comp_functor(
      int64_t num_inp,
      int64_t n,
      scalar_t* input_flat_ptr)
      : num_inp(num_inp), n(n), input_flat_ptr(input_flat_ptr) {}

 private:
  int64_t num_inp;
  int64_t n;
  scalar_t* input_flat_ptr;
};

template <typename scalar_t>
struct unique_dim_template_eq_comp_functor {
  template <typename T>
  bool operator()(T a, T b) const {
    for (int64_t i = 0; i < n; ++i) {
      scalar_t lhs = input_flat_ptr[i + a * n];
      scalar_t rhs = input_flat_ptr[i + b * n];
      if (lhs != rhs) {
        return false;
      }
    }
    return true;
  }
  unique_dim_template_eq_comp_functor(int64_t n, scalar_t* input_flat_ptr)
      : n(n), input_flat_ptr(input_flat_ptr) {}

 private:
  int64_t n;
  scalar_t* input_flat_ptr;
};

template <typename scalar_t>
struct unique_dim_template_noteq_comp_functor {
  template <typename T>
  bool operator()(T a, T b) const {
    for (int64_t i = 0; i < n; ++i) {
      scalar_t lhs = input_flat_ptr[i + a * n];
      scalar_t rhs = input_flat_ptr[i + b * n];
      if (lhs != rhs) {
        return true;
      }
    }
    return false;
  }
  unique_dim_template_noteq_comp_functor(int64_t n, scalar_t* input_flat_ptr)
      : n(n), input_flat_ptr(input_flat_ptr) {}

 private:
  int64_t n;
  scalar_t* input_flat_ptr;
};

template <typename scalar_t>
std::tuple<Tensor, Tensor, Tensor> unique_dim_template(
    const Tensor& self,
    const int64_t dim,
    const bool consecutive,
    const bool return_inverse,
    const bool return_counts) {
  auto& dpcpp_queue = dpcppGetCurrentQueue();

  auto sizes = self.sizes().vec();
  auto num_zero_dims = std::count(sizes.begin(), sizes.end(), 0);

  if (self.size(dim) == 0) {
    TORCH_CHECK(
        num_zero_dims == 1,
        "Number of zero sized dimensions is more than one, so unique cannot be applied ")
    Tensor output = at::empty({0}, self.options());
    Tensor inverse_indices = at::empty({0}, self.options().dtype(kLong));
    Tensor counts = at::empty({0}, self.options().dtype(kLong));

    return std::make_tuple(output, inverse_indices, counts);
  }

  TORCH_CHECK(
      num_zero_dims == 0,
      "There are 0 sized dimensions, and they aren't selected, so unique cannot be applied");

  int64_t num_inp = self.size(dim);
  auto index_options = self.options().dtype(kLong);
  Tensor input_flat = self.transpose(dim, 0).contiguous().view({num_inp, -1});
  int64_t n = input_flat.size(1);
  scalar_t* input_flat_ptr = input_flat.data_ptr<scalar_t>();

  Tensor indices = at::arange(0, num_inp, index_options);
  Tensor indices_idx = at::arange(0, num_inp, index_options);
  int64_t* indices_data = indices.data_ptr<int64_t>();
  auto indices_begin = indices_data;

  unique_dim_template_less_comp_functor<scalar_t> less_comp(
      num_inp, n, input_flat_ptr);
  unique_dim_template_eq_comp_functor<scalar_t> equal_comp(n, input_flat_ptr);
  unique_dim_template_noteq_comp_functor<scalar_t> not_equal_comp(
      n, input_flat_ptr);

  if (!consecutive) {
    torch_ipex::xpu::pstl::sort<int64_t, int64_t>(
        indices_begin, indices_idx.data_ptr<int64_t>(), num_inp, less_comp);
  }
  Tensor origin_indices = indices.clone();
  int64_t* origin_indices_data = origin_indices.data_ptr<int64_t>();

  Tensor inverse_indices, counts;
  int64_t num_out;

  inverse_indices = compute_inverse<int64_t, int64_t>(
      indices, num_inp, indices, return_inverse, index_options, not_equal_comp);

  std::tie(counts, num_out) = compute_unique<int64_t, int64_t>(
      origin_indices_data,
      num_inp,
      origin_indices,
      return_counts,
      index_options,
      equal_comp);
  origin_indices.resize_(num_out);
  return std::tuple<Tensor, Tensor, Tensor>(
      self.index_select(dim, origin_indices), inverse_indices, counts);
}

} // namespace impl

std::tuple<Tensor, Tensor> _unique(
    const Tensor& self,
    const bool sorted,
    const bool return_inverse) {
  return IPEX_DISPATCH_ALL_TYPES_AND3(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      at::ScalarType::Bool,
      self.scalar_type(),
      "_unique",
      [&] {
        Tensor output, inverse;
        std::tie(output, inverse, std::ignore) =
            impl::unique_template<scalar_t>(self, false, return_inverse, false);
        return std::make_tuple(output, inverse);
      });
}

std::tuple<Tensor, Tensor, Tensor> _unique2(
    const Tensor& self,
    const bool sorted,
    const bool return_inverse,
    const bool return_counts) {
  return IPEX_DISPATCH_ALL_TYPES_AND3(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      at::ScalarType::Bool,
      self.scalar_type(),
      "_unique2",
      [&] {
        return impl::unique_template<scalar_t>(
            self, false, return_inverse, return_counts);
      });
}

std::tuple<Tensor, Tensor, Tensor> unique_dim(
    const Tensor& self,
    const int64_t dim,
    const bool sorted,
    const bool return_inverse,
    const bool return_counts) {
  return IPEX_DISPATCH_ALL_TYPES_AND3(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      at::ScalarType::Bool,
      self.scalar_type(),
      "unique_dim",
      [&] {
        return impl::unique_dim_template<scalar_t>(
            self, dim, false, return_inverse, return_counts);
      });
}

std::tuple<Tensor, Tensor, Tensor> unique_dim_consecutive(
    const Tensor& self,
    const int64_t dim,
    const bool return_inverse,
    const bool return_counts) {
  return IPEX_DISPATCH_ALL_TYPES_AND3(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      at::ScalarType::Bool,
      self.scalar_type(),
      "unique_dim_consecutive",
      [&] {
        return impl::unique_dim_template<scalar_t>(
            self, dim, true, return_inverse, return_counts);
      });
}

std::tuple<Tensor, Tensor, Tensor> unique_consecutive(
    const Tensor& self,
    const bool return_inverse,
    const bool return_counts,
    c10::optional<int64_t> dim) {
  if (!dim.has_value()) {
    return IPEX_DISPATCH_ALL_TYPES_AND3(
        at::ScalarType::BFloat16,
        at::ScalarType::Half,
        at::ScalarType::Bool,
        self.scalar_type(),
        "unique_consecutive",
        [&] {
          return impl::unique_template<scalar_t>(
              self, true, return_inverse, return_counts);
        });
  }
  return at::AtenIpexTypeXPU::unique_dim_consecutive(
      self, dim.value(), return_inverse, return_counts);
}

} // namespace AtenIpexTypeXPU
} // namespace at
