import torch
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch  # noqa
import pytest


class TestTorchMethod(TestCase):
    def test_dequantize_per_tensor(self, dtype=torch.float):
        # oneDNN asymm quant has undefined behaviour on Windows currently.
        zp_vec = [0, 2]
        for dtype in [torch.qint8, torch.quint8]:
            for zp in zp_vec:
                src = torch.randn(1, 3, 2, 2)
                src_gpu = src.to("xpu")
                tensor_scale = 0.3

                dst_q = torch.quantize_per_tensor(
                    src, scale=tensor_scale, zero_point=zp, dtype=dtype
                )
                dst = torch.dequantize(dst_q)

                dst_gpu_q = torch.quantize_per_tensor(
                    src_gpu, scale=tensor_scale, zero_point=zp, dtype=dtype
                )
                dst_gpu = torch.dequantize(dst_gpu_q)

                self.assertEqual(dst, dst_gpu)

    @pytest.mark.skipif(
        not torch.xpu.has_fp64_dtype(), reason="fp64 not support by this device"
    )
    def test_dequantize_per_channel(self, dtype=torch.float):
        # oneDNN asymm quant has undefined behaviour on Windows currently.
        zp_vec = [0, 2]
        print("zp vec:", zp_vec)
        for dtype in [torch.qint8, torch.quint8]:
            for zp in zp_vec:
                src = torch.randn(1, 3, 2, 2)
                src_gpu = src.to("xpu")

                channel_scale = torch.tensor([0.1, 0.3, 0.5])
                channel_zero_point = torch.tensor([0, 0, 0])
                channel_scale_xpu = torch.tensor([0.1, 0.3, 0.5], device="xpu")
                channel_zero_point_xpu = torch.tensor([0, 0, 0], device="xpu")

                dst_q = torch.quantize_per_channel(
                    src,
                    scales=channel_scale,
                    zero_points=channel_zero_point,
                    dtype=dtype,
                    axis=1,
                )
                dst = torch.dequantize(dst_q)

                dst_gpu_q = torch.quantize_per_channel(
                    src_gpu,
                    scales=channel_scale_xpu,
                    zero_points=channel_zero_point_xpu,
                    dtype=dtype,
                    axis=1,
                )
                dst_gpu = torch.dequantize(dst_gpu_q)

                self.assertEqual(dst, dst_gpu)

    def test_dequantize_FP32_input(self, dtype=torch.float):
        src = torch.randn(1, 3, 2, 2)
        src_gpu = src.to("xpu")

        dst = src
        dst_gpu = torch.dequantize(src_gpu)

        self.assertEqual(dst, dst_gpu)
