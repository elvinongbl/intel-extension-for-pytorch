# - Try to find oneDNN
#
# The following are set after configuration is done:
#  ONEDNN_FOUND          : set to true if oneDNN is found.
#  ONEDNN_INCLUDE_DIR    : path to oneDNN include dir.
#  ONEDNN_LIBRARY        : list of libraries for oneDNN
#
# The following variables are used:
#  ONEDNN_USE_NATIVE_ARCH : Whether native CPU instructions should be used in ONEDNN. This should be turned off for
#  general packaging to avoid incompatible CPU instructions. Default: OFF.

IF (NOT ONEDNN_FOUND)
SET(ONEDNN_FOUND OFF)

SET(ONEDNN_LIBRARY)
SET(ONEDNN_LIB_SHARED)
SET(ONEDNN_LIB_STATIC)
SET(ONEDNN_INCLUDE_DIR)
SET(DNNL_INCLUDES)

IF(NOT "${USE_ONEDNN_DIR}" STREQUAL "")
  SET(ONEDNN_ROOT "${USE_ONEDNN_DIR}")
  # To find includes
  FIND_PATH(ONEDNN_INCLUDE_DIR dnnl.hpp dnnl.h PATHS ${ONEDNN_ROOT} PATH_SUFFIXES include)
  FIND_PATH(DNNL_INCLUDES dnnl.hpp dnnl.h PATHS ${ONEDNN_ROOT} PATH_SUFFIXES include/oneapi/dnnl)
  IF(NOT ONEDNN_INCLUDE_DIR)
    MESSAGE(FATAL_ERROR "oneDNN headers dnnl.hpp dnnl.h not found in given direcorty ${ONEDNN_ROOT}/include")
  ELSEIF(NOT DNNL_INCLUDES)
    MESSAGE(FATAL_ERROR "dnnl headers dnnl.hpp dnnl.h not found in given directory ${ONEDNN_ROOT}/include/oneapi/dnnl")
  ENDIF()
  LIST(APPEND ONEDNN_INCLUDE_DIR ${DNNL_INCLUDES})
  # To find libraries
  FIND_LIBRARY(ONEDNN_EXTERNAL_LIB
    NAMES dnnl
    PATHS ${ONEDNN_ROOT}
    PATH_SUFFIXES lib
  )
  IF(NOT ONEDNN_EXTERNAL_LIB)
    MESSAGE(FATAL_ERROR "oneDNN library neither libdnnl.so nor libdnnl.a could be found in given directory ${ONEDNN_ROOT}/lib")
  ENDIF()
  SET(ONEDNN_LIBRARY_FILES ${ONEDNN_EXTERNAL_LIB})
  SET(CURRENT_LIBRARY ${ONEDNN_EXTERNAL_LIB})
  WHILE(IS_SYMLINK ${CURRENT_LIBRARY})
    GET_FILENAME_COMPONENT(SYMLINK_TARGET_DIR "${CURRENT_LIBRARY}" DIRECTORY)
    EXECUTE_PROCESS(COMMAND readlink -z ${CURRENT_LIBRARY}
                    OUTPUT_VARIABLE SYMLINK_TARGET
                    OUTPUT_STRIP_TRAILING_WHITESPACE)
    SET(CURRENT_LIBRARY "${SYMLINK_TARGET_DIR}/${SYMLINK_TARGET}")
    IF(NOT SYMLINK_TARGET STREQUAL "")
      LIST(APPEND ONEDNN_LIBRARY_FILES "${CURRENT_LIBRARY}")
    ENDIF()
  ENDWHILE()
