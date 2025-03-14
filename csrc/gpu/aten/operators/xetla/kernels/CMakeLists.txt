set(TARGET xetla_kernels)

set(XETLA_AVAILABLE_ARCHS xe_hpc xe_hpg xe_lpg)
string(REPLACE "," ";" USE_XETLA ${USE_XETLA})
# set true value to all available archs ; set false value to empty list
if("${USE_XETLA}")  # A quoted string always evaluates to false unless: The string's value is one of the true constants
  # Note: CMake < 3.20 will evaluate `("${USE_XETLA}")` to True if USE_XETLA is mtl-p, and then be replaced by the whole
  # XETLA_AVAILABLE_ARCHS. Therefore cmake >= 3.20 should be used to partially enable xetla kernels for development. 
  set(USE_XETLA ${XETLA_AVAILABLE_ARCHS})
elseif(NOT USE_XETLA) # if(<variable>): True if given a variable that is defined to a value that is not a false constant
  set(USE_XETLA "")
endif()

# link openblas
set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS}
  -fsycl
  -fsycl-device-code-split=per_kernel
  -fsycl-max-parallel-link-jobs=${SYCL_MAX_PARALLEL_LINK_JOBS}
)

if (USE_AOT_DEVLIST)
  set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS} -fsycl-targets=spir64_gen)
endif()

# set RPATH
if(NOT WINDOWS)
  foreach(RPATH ${RPATHS_LIST})
    set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS} ${RPATH})
  endforeach()
  set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS} "-Wl,--disable-new-dtags")
endif()

set(XETLA_OFFLINE_OPTIONS "-doubleGRF")
set(XETLA_OFFLINE_OPTIONS "${XETLA_OFFLINE_OPTIONS} -vc-disable-indvars-opt")
set(XETLA_OFFLINE_OPTIONS "${XETLA_OFFLINE_OPTIONS} -vc-codegen")
# For registers usage verbose at AOT
set(XETLA_OFFLINE_OPTIONS "${XETLA_OFFLINE_OPTIONS} -Xfinalizer -printregusage")
# Enable bank conflict reduction.
set(XETLA_OFFLINE_OPTIONS "${XETLA_OFFLINE_OPTIONS} -Xfinalizer -enableBCR")
# Optimization to reduce the tokens used for DPAS instruction.
set(XETLA_OFFLINE_OPTIONS "${XETLA_OFFLINE_OPTIONS} -Xfinalizer -DPASTokenReduction")

set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS} -Xs )

foreach(used_arch IN LISTS USE_XETLA)
  foreach(available_arch IN LISTS XETLA_AVAILABLE_ARCHS)
    if (used_arch STREQUAL available_arch)
      string(TOUPPER "${used_arch}" arch_upper)
      set(USE_XETLA_${arch_upper} ON)
      message(STATUS "XeTLA: Found arch from list: ${arch_upper}")
      break()
    endif()
  endforeach()
  if (NOT USE_XETLA_${arch_upper})
    message(FATAL_ERROR "Unexpected XeTLA architecture: ${used_arch}")
  endif()
endforeach()

if (USE_AOT_DEVLIST)
  # Disable implementations for architectures not in USE_AOT_DEVLIST
  function (disable_architecture_by_aot ARCH REGULAR_EXPRESSION)
    string(REGEX MATCHALL ${REGULAR_EXPRESSION} USE_AOT_DEVLIST_${ARCH} "${USE_AOT_DEVLIST}")
    string(REPLACE ";" "," USE_AOT_DEVLIST_${ARCH} "${USE_AOT_DEVLIST_${ARCH}}")
    set(USE_AOT_DEVLIST_${ARCH} "${USE_AOT_DEVLIST_${ARCH}}" PARENT_SCOPE)
    message(STATUS "XeTLA: USE_AOT_DEVLIST_${ARCH}: ${USE_AOT_DEVLIST_${ARCH}}")
    if("${USE_AOT_DEVLIST_${ARCH}}" STRLESS_EQUAL "")
      set(USE_XETLA_${ARCH} OFF PARENT_SCOPE)
    endif()
  endfunction()
  disable_architecture_by_aot(XE_HPC "(pvc|xe-hpc)")
  disable_architecture_by_aot(XE_HPG "(ats-m150|acm-g10|acm-g11|acm-g12|xe-hpg)")
  disable_architecture_by_aot(XE_LPG "(mtl-m|mtl-s|mtl-p|mtl-h|mtl-u|xe-lpg|0x7d55|0x7dd5|0x7d57|0x7dd7)")

  set(XETLA_USE_AOT_DEVLIST "${USE_AOT_DEVLIST}")
  if (USE_XETLA_XE_HPC)  # Temporary fix as AOT fails of try to compile XE_HPC kernels for ats-m150 etc
    message(STATUS "XeTLA: XE_HPC suppress other aot targets")
    set(XETLA_USE_AOT_DEVLIST "${USE_AOT_DEVLIST_XE_HPC}")
  elseif(USE_XETLA_XE_HPG) # Temporary fix as AOT fails of try to compile XE_HPG kernels for mtl-p etc
    message(STATUS "XeTLA: XE_HPG suppress other aot targets")
    set(XETLA_USE_AOT_DEVLIST "${USE_AOT_DEVLIST_XE_HPG}")
  endif()
  message(STATUS "XeTLA: XETLA_USE_AOT_DEVLIST: ${XETLA_USE_AOT_DEVLIST}")
  set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS} "-device ${XETLA_USE_AOT_DEVLIST} -options '${XETLA_OFFLINE_OPTIONS}'")
