#include <ATen/CPUFunctions.h>
#include <ATen/TensorUtils.h>
#include <ATen/detail/XPUHooksInterface.h>
#include <ATen/xpu/PinnedMemoryAllocator.h>
#include <c10/core/Storage.h>
#include <core/Allocator.h>
#include <core/detail/TensorInfo.h>

#include <utils/DPCPP.h>
#include "comm/RegistrationDeclarations.h"

using namespace torch_ipex::xpu::dpcpp::detail;
using namespace torch_ipex::xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {

// Note: The user must call is_pinned(device='xpu') to explicitly call here.
bool is_pinned(const Tensor& self, c10::optional<Device> device) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      !device.has_value() || device->type() == c10::DeviceType::XPU);

  return at::detail::getXPUHooks().isPinnedPtr(self.storage().data());
}

// Note: The user must call tensor.pin_memory(device='xpu') to explicitly call
// here.
Tensor _pin_memory(const Tensor& self, c10::optional<Device> device) {
  TORCH_INTERNAL_ASSERT_DEBUG_ONLY(
      !device.has_value() || device->type() == c10::DeviceType::XPU);

  auto* allocator = at::xpu::getPinnedMemoryAllocator();
  auto storage = c10::Storage(
      c10::Storage::use_byte_size_t(),
      at::detail::computeStorageNbytes(
          self.sizes(), self.strides(), self.dtype().itemsize()),
      allocator,
      /*resizable=*/false);
  auto tensor = at::cpu::empty({0}, self.options())
                    .set_(storage, 0, self.sizes(), self.strides());
  tensor.copy_(self);
  return tensor;
}

} // namespace AtenIpexTypeXPU
} // namespace at
