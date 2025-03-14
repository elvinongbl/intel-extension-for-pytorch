#pragma once

#include <include/xpu/Settings.h>
#include <utils/Macros.h>

namespace torch_ipex::xpu {

enum ENV_VAL { OFF = 0, ON = 1, ENV_VAL_MIN = OFF, ENV_VAL_MAX = ON };
static const char* ENV_VAL_STR[]{"OFF", "ON"};

enum VERBOSE_LEVEL {
  DISABLE = 0,
  XXX = 1,
  VERBOSE_LEVEL_MIN = DISABLE,
  VERBOSE_LEVEL_MAX = XXX
};
static const char* VERBOSE_LEVEL_STR[]{"DISABLE", "DEBUG"};

enum LOG_LEVEL {
  DISABLED = -1,
  TRACE = 0,
  DEBUG = 1,
  INFO = 2,
  WARN = 3,
  ERR = 4,
  CRITICAL = 5,
  LOG_LEVEL_MAX = CRITICAL,
  LOG_LEVEL_MIN = DISABLED
};
static const char* LOG_LEVEL_STR[]{
    "DISABLED",
    "TRACE",
    "DEBUG",
    "INFO",
    "WARN",
    "ERR",
    "CRITICAL"};

enum XPU_BACKEND {
  GPU = 0,
  CPU = 1,
  AUTO = 2,
  XPU_BACKEND_MIN = GPU,
  XPU_BACKEND_MAX = AUTO
};
static const char* XPU_BACKEND_STR[]{"GPU", "CPU", "AUTO"};

enum COMPUTE_ENG {
  RECOMMEND = 0,
  BASIC = 1,
  ONEDNN = 2,
  ONEMKL = 3,
  XETLA = 4,
  COMPUTE_ENG_MIN = RECOMMEND,
  COMPUTE_ENG_MAX = XETLA
};
static const char* COMPUTE_ENG_STR[]{
    "RECOMMEND",
    "BASIC",
    "ONEDNN",
    "ONEMKL",
    "XETLA"};

namespace dpcpp {

class IPEX_API Settings final {
 public:
  Settings();

  bool has_fp64_dtype(int device_id = -1);
  bool has_2d_block_array(int device_id = -1);
  bool has_atomic64(int device_id = -1);
  bool has_xmx(int device_id = -1);

  static Settings& I(); // Singleton

  int get_verbose_level() const;
  bool set_verbose_level(int level);

  int get_log_level();
  bool set_log_level(int level);

  std::string get_log_output_file_path();
  bool set_log_output_file_path(std::string path);

  bool set_log_rotate_file_size(int size);
  int get_log_rotate_file_size();

  bool set_log_split_file_size(int size);
  int get_log_split_file_size();

  bool set_log_component(std::string component);
  std::string get_log_component();

  XPU_BACKEND get_backend() const;
  bool set_backend(XPU_BACKEND backend);

  COMPUTE_ENG get_compute_eng() const;
  bool set_compute_eng(COMPUTE_ENG eng);

  bool is_sync_mode_enabled() const;
  void enable_sync_mode();
  void disable_sync_mode();

  bool is_onednn_layout_enabled() const;
  void enable_onednn_layout();
  void disable_onednn_layout();

  FP32_MATH_MODE get_fp32_math_mode() const;
  bool set_fp32_math_mode(FP32_MATH_MODE mode);

  bool set_onednn_verbose(int level);
  bool set_onemkl_verbose(int level);

  bool is_onemkl_enabled() const;

  bool is_channels_last_1d_enabled() const;
  bool is_xetla_enabled() const;

  bool is_simple_trace_enabled() const;
  void enable_simple_trace();
  void disable_simple_trace();

  bool is_pti_enabled() const;
  bool is_ds_kernel_enabled() const;

 private:
  VERBOSE_LEVEL verbose_level;
  LOG_LEVEL log_level;
  std::string log_component;
  std::string log_output;
  int log_rotate_size;
  int log_split_size;
  XPU_BACKEND xpu_backend;
  COMPUTE_ENG compute_eng;
  FP32_MATH_MODE fp32_math_mode;

  ENV_VAL sync_mode_enabled;
  ENV_VAL onednn_layout_enabled;

#ifdef BUILD_SIMPLE_TRACE
  ENV_VAL simple_trace_enabled;
#endif
};

} // namespace dpcpp
} // namespace torch_ipex::xpu
