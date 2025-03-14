import pytest
import torch
from inference_test_utils import get_dtypes
import intel_extension_for_pytorch as ipex

TOLERANCES = None

inference_module = ipex.xpu.deepspeed
ipex_device = 'xpu:0'

def get_tolerances():
    global TOLERANCES
    if TOLERANCES is None:
        # Residual add, as a sequence of casted additions, currently requires a higher tolerance
        # than the other operators for FP16. We should instead better align the behaviors
        # of the reference to match our kernel implementation (TODO(cmikeh2))
        TOLERANCES = {torch.float32: (5e-4, 5e-5), torch.float16: (3e-2, 4e-3)}
        TOLERANCES[torch.bfloat16] = (4.8e-1, 3.2e-2)
    return TOLERANCES


def allclose(x, y):
    assert x.dtype == y.dtype
    rtol, atol = get_tolerances()[x.dtype]
    return torch.allclose(x, y, rtol=rtol, atol=atol)


def res_add_bias_ref(hidden_state, residual, attn_output, attn_bias, final_bias, mp_size=1, pre_attn_norm=True):
    if pre_attn_norm:
        hidden_state += (residual + final_bias + attn_output + attn_bias) / mp_size
    else:
        hidden_state += residual + final_bias
    return hidden_state


def res_add_bias_ref_gptj(hidden_state, residual, attn_output, attn_bias, final_bias, add_attn_bias, mp_size):
    hidden_state += attn_output + (residual + final_bias) / mp_size
    if add_attn_bias:
        hidden_state += attn_bias / mp_size
    return hidden_state


def run_residual_add_reference(hidden_state, residual, attn_output, attn_bias, final_bias, mlp_after_attn,
                               add_attn_bias, mp_size, pre_attn_norm):
    if mlp_after_attn:
        return res_add_bias_ref(hidden_state, residual, attn_output, attn_bias, final_bias, mp_size, pre_attn_norm)
    else:
        return res_add_bias_ref_gptj(hidden_state, residual, attn_output, attn_bias, final_bias, add_attn_bias,
                                     mp_size)


@pytest.mark.skipif(not inference_module.has_deepspeed(), reason="deepspeed module is not available")
@pytest.mark.parametrize("batch", [1, 2])
@pytest.mark.parametrize("sequence", [1, 128, 255])
@pytest.mark.parametrize("hidden_dim", [512, 1232, 4096])
@pytest.mark.parametrize("dtype", get_dtypes())
@pytest.mark.parametrize("mlp_after_attn", [True, False])
@pytest.mark.parametrize("add_bias", [True, False])
@pytest.mark.parametrize("mp_size", [1, 2])
@pytest.mark.parametrize("pre_attn_norm", [True, False])
@pytest.mark.parametrize("use_triton_ops", [False])
def test_residual_add(batch, sequence, hidden_dim, dtype, mlp_after_attn, add_bias, mp_size,
                      pre_attn_norm, use_triton_ops):
    if use_triton_ops and dtype == torch.float16:
        pytest.skip("triton has to be installed for the test")
    ds_out = torch.randn((batch, sequence, hidden_dim), dtype=dtype, device=ipex_device)
    residual = torch.randn((batch, sequence, hidden_dim), dtype=dtype, device=ipex_device)
    attn_output = torch.randn((batch, sequence, hidden_dim), dtype=dtype, device=ipex_device)
    final_bias = torch.randn((hidden_dim), dtype=dtype, device=ipex_device)
    attn_bias = torch.randn((hidden_dim), dtype=dtype, device=ipex_device)

    ref_out = ds_out.clone()
    ref_out = run_residual_add_reference(ref_out, residual, attn_output, attn_bias, final_bias, mlp_after_attn,
                                         add_bias, mp_size, pre_attn_norm)

    res_add_args = [
        ds_out, residual, attn_output, attn_bias, final_bias, mp_size, mlp_after_attn, add_bias, pre_attn_norm
    ]

    # if use_triton_ops:
    #     from deepspeed.ops.transformer.inference.triton import residual_add_bias
    #     ds_out = residual_add_bias(*res_add_args)
    if dtype == torch.float16:
        ds_out = inference_module.residual_add_bias_fp16(*res_add_args)
    elif dtype == torch.float32:
        ds_out = inference_module.residual_add_bias_fp32(*res_add_args)
    elif dtype == torch.bfloat16:
        ds_out = inference_module.residual_add_bias_bf16(*res_add_args)
    else:
        if dtype == torch.float16:
            ds_out = inference_module.residual_add_bias_fp16(*res_add_args)
        elif dtype == torch.float32:
            ds_out = inference_module.residual_add_bias_fp32(*res_add_args)
        else:
            raise ValueError(f"Unsupported dtype: {dtype}")

    if not allclose(ds_out, ref_out):
        print((ds_out - ref_out).abs().max())
        print((ds_out - ref_out).abs().mean())
        print((ds_out - ref_out))
        assert (allclose(ds_out, ref_out))

    assert (allclose(ds_out, ref_out))
