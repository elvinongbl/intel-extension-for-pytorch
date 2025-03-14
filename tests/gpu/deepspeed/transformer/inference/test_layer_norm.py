import torch
import pytest
from inference_test_utils import allclose, get_dtypes, assert_almost_equal

import intel_extension_for_pytorch as ipex

inference_module = ipex.xpu.deepspeed
ipex_device = 'xpu:0'

def ref_implementation(vals, gamma, beta, epsilon, channels, dtype):
    vals_f = vals.to(torch.float32)
    gamma_f = gamma.to(torch.float32)
    beta_f = beta.to(torch.float32)
    return torch.nn.functional.layer_norm(vals_f, (channels, ), weight=gamma_f, bias=beta_f, eps=epsilon).to(dtype)


def ds_implementation(vals, gamma, beta, epsilon):
    return inference_module.layer_norm(vals, gamma, beta, epsilon)


@pytest.mark.skipif(not inference_module.has_deepspeed(), reason="deepspeed module is not available")
@pytest.mark.parametrize("batch", [1, 32])
@pytest.mark.parametrize("seq_len", [1, 128])
@pytest.mark.parametrize("channels", [384, 512, 768, 1024, 2048, 8192, 14432])
@pytest.mark.parametrize("dtype", get_dtypes())
def test_layer_norm(batch, seq_len, channels, dtype):
    vals = torch.randn((batch, seq_len, channels), dtype=dtype, device=ipex_device)
    gamma = torch.randn((channels), dtype=dtype, device=ipex_device)
    beta = torch.rand((channels), dtype=dtype, device=ipex_device)
    epsilon = 1e-5

    ref_output = ref_implementation(vals, gamma, beta, epsilon, channels, dtype)
    new_output = ds_implementation(vals, gamma, beta, epsilon)

    if not allclose(new_output, ref_output):
        #print(new_output - ref_output)
        assert allclose(new_output, ref_output)


def residual_ref_implementation(vals, bias, res, gamma, beta, epsilon, channels, dtype):
    vals_f = vals.to(torch.float32)
    bias_f = bias.to(torch.float32).reshape(1, 1, -1)
    res_f = res.to(torch.float32)
    gamma_f = gamma.to(torch.float32)
    beta_f = beta.to(torch.float32)
    return torch.nn.functional.layer_norm(vals_f + bias_f + res_f, (channels, ),
                                          weight=gamma_f,
                                          bias=beta_f,
                                          eps=epsilon).to(dtype)


def residual_ds_implementation(vals, bias, res, gamma, beta, epsilon):
    return inference_module._layer_norm_residual(vals, bias, res, gamma, beta, epsilon)


@pytest.mark.skipif(not inference_module.has_deepspeed(), reason="deepspeed module is not available")
@pytest.mark.parametrize("batch", [1, 32])
@pytest.mark.parametrize("seq_len", [1, 128])
@pytest.mark.parametrize("channels", [384, 512, 768, 1024, 2048, 8192, 14432])
@pytest.mark.parametrize("dtype", get_dtypes())
def test_layer_norm_residual(batch, seq_len, channels, dtype):
    vals = torch.randn((batch, seq_len, channels), dtype=dtype, device=ipex_device)
    residual = torch.randn((batch, seq_len, channels), dtype=dtype, device=ipex_device)
    bias = torch.randn((channels), dtype=dtype, device=ipex_device)
    gamma = torch.randn((channels), dtype=dtype, device=ipex_device)
    beta = torch.rand((channels), dtype=dtype, device=ipex_device)
    epsilon = 1e-5

    new_output = residual_ds_implementation(vals, bias, residual, gamma, beta, epsilon)

    ref_output = residual_ref_implementation(vals, bias, residual, gamma, beta, epsilon, channels, dtype)

    assert allclose(new_output, ref_output)


def residual_store_ref_implementation(vals, bias, res, gamma, beta, epsilon, channels, dtype):
    vals_f = vals.to(torch.float32)
    bias_f = bias.to(torch.float32).reshape(1, 1, -1)
    res_f = res.to(torch.float32)
    gamma_f = gamma.to(torch.float32)
    beta_f = beta.to(torch.float32)
    res_output = vals_f + bias_f + res_f
    norm_output = torch.nn.functional.layer_norm(res_output, (channels, ), weight=gamma_f, bias=beta_f,
                                                 eps=epsilon).to(dtype)
    return norm_output, res_output.to(dtype)


def residual_store_ds_implementation(vals, bias, res, gamma, beta, epsilon):
    return inference_module.layer_norm_residual_store_pre_ln_res(vals, bias, res, gamma, beta, epsilon)


@pytest.mark.skipif(not inference_module.has_deepspeed(), reason="deepspeed module is not available")
@pytest.mark.parametrize("batch", [1, 32])
@pytest.mark.parametrize("seq_len", [1, 128])
@pytest.mark.parametrize("channels", [384, 512, 768, 1024, 2048, 8192, 14432])
@pytest.mark.parametrize("dtype", get_dtypes())
def test_layer_norm_residual_store_pre_ln_res(batch, seq_len, channels, dtype):
    vals = torch.randn((batch, seq_len, channels), dtype=dtype, device=ipex_device)
    residual = torch.randn((batch, seq_len, channels), dtype=dtype, device=ipex_device)
    bias = torch.randn((channels), dtype=dtype, device=ipex_device)
    gamma = torch.randn((channels), dtype=dtype, device=ipex_device)
    beta = torch.rand((channels), dtype=dtype, device=ipex_device)
    epsilon = 1e-5

    # Need to run the reference first since there's an in-place component to ours
    ref_norm_output, norm_res_output = residual_store_ref_implementation(vals, bias, residual, gamma, beta, epsilon,
                                                                         channels, dtype)

    ds_norm_output, ds_res_output = residual_store_ds_implementation(vals, bias, residual, gamma, beta, epsilon)

    assert allclose(ds_res_output, norm_res_output)
    assert allclose(ds_norm_output, ref_norm_output)
