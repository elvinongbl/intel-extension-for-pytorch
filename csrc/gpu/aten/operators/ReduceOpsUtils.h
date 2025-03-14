#pragma once

#include <ATen/WrapDimUtils.h>
#include <ATen/WrapDimUtilsMulti.h>
#include <ATen/core/DimVector.h>
#include <ATen/native/ReduceOps.h>
#include <ATen/native/ReduceOpsUtils.h>
#include <ATen/native/SharedReduceOps.h>
#include <ATen/native/TensorIterator.h>

#include "Reduce.h"

using namespace at::native;
using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {

static inline void check_result_is_bytebool(
    const char* name,
    const Tensor& self,
    const Tensor& result) {
  if (result.defined()) {
    // Refer [all, any : uint8 compatibility]
    TORCH_CHECK(
        result.scalar_type() == ScalarType::Bool ||
            result.scalar_type() == ScalarType::Byte,
        name,
        " only supports bool tensor for result, got: ",
        result.scalar_type());
  }
}

static inline ScalarType get_dtype_from_self(
    const Tensor& self,
    const optional<ScalarType>& dtype,
    bool promote_integers) {
  if (dtype.has_value()) {
    return dtype.value();
  }
  ScalarType src_type = self.scalar_type();
  if (promote_integers && at::isIntegralType(src_type, /*includeBool=*/true)) {
    return kLong;
  }
  return src_type;
}

static inline ScalarType get_dtype_from_result(
    Tensor& result,
    optional<ScalarType> dtype) {
  TORCH_CHECK(
      result.defined(),
      "Cannot create a new tensor inside a reduction op. You likely tried to call an operator with an out argument but the out argument was an undefined tensor.");
  if (dtype.has_value()) {
    return dtype.value();
  } else {
    return result.scalar_type();
  }
}

static inline ScalarType get_result_or_bytebool_dtype(
    const Tensor& self,
    const Tensor& result) {
  // Refer [all, any : uint8 compatibility]
  if (result.defined()) {
    return result.scalar_type();
  } else {
    return (self.scalar_type() == kByte) ? kByte : kBool;
  }
}

static void zero_numel_check_dims(
    const Tensor& self,
    const int64_t dim,
    const char* fn_name) {
  if (self.ndimension() == 0) {
    TORCH_CHECK_INDEX(
        dim == 0 || dim == -1,
        fn_name,
        ": Expected reduction dim -1 or 0 for scalar but got ",
        dim);
  } else {
    TORCH_CHECK_INDEX(
        self.size(dim) != 0,
        fn_name,
        ": Expected reduction dim ",
        dim,
        " to have non-zero size.");
  }
}

static void zero_numel_check_dims(
    const Tensor& self,
    const IntArrayRef dim,
    const char* fn_name) {
  for (const int64_t d : dim) {
    zero_numel_check_dims(self, d, fn_name);
  }
}

static ScalarType get_dtype(
    Tensor& result,
    const Tensor& self,
    optional<ScalarType> dtype,
    bool promote_integers = false) {
  if (dtype.has_value()) {
    return dtype.value();
  } else if (result.defined()) {
    return result.scalar_type();
  }
  ScalarType src_type = self.scalar_type();
  if (promote_integers && at::isIntegralType(src_type, /*includeBool=*/true)) {
    return kLong;
  }
  return src_type;
}

static inline int64_t ensure_nonempty_dim(int64_t dim) {
  return std::max<int64_t>(dim, 1);
}

static inline int64_t ensure_nonempty_size(const Tensor& t, int64_t dim) {
  return t.dim() == 0 ? 1 : t.size(dim);
}

static inline int64_t ensure_nonempty_stride(const Tensor& t, int64_t dim) {
  return t.dim() == 0 ? 1 : t.stride(dim);
}

using IdxVec = std::vector<int64_t>;
static inline IdxVec ensure_nonempty_vec(IdxVec vec) {
  if (vec.size() == 0) {
    vec.push_back(1);
  }
  return vec;
}

namespace meta {

using DimMask = TensorIterator::DimMask;

static DimMask make_dim_mask(IntArrayRef dims, int64_t ndim) {
  auto mask = DimMask();
  if (dims.empty()) {
    mask.flip();
  } else {
    for (int64_t dim : dims) {
      mask = at::dim_list_to_bitset(dims, ndim);
    }
  }
  return mask;
}

static void allocate_reduction_result(
    Tensor& result,
    const Tensor& self,
    DimMask mask,
    bool keepdim,
    ScalarType dtype) {
  auto shape = DimVector(self.sizes());
  for (int dim = shape.size() - 1; dim >= 0; dim--) {
    if (mask[dim]) {
      if (keepdim) {
        shape[dim] = 1;
      } else {
        shape.erase(shape.begin() + dim);
      }
    }
  }
  if (result.defined()) {
    result.resize_(shape);
  } else {
    result = at::empty(shape, self.options().dtype(dtype));
  }
}

static Tensor review_reduce_result(
    const Tensor& result,
    int ndim,
    DimMask mask,
    bool keepdim) {
  if (keepdim) {
    return result;
  }
  auto shape = DimVector(result.sizes());
  auto stride = DimVector(result.strides());
  for (int dim = 0; dim < ndim; dim++) {
    if (mask[dim]) {
      shape.insert(shape.begin() + dim, 1);
      stride.insert(stride.begin() + dim, 0);
    }
  }
  return result.as_strided(shape, stride);
}

static TensorIterator make_reduction(
    const char* name,
    Tensor& result,
    const Tensor& self,
    at::OptionalIntArrayRef dim_opt,
    bool keepdim,
    ScalarType in_dtype,
    ScalarType out_dtype) {
  // check that result type and dtype match if provided
  std::string opt_dtype = toString(out_dtype);
  opt_dtype[0] += 32;
  std::string out_dtype_str = toString(result.scalar_type());
  out_dtype_str[0] += 32;
  TORCH_CHECK(
      !result.defined() || result.scalar_type() == out_dtype,
      "Expected out tensor to have dtype ",
      opt_dtype,
      ", but got ",
      out_dtype_str,
      " instead");
  IntArrayRef dim = dim_opt.value_or(IntArrayRef{});
  int64_t ndim = self.dim();
  auto mask = make_dim_mask(dim, ndim);
  allocate_reduction_result(result, self, mask, keepdim, out_dtype);
  auto viewed_result = review_reduce_result(result, ndim, mask, keepdim);
  namedinference::propagate_names_for_reduction(result, self, dim, keepdim);
  if (self.scalar_type() == in_dtype) {
    return TensorIterator::reduce_op(viewed_result, self);
  }
  return TensorIterator::reduce_op(viewed_result, self.to(in_dtype));
}

static TensorIterator make_reduction(
    const char* name,
    Tensor& result,
    const Tensor& self,
    at::OptionalIntArrayRef dim,
    bool keepdim,
    ScalarType out_dtype) {
  // special case for type promotion in mixed precision, improves computational
  // efficiency.
  // not generalize this to common mismatched input/output types to avoid cross
  // product of templated kernel launches.
  const bool gpu_lowp_to_f32 =
      (self.is_xpu() &&
       (self.scalar_type() == kHalf || self.scalar_type() == kBFloat16) &&
       out_dtype == kFloat);
  auto in_dtype = gpu_lowp_to_f32 ? self.scalar_type()
      : self.is_complex()         ? c10::toComplexType(out_dtype)
                                  : out_dtype;
  return make_reduction(name, result, self, dim, keepdim, out_dtype, out_dtype);
}

static TensorIterator make_reduction(
    const char* name,
    Tensor& result1,
    Tensor& result2,
    const Tensor& self,
    at::OptionalIntArrayRef dim_opt,
    bool keepdim,
    ScalarType dtype1,
    ScalarType dtype2) {
  // check that result type and dtype match if provided
  TORCH_CHECK(
      (!result1.defined() || result1.scalar_type() == dtype1) &&
          (!result2.defined() || result2.scalar_type() == dtype2),
      name,
      ": provided dtype must match dtype of result. Got ",
      toString(result1.scalar_type()),
      toString(result2.scalar_type()),
      " and ",
      toString(dtype1),
      toString(dtype2),
      ".");

  // dim={} performs an all-reduce, same as dim=None
  auto dim = dim_opt.value_or(IntArrayRef{});
  int64_t ndim = self.dim();
  DimMask mask = make_dim_mask(dim, ndim);
  allocate_reduction_result(result1, self, mask, keepdim, dtype1);
  auto viewed_result1 = review_reduce_result(result1, ndim, mask, keepdim);

  allocate_reduction_result(result2, self, mask, keepdim, dtype2);
  auto viewed_result2 = review_reduce_result(result2, ndim, mask, keepdim);

  namedinference::propagate_names_for_reduction(result1, self, dim, keepdim);
  namedinference::propagate_names_for_reduction(result2, self, dim, keepdim);

  // special case for type promotion in mixed precision, improves computational
  // efficiency.
  // We don't generalize this to common mismatched input/output types to avoid
  // cross product of templated kernel launches.
  if (self.scalar_type() == dtype1 ||
      (self.is_xpu() && self.scalar_type() == kHalf && dtype1 == kFloat)) {
    return TensorIterator::reduce_op(viewed_result1, viewed_result2, self);
  }
  return TensorIterator::reduce_op(
      viewed_result1, viewed_result2, self.to(dtype1));
}

static TensorIterator make_reduction(
    const char* name,
    Tensor& result1,
    Tensor& result2,
    const Tensor& self,
    at::OptionalIntArrayRef dim,
    bool keepdim,
    ScalarType dtype) {
  if ((result1.defined() && dtype != result1.scalar_type()) ||
      (result2.defined() && dtype != result2.scalar_type())) {
    std::string result_dtype_str = dtype == result1.scalar_type()
        ? toString(result2.scalar_type())
        : toString(result1.scalar_type());
    result_dtype_str[0] += 32;
    std::string self_dtype_str = toString(dtype);
    self_dtype_str[0] += 32;
    TORCH_CHECK(
        false,
        "Expected out tensor to have dtype ",
        self_dtype_str,
        ", but got ",
        result_dtype_str,
        " instead");
  }
  return make_reduction(
      name, result1, result2, self, dim, keepdim, dtype, dtype);
}

} // namespace meta

} // namespace AtenIpexTypeXPU
} // namespace at
