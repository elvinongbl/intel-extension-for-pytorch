set(profiler_src)
if(NOT USE_PTI)
  list(APPEND profiler_src "${IPEX_GPU_PROFILER_SRC_DIR}/profiler_kineto.cpp")
  list(APPEND profiler_src "${IPEX_GPU_PROFILER_SRC_DIR}/kineto_shim.cpp")
else()
  FILE(GLOB profiler_src "*.cpp")
endif()

set(profiler_inc)
if (USE_PTI)
  list(APPEND profiler_inc "${IPEX_GPU_PROFILER_SRC_DIR}/include")
endif()

set(IPEX_PROFILER_SRCS ${profiler_src} PARENT_SCOPE)
set(IPEX_PROFILER_INCLUDES ${profiler_inc} PARENT_SCOPE)
