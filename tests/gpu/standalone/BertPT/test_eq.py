import torch
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch  # noqa

cpu_device = torch.device("cpu")
dpcpp_device = torch.device("xpu")

class TestTorchMethod(TestCase):
    def test_eq_1(self, dtype=torch.float):
        x1 = torch.randn(1, dtype=dtype).to("xpu")
        x2 = x1.to("xpu")
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))

    def test_eq_bfloat16_1(self, dtype=torch.bfloat16):
        x1 = torch.randn(1, dtype=dtype).to("xpu")
        x2 = x1.to("xpu")
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))

    def test_eq_float16_1(self, dtype=torch.float16):
        x1 = torch.randn(1, dtype=dtype).to("xpu")
        x2 = x1.to("xpu")
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))

    def test_eq_2(self, dtype=torch.float):
        x1 = torch.randn(20, dtype=dtype).to("xpu")
        x2 = x1.to("xpu")
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))

    def test_eq_bfloat16_2(self, dtype=torch.bfloat16):
        x1 = torch.randn(20, dtype=dtype).to("xpu")
        x2 = x1.to("xpu")
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))

    def test_eq_float16_2(self, dtype=torch.float16):
        x1 = torch.randn(20, dtype=dtype).to("xpu")
        x2 = x1.to("xpu")
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))
