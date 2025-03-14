#include <ATen/native/Fill.h>
#include <ATen/native/TensorIterator.h>
#include <core/detail/IndexUtils.h>

#include <runtime/Utils.h>
#include "comm/ATDispatch.h"
#include "comm/ApplyUtils.h"
#include "comm/RegistrationDeclarations.h"

#include "Loops.h"

using namespace torch_ipex::xpu::dpcpp::detail;
using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {
namespace impl {

template <typename T>
struct TensorFillOp {
  TensorFillOp(T v) : val(v) {}
  inline void operator()(T& v) const {
    v = val;
  }

  const T val;
};

template <typename scalar_t>
struct fill_kernel_dpcpp_functor {
  scalar_t operator()() const {
    return fill;
  }

  fill_kernel_dpcpp_functor(scalar_t fill) : fill(fill) {}

 private:
  scalar_t fill;
};

void fill_kernel_dpcpp(TensorIterator& iter, Scalar value) {
  IPEX_DISPATCH_ALL_TYPES_AND_COMPLEX_AND6(
      kComplexHalf,
      kHalf,
      kBFloat16,
      kBool,
      kFloat8_e4m3fn,
      kFloat8_e5m2,
      iter.dtype(),
      "fill_dpcpp",
      [&] {
        scalar_t fill = value.to<scalar_t>();
        fill_kernel_dpcpp_functor<scalar_t> f(fill);
        dpcpp_kernel_for_tensor_iter(iter, f);
      });
}

template <typename IndexType, int Dim>
struct FillSliceWithIndexKernelFunctor {
  void operator()(sycl::nd_item<1> item_id) const {
    IndexType local_id = item_id.get_local_id(0);
    IndexType slice = item_id.get_group_linear_id();
    const uint64_t offset = IndexToOffset<int64_t, IndexType>::get(slice, out);
    int64_t* base = out_data + offset;

    for (IndexType i = local_id; i < sliceSize;
         i += item_id.get_local_range(0)) {
      // Torch indices are 1-based (hence the +1)
      base[i * sliceStride] = i /* + TH_INDEX_BASE */;
    }
  }
  FillSliceWithIndexKernelFunctor(
      TensorInfo<int64_t, IndexType> out_,
      IndexType sliceSize_,
      IndexType sliceStride_,
      int64_t* out_data_)
      : out(out_),
        sliceSize(sliceSize_),
        sliceStride(sliceStride_),
        out_data(out_data_) {}

 private:
  TensorInfo<int64_t, IndexType> out;
  IndexType sliceSize;
  IndexType sliceStride;
  int64_t* out_data;
};

template <typename IndexType, int Dim>
void fillSliceWithIndex(
    TensorInfo<int64_t, IndexType> out,
    IndexType totalSlices,
    IndexType sliceSize,
    IndexType sliceStride) {
  auto& queue = dpcppGetCurrentQueue();
  auto dev_id = dpcppGetDeviceIdOfCurrentQueue();
  int64_t local_size = dpcppMaxWorkGroupSize(dev_id);
  auto cgf = DPCPP_Q_CGF(cgh) {
    auto out_data = out.data;
    FillSliceWithIndexKernelFunctor<IndexType, Dim> kfn(
        out, sliceSize, sliceStride, out_data);
    cgh.parallel_for<decltype(kfn)>(
        sycl::nd_range<1>(
            sycl::range<1>(totalSlices * local_size),
            sycl::range<1>(local_size)),
        kfn);
  };

  DPCPP_Q_SUBMIT(queue, cgf);
}

} // namespace impl

Tensor& fill_out(Tensor& self, Scalar value) {
  auto iter = TensorIterator::nullary_op(self);
  impl::fill_kernel_dpcpp(iter, value);
  return self;
}

Tensor& fill_(Tensor& self, const Scalar& value) {
  return fill_out(self, value);
}

Tensor& fill_(Tensor& self, const Tensor& value) {
  TORCH_CHECK(
      value.dim() == 0,
      "fill_ only supports 0-dimension value tensor but got tensor with ",
      value.dim(),
      " dimensions.");
  return fill_out(self, value.item());
}

Tensor& zero_(Tensor& self) {
  return at::AtenIpexTypeXPU::fill_(self, 0);
}

Tensor& fill_slice_with_index(Tensor& t, int dim) {
  int64_t dims = t.dim() == 0 ? 1 : t.dim();
  TORCH_CHECK(dims <= MAX_DPCPPTORCH_DIMS, DPCPPTORCH_DIM_WARNING);
  TORCH_CHECK(
      t.scalar_type() == at::kLong || t.scalar_type() == at::kInt,
      "non integer tensor");

  ptrdiff_t inElements = t.numel();
  if (inElements > 0) {
    int64_t sliceSize = t.dim() == 0 ? 1 : t.size(dim);
    ptrdiff_t numSlices = inElements / sliceSize;

#define FILL_INDEX(T, DIM)          \
  impl::fillSliceWithIndex<T, DIM>( \
      info, numSlices, sliceSize, info.strides[collapseDim])

    if (canUse32BitIndexMath(t)) {
      TensorInfo<int64_t, uint32_t> info =
          getTensorInfo<int64_t, unsigned int>(t);
      info.reduceDim(dim);
      int collapseDim = info.collapseDims(dim);
      if (info.isContiguous()) {
        FILL_INDEX(unsigned int, -2);
      } else {
        if (info.dims == 1) {
          FILL_INDEX(unsigned int, 1);
        } else if (info.dims == 2) {
          FILL_INDEX(unsigned int, 2);
        } else {
          FILL_INDEX(unsigned int, -1);
        }
      }
    } else {
      TensorInfo<int64_t, uint64_t> info = getTensorInfo<int64_t, uint64_t>(t);
      info.reduceDim(dim);
      int collapseDim = info.collapseDims(dim);

      // catch-all implementation
      FILL_INDEX(uint64_t, -1);
    }
  }

  return t;
}

} // namespace AtenIpexTypeXPU
} // namespace at