else()
  set(XETLA_KERNEL_FLAGS ${XETLA_KERNEL_FLAGS} "${XETLA_OFFLINE_OPTIONS}")
endif()

cmake_minimum_required(VERSION 3.16)
set(CMAKE_CXX_STANDARD 20)
SET(BLA_VENDOR INTEL10_64LP)

set(XETLA_INCLUDE_DIR "${USE_XETLA_SRC}/include")
find_file(XETLA_HEADER NAMES xetla.hpp PATHS ${XETLA_INCLUDE_DIR} PATH_SUFFIXES NO_DEFAULT_PATH)
if(NOT XETLA_HEADER)
  message(FATAL_ERROR "XeTLA header ${XETLA_HEADER} not found in ${XETLA_INCLUDE_DIR}")
endif()

set(XETLA_USED_ARCHS)
foreach(available_arch IN LISTS XETLA_AVAILABLE_ARCHS)
  string(TOUPPER ${available_arch} arch_upper)
  if(USE_XETLA_${arch_upper})
    list(APPEND XETLA_USED_ARCHS ${available_arch})
  endif()
endforeach()

# Generate cpp source files contains template instantiations for kernels with
# different configs so that they can be compiled as parallel jobs.
include(SDP/fmha_forward_configure.cmake)
fmha_forward_configure("${XETLA_USED_ARCHS}")
include(GEMM/GEMM_int4_configure.cmake)
GEMM_int4_configure("${XETLA_USED_ARCHS}")

# Xetla kernel apply different offline compiler options than framework.
# If framework link XeTLA as static lib, link options would conflict now.
# Temporarily adopting shared lib, will fix it soon.
FILE(GLOB_RECURSE kernels_src *.cpp)
add_library(${TARGET} SHARED ${kernels_src} ${FMHA_FORWARD_KERNEL_SRCS} ${GEMM_INT4_SRCS})

target_include_directories(${TARGET} PRIVATE .)
target_include_directories(${TARGET} PUBLIC ${XETLA_INCLUDE_DIR})
target_include_directories(${TARGET} PUBLIC ${TORCH_INCLUDE_DIRS})
target_link_libraries(${TARGET} PUBLIC ${GPU_TORCH_LIBS})

# Set visibility to hidden to close the differences of Windows & Linux
set_target_properties(${TARGET} PROPERTIES CXX_VISIBILITY_PRESET hidden)
target_compile_definitions(${TARGET} PRIVATE BUILD_XETLA_KERNEL_LIB)

# Feature flag macros should be public for uses in its dependent libraries
target_compile_definitions(${TARGET} PUBLIC USE_XETLA)
foreach(available_arch IN LISTS XETLA_AVAILABLE_ARCHS)
  string(TOUPPER ${available_arch} ARCH)
  if(USE_XETLA_${ARCH})
    message(STATUS "XeTLA: USE_XETLA_${ARCH} enabled.")
    target_compile_definitions(${TARGET} PUBLIC "USE_XETLA_${ARCH}")
  else()
    message(STATUS "XeTLA: USE_XETLA_${ARCH} disabled.")
  endif()
endforeach()

target_link_options(${TARGET} PRIVATE ${XETLA_KERNEL_FLAGS})
target_compile_options(${TARGET} PRIVATE -fsycl)
if (USE_AOT_DEVLIST)
  target_compile_options(${TARGET} PRIVATE -fsycl-targets=spir64_gen)
endif()