ELSE()
  SET(THIRD_PARTY_DIR "${PROJECT_SOURCE_DIR}/third_party")
  SET(ONEDNN_DIR "oneDNN")
  SET(ONEDNN_ROOT "${THIRD_PARTY_DIR}/${ONEDNN_DIR}")

  FIND_PATH(ONEDNN_INCLUDE_DIR dnnl.hpp dnnl.h PATHS ${ONEDNN_ROOT} PATH_SUFFIXES include)
  IF(NOT ONEDNN_INCLUDE_DIR)
    FIND_PACKAGE(Git)
    IF(NOT Git_FOUND)
      MESSAGE(FATAL_ERROR "Can not find Git executable!")
    ENDIF()
    EXECUTE_PROCESS(
      COMMAND ${GIT_EXECUTABLE} submodule update --init ${ONEDNN_DIR}
      WORKING_DIRECTORY ${THIRD_PARTY_DIR} COMMAND_ERROR_IS_FATAL ANY)
    FIND_PATH(ONEDNN_INCLUDE_DIR dnnl.hpp dnnl.h PATHS ${ONEDNN_ROOT} PATH_SUFFIXES include)
  ENDIF(NOT ONEDNN_INCLUDE_DIR)

  IF(NOT ONEDNN_INCLUDE_DIR)
    MESSAGE(FATAL_ERROR "oneDNN source files not found!")
  ENDIF(NOT ONEDNN_INCLUDE_DIR)

  SET(DNNL_ENABLE_PRIMITIVE_CACHE TRUE CACHE BOOL "oneDNN sycl primitive cache" FORCE)

  IF(ONEDNN_USE_NATIVE_ARCH)  # Disable HostOpts in oneDNN unless ONEDNN_USE_NATIVE_ARCH is set.
    SET(DNNL_ARCH_OPT_FLAGS "HostOpts" CACHE STRING "" FORCE)
  ELSE()
    IF(CMAKE_CXX_COMPILER_ID STREQUAL "GNU" OR CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
      SET(DNNL_ARCH_OPT_FLAGS "-msse4" CACHE STRING "" FORCE)
    ELSE()
      SET(DNNL_ARCH_OPT_FLAGS "" CACHE STRING "" FORCE)
    ENDIF()
  ENDIF()

  IF(BUILD_SEPARATE_OPS)
    SET(DNNL_LIBRARY_TYPE SHARED CACHE STRING "" FORCE)
  ELSE()
    SET(DNNL_LIBRARY_TYPE STATIC CACHE STRING "" FORCE)
  ENDIF()

  # IF (USE_ONEMKL)
  #   SET(DNNL_BLAS_VENDOR "MKL" CACHE STRING "" FORCE)
  # ELSE()
  #   SET(DNNL_BLAS_VENDOR "NONE" CACHE STRING "" FORCE)
  # ENDIF()

  # FIXME: Set threading to THREADPOOL to bypass issues due to not found TBB or OMP.
  # NOTE: We will not use TBB, but we cannot enable OMP. We build whole oneDNN by DPC++
  # compiler which only provides the Intel iomp. But oneDNN cmake scripts only try to
  # find the iomp in ICC compiler, which caused a build fatal error here.
  SET(DNNL_CPU_RUNTIME "THREADPOOL" CACHE STRING "oneDNN cpu backend" FORCE)
  SET(DNNL_GPU_RUNTIME "SYCL" CACHE STRING "oneDNN gpu backend" FORCE)
  SET(DNNL_BUILD_TESTS FALSE CACHE BOOL "build with oneDNN tests" FORCE)
  SET(DNNL_BUILD_EXAMPLES FALSE CACHE BOOL "build with oneDNN examples" FORCE)
  SET(DNNL_ENABLE_CONCURRENT_EXEC TRUE CACHE BOOL "multi-thread primitive execution" FORCE)
  SET(DNNL_EXPERIMENTAL TRUE CACHE BOOL "use one pass for oneDNN BatchNorm" FORCE)

  ADD_SUBDIRECTORY(${ONEDNN_ROOT} oneDNN EXCLUDE_FROM_ALL)
  SET(ONEDNN_LIBRARY ${DNNL_LIBRARY_NAME})
  IF(NOT TARGET ${ONEDNN_LIBRARY})
    MESSAGE(FATAL_ERROR "Failed to include oneDNN target")
  ENDIF(NOT TARGET ${ONEDNN_LIBRARY})

  IF(NOT APPLE AND CMAKE_COMPILER_IS_GNUCC)
    TARGET_COMPILE_OPTIONS(${ONEDNN_LIBRARY} PRIVATE -Wno-uninitialized)
    TARGET_COMPILE_OPTIONS(${ONEDNN_LIBRARY} PRIVATE -Wno-strict-overflow)
    TARGET_COMPILE_OPTIONS(${ONEDNN_LIBRARY} PRIVATE -Wno-error=strict-overflow)
  ENDIF(NOT APPLE AND CMAKE_COMPILER_IS_GNUCC)

  TARGET_COMPILE_OPTIONS(${ONEDNN_LIBRARY} PRIVATE -Wno-tautological-compare)
  GET_TARGET_PROPERTY(DNNL_INCLUDES ${ONEDNN_LIBRARY} INCLUDE_DIRECTORIES)
  IF (NOT WINDOWS)
    # Help oneDNN to search ze_loader by dlopen
    TARGET_LINK_LIBRARIES(${ONEDNN_LIBRARY} PRIVATE ze_loader)
  ENDIF()
  LIST(APPEND ONEDNN_INCLUDE_DIR ${DNNL_INCLUDES})
ENDIF()

SET(ONEDNN_FOUND ON)
MESSAGE(STATUS "Found oneDNN: TRUE")

ENDIF(NOT ONEDNN_FOUND)
