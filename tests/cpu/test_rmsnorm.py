import torch
import torch.nn as nn
from common_utils import TestCase
import unittest


class RMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6, dtype=torch.float):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size, dtype=dtype))
        self.variance_epsilon = eps

    def forward(self, hidden_states, fused_rmsnorm=False):
        if fused_rmsnorm:
            return torch.ops.torch_ipex.rmsnorm(
                hidden_states, self.weight, self.variance_epsilon
            )
        else:
            input_dtype = hidden_states.dtype
            hidden_states = hidden_states.to(torch.float32)
            variance = hidden_states.pow(2).mean(-1, keepdim=True)
            hidden_states = hidden_states * torch.rsqrt(
                variance + self.variance_epsilon
            )
            res = (self.weight * hidden_states).to(input_dtype)
            return res


class RMSNormTester(TestCase):
    def test_RMSNorm(self):
        for dim in [2, 3, 4, 5]:
            # RMSNorm input is fp32
            for weight_dtype in [torch.float32, torch.half, torch.bfloat16]:
                with torch.no_grad():
                    input_size = [
                        3,
                    ]
                    for _ in range(dim - 1):
                        input_size.append(10)
                    x = torch.randn(input_size)
                    model = RMSNorm(input_size, dtype=weight_dtype).eval()
                    y1_fp32 = model(x)
                    fused_y1_fp32 = model(x, fused_rmsnorm=True)
                    self.assertEqual(y1_fp32, fused_y1_fp32)
            # RMSNorm input is bf16
            for weight_dtype in [torch.float32, torch.half, torch.bfloat16]:
                with torch.no_grad():
                    input_size = [
                        3,
                    ]
                    for _ in range(dim - 1):
                        input_size.append(10)
                    x = torch.randn(input_size)

                    model = RMSNorm(input_size, dtype=weight_dtype).eval()
                    x_bf16 = x.to(torch.bfloat16)
                    y1_bf16 = model(x_bf16)
                    fused_y1_bf16 = model(x_bf16, fused_rmsnorm=True)
                    self.assertEqual(y1_bf16, fused_y1_bf16, prec=1e-2)
            # RMSNorm input is fp16
            for weight_dtype in [torch.float32, torch.half, torch.bfloat16]:
                with torch.no_grad():
                    input_size = [
                        3,
                    ]
                    for _ in range(dim - 1):
                        input_size.append(10)
                    x = torch.randn(input_size)
                    model = RMSNorm(input_size, dtype=weight_dtype).eval()
                    x_fp16 = x.to(torch.half)
                    y1_fp16 = model(x_fp16)
                    fused_y1_fp16 = model(x_fp16, fused_rmsnorm=True)
                    self.assertEqual(y1_fp16, fused_y1_fp16, prec=1e-2)


if __name__ == "__main__":
    test = unittest.main()
