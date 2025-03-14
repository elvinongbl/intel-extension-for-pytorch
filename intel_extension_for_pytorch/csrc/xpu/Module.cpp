#include <torch/csrc/Dtype.h>
#include <torch/csrc/Exceptions.h>
#include <torch/csrc/THP.h>
#include <torch/csrc/tensor/python_tensor.h>
#include <torch/csrc/utils/pycfunction_helpers.h>

#include <ATen/autocast_mode.h>

#include <ATen/xpu/XPUGeneratorImpl.h>
#include <core/Allocator.h>
#include <include/xpu/Settings.h>
#include <profiler/profiler_kineto.h>
#include <pybind11/stl.h>
#include <utils/Settings.h>
#include "Module.h"

#include <thread>

#define ASSERT_TRUE(cmd) \
  if (!(cmd))            \
  return

namespace torch_ipex::xpu {

PyObject* module;

static PyObject* THPModule_initExtension(PyObject* self, PyObject* noargs) {
  HANDLE_TH_ERRORS
  // initialize ipex module
  Py_RETURN_NONE;
  END_HANDLE_TH_ERRORS
}

PyObject* THPModule_xpu_CachingAllocator_raw_alloc(
    PyObject* self,
    PyObject* args) {
  HANDLE_TH_ERRORS
  PyObject* size_o = nullptr;
  if (!PyArg_ParseTuple(args, "O", &size_o)) {
    THPUtils_invalidArguments(
        args, nullptr, "caching_allocator_alloc", 1, "(ssize_t size)");
    return nullptr;
  }
  auto size = PyLong_AsSsize_t(size_o);
  void* mem = c10::GetAllocator(c10::DeviceType::XPU)->raw_allocate(size);
  return PyLong_FromVoidPtr(mem);
  END_HANDLE_TH_ERRORS
}

PyObject* THPModule_xpu_CachingAllocator_delete(
    PyObject* _unused,
    PyObject* obj) {
  HANDLE_TH_ERRORS
  void* mem_ptr = PyLong_AsVoidPtr(obj);
  c10::GetAllocator(c10::DeviceType::XPU)->raw_deallocate(mem_ptr);
  Py_RETURN_NONE;
  END_HANDLE_TH_ERRORS
}

PyObject* THPModule_resetPeakMemoryStats(PyObject* _unused, PyObject* arg) {
  HANDLE_TH_ERRORS
  TORCH_CHECK(
      THPUtils_checkLong(arg), "invalid argument to reset_peak_memory_stats");
  const int device = (int)THPUtils_unpackLong(arg);
  torch_ipex::xpu::dpcpp::resetPeakStatsInDevAlloc(device);
  END_HANDLE_TH_ERRORS
  Py_RETURN_NONE;
}

PyObject* THPModule_resetAccumulatedMemoryStats(
    PyObject* _unused,
    PyObject* arg) {
  HANDLE_TH_ERRORS
  TORCH_CHECK(
      THPUtils_checkLong(arg),
      "invalid argument to reset_accumulated_memory_stats");
  const int device = (int)THPUtils_unpackLong(arg);
  torch_ipex::xpu::dpcpp::resetAccumulatedStatsInDevAlloc(device);
  END_HANDLE_TH_ERRORS
  Py_RETURN_NONE;
}

PyObject* THPModule_emptyCache(PyObject* _unused, PyObject* noargs) {
  HANDLE_TH_ERRORS
  torch_ipex::xpu::dpcpp::emptyCacheInDevAlloc();
  END_HANDLE_TH_ERRORS
  Py_RETURN_NONE;
}

PyObject* THPModule_memoryStats(PyObject* _unused, PyObject* arg) {
  HANDLE_TH_ERRORS
  TORCH_CHECK(THPUtils_checkLong(arg), "invalid argument to memory_allocated");
  const int device = (int)THPUtils_unpackLong(arg);

  using torch_ipex::xpu::dpcpp::DeviceStats;
  using torch_ipex::xpu::dpcpp::Stat;
  using torch_ipex::xpu::dpcpp::StatArray;
  using torch_ipex::xpu::dpcpp::StatType;

  const auto statToDict = [](const Stat& stat) {
    py::dict dict;

    dict["current"] = stat.current;
    dict["peak"] = stat.peak;
    dict["allocated"] = stat.allocated;
    dict["freed"] = stat.freed;
    return dict;
  };

  const auto statArrayToDict = [=](const StatArray& statArray) {
    const std::array<const char*, static_cast<size_t>(StatType::NUM_TYPES)>
        statTypeNames = {"all", "small_pool", "large_pool"};
    py::dict dict;
    for (size_t i = 0; i < statTypeNames.size(); ++i) {
      dict[statTypeNames[i]] = statToDict(statArray[i]);
    }
    return dict;
  };

  const auto stats = torch_ipex::xpu::dpcpp::getDeviceStatsFromDevAlloc(device);

  py::dict result;
  result["num_alloc_retries"] = stats.num_alloc_retries;
  result["num_ooms"] = stats.num_ooms;
  result["allocation"] = statArrayToDict(stats.allocation);
  result["segment"] = statArrayToDict(stats.segment);
  result["active"] = statArrayToDict(stats.active);
  result["inactive_split"] = statArrayToDict(stats.inactive_split);
  result["allocated_bytes"] = statArrayToDict(stats.allocated_bytes);
  result["reserved_bytes"] = statArrayToDict(stats.reserved_bytes);
  result["active_bytes"] = statArrayToDict(stats.active_bytes);
  result["inactive_split_bytes"] = statArrayToDict(stats.inactive_split_bytes);

  return result.release().ptr();
  END_HANDLE_TH_ERRORS
}

PyObject* THPModule_memorySnapshot(PyObject* _unused, PyObject* noargs) {
  HANDLE_TH_ERRORS

  using torch_ipex::xpu::dpcpp::BlockInfo;
  using torch_ipex::xpu::dpcpp::SegmentInfo;

  const auto segmentInfoToDict = [](const SegmentInfo& segmentInfo) {
    py::dict segmentDict;
    segmentDict["device"] = segmentInfo.device;
    segmentDict["address"] = segmentInfo.address;
    segmentDict["total_size"] = segmentInfo.total_size;
    segmentDict["allocated_size"] = segmentInfo.allocated_size;
    segmentDict["active_size"] = segmentInfo.active_size;
    segmentDict["segment_type"] = (segmentInfo.is_large ? "large" : "small");

    py::list blocks;
    for (const auto& blockInfo : segmentInfo.blocks) {
      py::dict blockDict;
      blockDict["size"] = blockInfo.size;
      blockDict["state"] =
          (blockInfo.allocated
               ? "active_allocated"
               : (blockInfo.active ? "active_pending_free" : "inactive"));
      blocks.append(blockDict);
    }
    segmentDict["blocks"] = blocks;

    return segmentDict;
  };

  const std::vector<SegmentInfo>& snapshot =
      torch_ipex::xpu::dpcpp::snapshotOfDevAlloc();
  py::list result;

  for (const auto& segmentInfo : snapshot) {
    result.append(segmentInfoToDict(segmentInfo));
  }

  return result.release().ptr();
  END_HANDLE_TH_ERRORS
}

static PyObject* set_autocast_xpu_enabled(PyObject* _unused, PyObject* arg) {
  HANDLE_TH_ERRORS
  TORCH_CHECK_TYPE(
      PyBool_Check(arg),
      "enabled must be a bool (got ",
      Py_TYPE(arg)->tp_name,
      ")");
  at::autocast::set_xpu_enabled(arg == Py_True);
  Py_RETURN_NONE;
  END_HANDLE_TH_ERRORS
}

static PyObject* is_autocast_xpu_enabled(PyObject* _unused, PyObject* arg) {
  HANDLE_TH_ERRORS
  if (at::autocast::is_xpu_enabled()) {
    Py_RETURN_TRUE;
  } else {
    Py_RETURN_FALSE;
  }
  END_HANDLE_TH_ERRORS
}

static PyObject* set_autocast_xpu_dtype(PyObject* _unused, PyObject* arg) {
  HANDLE_TH_ERRORS
  TORCH_CHECK_TYPE(
      THPDtype_Check(arg),
      "dtype must be a torch.dtype (got ",
      Py_TYPE(arg)->tp_name,
      ")");
  at::ScalarType targetType = reinterpret_cast<THPDtype*>(arg)->scalar_type;
  at::autocast::set_autocast_xpu_dtype(targetType);
  Py_RETURN_NONE;
  END_HANDLE_TH_ERRORS
}

static PyObject* get_autocast_xpu_dtype(PyObject* _unused, PyObject* arg) {
  HANDLE_TH_ERRORS
  at::ScalarType current_dtype = at::autocast::get_autocast_xpu_dtype();
  auto dtype = (PyObject*)torch::getTHPDtype(current_dtype);
  Py_INCREF(dtype);
  return dtype;
  END_HANDLE_TH_ERRORS
}

static struct PyMethodDef _THPModule_methods[] = {
    {"_initExtension",
     (PyCFunction)THPModule_initExtension,
     METH_NOARGS,
     nullptr},
    {"_emptyCache", (PyCFunction)THPModule_emptyCache, METH_NOARGS, nullptr},
    {"_memoryStats", (PyCFunction)THPModule_memoryStats, METH_O, nullptr},
    {"_resetAccumulatedMemoryStats",
     (PyCFunction)THPModule_resetAccumulatedMemoryStats,
     METH_O,
     nullptr},
    {"_resetPeakMemoryStats",
     (PyCFunction)THPModule_resetPeakMemoryStats,
     METH_O,
     nullptr},
    {"_memorySnapshot",
     (PyCFunction)THPModule_memorySnapshot,
     METH_NOARGS,
     nullptr},
    {"set_autocast_xpu_enabled", set_autocast_xpu_enabled, METH_O, nullptr},
    {"is_autocast_xpu_enabled", is_autocast_xpu_enabled, METH_NOARGS, nullptr},
    {"set_autocast_xpu_dtype", set_autocast_xpu_dtype, METH_O, nullptr},
    {"get_autocast_xpu_dtype", get_autocast_xpu_dtype, METH_NOARGS, nullptr},
    {nullptr}};

at::Scalar scalar_slow(PyObject* object) {
  // Zero-dim tensors are converted to Scalars as-is. Note this doesn't
  // currently handle most NumPy scalar types except np.float64.
  if (THPVariable_Check(object)) {
    return ((THPVariable*)object)->cdata->item();
  }

  if (THPUtils_checkLong(object)) {
    return at::Scalar(static_cast<int64_t>(THPUtils_unpackLong(object)));
  }

  if (PyBool_Check(object)) {
    return at::Scalar(THPUtils_unpackBool(object));
  }

  if (PyComplex_Check(object)) {
    return at::Scalar(THPUtils_unpackComplexDouble(object));
  }
  return at::Scalar(THPUtils_unpackDouble(object));
}

void init_xpu_module(pybind11::module& m) {
  // For Runtime API, still use pybind
  using namespace torch_ipex::xpu::dpcpp;
  m.def("dump_memory_stat", [](const int& device_index) {
    return torch_ipex::xpu::dpcpp::dumpMemoryStatusFromDevAlloc(device_index);
  });

  m.def(
      "_is_onemkl_enabled", []() { return Settings::I().is_onemkl_enabled(); });
  m.def("_is_channels_last_1d_enabled", []() {
    return Settings::I().is_channels_last_1d_enabled();
  });

  m.def("_has_fp64_dtype", [](int device) {
    return Settings::I().has_fp64_dtype(device);
  });

  m.def("_has_2d_block_array", [](int device) {
    return Settings::I().has_2d_block_array(device);
  });

  m.def("_has_xmx", [](int device) { return Settings::I().has_xmx(device); });

  m.def(
      "_get_verbose_level", []() { return Settings::I().get_verbose_level(); });

  m.def("_set_verbose_level", [](int level) {
    return Settings::I().set_verbose_level(level);
  });

  py::enum_<LOG_LEVEL>(m, "LogLevel")
      .value("DISABLED", LOG_LEVEL::DISABLED)
      .value("TRACE", LOG_LEVEL::TRACE)
      .value("DEBUG", LOG_LEVEL::DEBUG)
      .value("INFO", LOG_LEVEL::INFO)
      .value("WARN", LOG_LEVEL::WARN)
      .value("ERR", LOG_LEVEL::ERR)
      .value("CRITICAL", LOG_LEVEL::CRITICAL)
      .export_values();

  m.def("_get_log_level", []() { return Settings::I().get_log_level(); });

  m.def("_set_log_level", [](int level) {
    return Settings::I().set_log_level(level);
  });

  m.def("_get_log_output_file_path", []() {
    return Settings::I().get_log_output_file_path();
  });

  m.def("_set_log_output_file_path", [](std::string path) {
    return Settings::I().set_log_output_file_path(path);
  });

  m.def("_get_log_rotate_file_size", []() {
    return Settings::I().get_log_rotate_file_size();
  });

  m.def("_set_log_rotate_file_size", [](int size) {
    return Settings::I().set_log_rotate_file_size(size);
  });

  m.def("_get_log_split_file_size", []() {
    return Settings::I().get_log_split_file_size();
  });

  m.def("_set_log_split_file_size", [](int size) {
    return Settings::I().set_log_split_file_size(size);
  });

  m.def("_set_log_component", [](std::string component) {
    return Settings::I().set_log_component(component);
  });

  m.def(
      "_get_log_component", []() { return Settings::I().get_log_component(); });

  py::enum_<xpu::XPU_BACKEND>(m, "XPUBackend")
      .value("GPU", xpu::XPU_BACKEND::GPU)
      .value("CPU", xpu::XPU_BACKEND::CPU)
      .value("AUTO", xpu::XPU_BACKEND::AUTO)
      .export_values();

  m.def("_get_backend", []() {
    return static_cast<int>(Settings::I().get_backend());
  });
  m.def("_set_backend", [](const int backend) {
    return Settings::I().set_backend(static_cast<xpu::XPU_BACKEND>(backend));
  });

  m.def("_is_sync_mode", []() { return Settings::I().is_sync_mode_enabled(); });

  m.def("_enable_sync_mode", []() { Settings::I().enable_sync_mode(); });

  m.def("_disable_sync_mode", []() { Settings::I().disable_sync_mode(); });

  m.def("_is_onednn_layout_enabled", []() {
    return Settings::I().is_onednn_layout_enabled();
  });

  m.def("_is_xetla_enabled", []() { return Settings::I().is_xetla_enabled(); });

  m.def(
      "_enable_onednn_layout", []() { Settings::I().enable_onednn_layout(); });

  m.def("_disable_onednn_layout", []() {
    Settings::I().disable_onednn_layout();
  });

  py::enum_<xpu::COMPUTE_ENG>(m, "XPUComputeEng")
      .value("RECOMMEND", torch_ipex::xpu::COMPUTE_ENG::RECOMMEND)
      .value("BASIC", torch_ipex::xpu::COMPUTE_ENG::BASIC)
      .value("ONEDNN", torch_ipex::xpu::COMPUTE_ENG::ONEDNN)
      .value("ONEMKL", torch_ipex::xpu::COMPUTE_ENG::ONEMKL)
      .value("XETLA", torch_ipex::xpu::COMPUTE_ENG::XETLA)
      .export_values();

  m.def("_get_compute_eng", []() {
    return static_cast<int>(Settings::I().get_compute_eng());
  });
  m.def("_set_compute_eng", [](const int eng) {
    return Settings::I().set_compute_eng(static_cast<xpu::COMPUTE_ENG>(eng));
  });

  m.def("_set_onednn_verbose", [](const int level) {
    return Settings::I().set_onednn_verbose(level);
  });

  m.def("_set_onemkl_verbose", [](const int level) {
    return Settings::I().set_onemkl_verbose(level);
  });

  py::enum_<FP32_MATH_MODE>(m, "XPUFP32MathMode")
      .value("FP32", FP32_MATH_MODE::FP32)
      .value("TF32", FP32_MATH_MODE::TF32)
      .value("BF32", FP32_MATH_MODE::BF32)
      .export_values();

  m.def("_get_fp32_math_mode", []() {
    return Settings::I().get_fp32_math_mode();
  });
  m.def("_set_fp32_math_mode", [](const FP32_MATH_MODE mode) {
    return Settings::I().set_fp32_math_mode(mode);
  });

  m.def("_enable_simple_trace", []() { Settings::I().enable_simple_trace(); });

  m.def(
      "_disable_simple_trace", []() { Settings::I().disable_simple_trace(); });

  m.def("_is_simple_trace_enabled", []() {
    return Settings::I().is_simple_trace_enabled();
  });

  m.def("_is_pti_enabled", []() { return Settings::I().is_pti_enabled(); });

  m.def("_is_ds_kernel_enabled", []() {
    return Settings::I().is_ds_kernel_enabled();
  });

  m.def("_prepare_profiler", torch_ipex::xpu::dpcpp::profiler::prepareProfiler);

  auto module = m.ptr();
  PyModule_AddFunctions(module, _THPModule_methods);
}

} // namespace torch_ipex::xpu
