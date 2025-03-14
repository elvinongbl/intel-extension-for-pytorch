import torch
import torch.nn as nn
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch  # noqa

cpu_device = torch.device("cpu")
dpcpp_device = torch.device("xpu")


class TestNNMethod(TestCase):
    def test_avg_pool2d(self, dtype=torch.float):
        x_cpu = torch.ones([2, 2, 3, 3], device=cpu_device)
        grad_cpu = torch.ones([2, 2, 3, 3], device=cpu_device)
        x_dpcpp = x_cpu.to(dpcpp_device)
        grad_dpcpp = grad_cpu.to(dpcpp_device)
        self.assertEqual(x_cpu, x_dpcpp.to(cpu_device))
        self.assertEqual(grad_cpu, grad_dpcpp.to(cpu_device))

        avg_pool = nn.AvgPool2d(kernel_size=3, stride=1, padding=1)

        x_cpu.requires_grad_(True)
        # y_cpu = conv1(x_cpu)
        y_cpu = avg_pool(x_cpu)
        print("y_cpu", y_cpu)
        # conv1.zero_grad()
        output_cpu = y_cpu.backward(grad_cpu)
        print("x_cpu.grad", x_cpu.grad)

        # conv1.to("xpu")
        avg_pool.to(dpcpp_device)
        x_dpcpp.requires_grad_(True)
        # y_dpcpp = conv1(x_dpcpp)
        y_dpcpp = avg_pool(x_dpcpp)
        print("y_dpcpp", y_dpcpp.to("cpu"))
        # conv1.zero_grad()
        output_dpcpp = y_dpcpp.backward(grad_dpcpp)
        print("x_dpcpp.grad", x_dpcpp.grad.to("cpu"))
        self.assertEqual(x_cpu.grad, x_dpcpp.grad.to(cpu_device))

    def test_channels_last_simple_fwd(self, dtype=torch.float):
        x = torch.randn(1, 2, 3, 3, dtype=torch.float)
        w = torch.randn(2, 2, 3, 3, dtype=torch.float)
        conv = torch.nn.Conv2d(2, 2, kernel_size=3, stride=1, padding=1, bias=False)
        avg_pool = torch.nn.AvgPool2d(kernel_size=3, stride=1, padding=1)
        relu = torch.nn.ReLU()
        conv.weight.data = w
        ref = conv(x)
        ref = relu(ref)
        ref = avg_pool(ref)

        x = x.to("xpu").to(memory_format=torch.channels_last)
        w = w.to("xpu")
        conv.weight.data = w
        real = conv(x)
        real = relu(real)
        real = avg_pool(real)
        real = real.contiguous().cpu()

        print(real)
        print(ref)

        self.assertEqual(real, ref)

    def test_channels_last_simple_bwd(self, dtype=torch.float):
        x_cpu = torch.ones([2, 2, 3, 3], device=cpu_device)
        grad_cpu = torch.ones([2, 2, 3, 3], device=cpu_device)
        x_dpcpp = x_cpu.to(dpcpp_device).to(memory_format=torch.channels_last)
        grad_dpcpp = grad_cpu.to(dpcpp_device)

        avg_pool = nn.AvgPool2d(kernel_size=3, stride=1, padding=1)

        x_cpu.requires_grad_(True)
        y_cpu = avg_pool(x_cpu)
        print("y_cpu", y_cpu)
        output_cpu = y_cpu.backward(grad_cpu)
        print("x_cpu.grad", x_cpu.grad)

        avg_pool.to(dpcpp_device)
        x_dpcpp.requires_grad_(True)
        y_dpcpp = avg_pool(x_dpcpp)
        print("y_dpcpp", y_dpcpp.to("cpu"))
        output_dpcpp = y_dpcpp.backward(grad_dpcpp)
        print("x_dpcpp.grad", x_dpcpp.grad.to("cpu"))
        self.assertEqual(x_cpu.grad, x_dpcpp.grad.to(cpu_device))

    def test_avg_pool_3D(self, dtype=torch.float):
        x = torch.randn([30, 40, 50])
        grad = torch.randn([30, 40, 50])
        m = nn.AvgPool2d(kernel_size=3, stride=1, padding=1)

        # 3D contiguous input
        # CPU
        input_cpu = x.clone()
        input_cpu.requires_grad_(True)
        grad_cpu = grad.clone()
        output_cpu = m(input_cpu)
        output_cpu.backward(grad_cpu)

        # XPU
        input_xpu = x.clone().to(dpcpp_device)
        input_xpu.requires_grad_(True)
        grad_xpu = grad.clone().to(dpcpp_device)
        output_xpu = m(input_xpu)
        output_xpu.backward(grad_xpu)

        self.assertEqual(output_cpu, output_xpu.to(cpu_device))
        self.assertEqual(input_cpu.grad, input_xpu.grad.to(cpu_device))

        # 3D non-contiguous input
        # CPU
        input_cpu = x.clone().transpose(0, 1)
        input_cpu.requires_grad_(True)
        grad_cpu = grad.clone().transpose(0, 1)
        output_cpu = m(input_cpu)
        output_cpu.backward(grad_cpu)

        # XPU
        input_xpu = x.clone().transpose(0, 1).to(dpcpp_device)
        input_xpu.requires_grad_(True)
        grad_xpu = grad.clone().transpose(0, 1).to(dpcpp_device)
        output_xpu = m(input_xpu)
        output_xpu.backward(grad_xpu)

        self.assertEqual(output_cpu, output_xpu.to(cpu_device))
        self.assertEqual(input_cpu.grad, input_xpu.grad.to(cpu_device))

    def test_avg_pool_4D(self, dtype=torch.float):
        x = torch.randn([20, 30, 40, 50])
        grad = torch.randn([20, 30, 40, 50])
        m = nn.AvgPool2d(kernel_size=3, stride=1, padding=1)

        # 4D contiguous input
        # CPU
        input_cpu = x.clone()
        input_cpu.requires_grad_(True)
        grad_cpu = grad.clone()
        output_cpu = m(input_cpu)
        output_cpu.backward(grad_cpu)

        # XPU
        input_xpu = x.clone().to(dpcpp_device)
        input_xpu.requires_grad_(True)
        grad_xpu = grad.clone().to(dpcpp_device)
        output_xpu = m(input_xpu)
        output_xpu.backward(grad_xpu)

        self.assertEqual(output_cpu, output_xpu.to(cpu_device))
        self.assertEqual(input_cpu.grad, input_xpu.grad.to(cpu_device))

        # 4D channel_last input
        # CPU
        mem_format = torch.channels_last
        input_cpu = x.clone().contiguous(memory_format=mem_format)
        input_cpu.requires_grad_(True)
        grad_cpu = grad.clone().contiguous(memory_format=mem_format)
        output_cpu = m(input_cpu)
        output_cpu.backward(grad_cpu)

        # XPU
        input_xpu = x.clone().contiguous(memory_format=mem_format).to(dpcpp_device)
        input_xpu.requires_grad_(True)
        grad_xpu = grad.clone().contiguous(memory_format=mem_format).to(dpcpp_device)
        output_xpu = m(input_xpu)
        output_xpu.backward(grad_xpu)

        self.assertEqual(output_cpu, output_xpu.to(cpu_device))
        self.assertEqual(input_cpu.grad, input_xpu.grad.to(cpu_device))

        # 4D non-contiguous input
        # CPU
        input_cpu = x.clone().transpose(2, 3)
        input_cpu.requires_grad_(True)
        grad_cpu = grad.clone().transpose(2, 3)
        output_cpu = m(input_cpu)
        output_cpu.backward(grad_cpu)

        # XPU
        input_xpu = x.clone().transpose(2, 3).to(dpcpp_device)
        input_xpu.requires_grad_(True)
        grad_xpu = grad.clone().transpose(2, 3).to(dpcpp_device)
        output_xpu = m(input_xpu)
        output_xpu.backward(grad_xpu)

        self.assertEqual(output_cpu, output_xpu.to(cpu_device))
        self.assertEqual(input_cpu.grad, input_xpu.grad.to(cpu_device))

    def test_avg_pool_blk_format(self, dtype=torch.float):
        x = torch.randn([10, 16, 30, 40])
        grad = torch.randn([10, 16, 30, 40])
        conv_cpu1 = nn.Conv2d(16, 16, kernel_size=3, stride=1, padding=1, bias=False)
        pool_cpu = nn.AvgPool2d(kernel_size=3, stride=1, padding=1)
        conv_cpu2 = nn.Conv2d(16, 16, kernel_size=1, stride=1, bias=False)

        # 5D contiguous input
        # CPU
        input_cpu = x.clone()
        input_cpu.requires_grad_(True)
        grad_cpu = grad.clone()
        output_cpu = conv_cpu2(pool_cpu(conv_cpu1(input_cpu)))
        output_cpu.backward(grad_cpu)

        conv_cpu1.zero_grad()
        conv_cpu2.zero_grad()
        # XPU
        with torch.xpu.onednn_layout():
            input_xpu = x.clone().to(dpcpp_device)
            input_xpu.requires_grad_(True)
            grad_xpu = grad.clone().to(dpcpp_device)
            conv_dpcpp1 = conv_cpu1.to(dpcpp_device)
            pool_dpcpp = pool_cpu.to(dpcpp_device)
            conv_dpcpp2 = conv_cpu2.to(dpcpp_device)
            output_xpu = conv_dpcpp2(pool_dpcpp(conv_dpcpp1(input_xpu)))
            output_xpu.backward(grad_xpu)

        self.assertEqual(output_cpu, output_xpu.to(cpu_device))
        self.assertEqual(input_cpu.grad, input_xpu.grad.to(cpu_device))
