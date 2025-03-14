/*******************************************************************************
 * Copyright (c) 2022-2023 Intel Corporation
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *******************************************************************************/

/// @file
/// C++ API

#pragma once

#include <experimental/kernel/reduction/common.hpp>

namespace gpu::xetla::kernel {

/// @brief Sets up attribute of the row reduction.
///
/// @tparam wg_tile_n_ Is the num of cols processed by one workgroup.
/// @tparam wg_tile_m_ Is the num of rows processed by one workgroup in each
/// inner loop.
/// @tparam sg_tile_n_ Is the num of cols processed by one subgroup.
/// @tparam sg_tile_m_ Is the num of rows processed by one subgroup in each
/// inner loop.
/// @tparam is_dynamic_job_
template <
    uint32_t wg_tile_n_,
    uint32_t wg_tile_m_,
    uint32_t sg_tile_n_,
    uint32_t sg_tile_m_,
    bool is_dynamic_job_ = true>
struct row_reduction_attr_t {
  static constexpr uint32_t wg_tile_m = wg_tile_m_;
  static constexpr uint32_t wg_tile_n = wg_tile_n_;
  static constexpr uint32_t sg_tile_m = sg_tile_m_;
  static constexpr uint32_t sg_tile_n = sg_tile_n_;
  static constexpr bool is_dynamic_job = is_dynamic_job_;
};

} // namespace gpu::xetla::kernel
