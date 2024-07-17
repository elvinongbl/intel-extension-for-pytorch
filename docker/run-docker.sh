#!/bin/bash

# To build
# docker build -f docker/Dockerfile.compile \
#   --build-arg HTTP_PROXY=http://proxy.png.intel.com:911 \
#   --build-arg HTTPS_PROXY=http://proxy.png.intel.com:911 \
#   --build-arg GID_RENDER=$(getent group render | sed -E 's,^render:[^:]*:([^:]*):.*$,\1,') \
#   -t intel/intel-extension-for-pytorch:xpu .

IMAGE_NAME=intel/intel-extension-for-pytorch:xpu

docker run --rm \
    -v /home/bong5/workspace:/workspace \
    --device=/dev/dri \
    --ipc=host \
    -e http_proxy=$http_proxy \
    -e https_proxy=$https_proxy \
    -e no_proxy=$no_proxy \
    -it $IMAGE_NAME bash

# Below are steps to cross-check IPEX build and its APIs:
# $ pip list
    # Package                               Version
    # ------------------------------------- ------------------
    # annotated-types                       0.7.0
    # certifi                               2024.7.4
    # charset-normalizer                    3.3.2
    # filelock                              3.15.4
    # fsspec                                2024.6.1
    # huggingface-hub                       0.23.5
    # idna                                  3.7
    # intel-extension-for-pytorch           2.3.110+git8919ed1
    # intel-extension-for-pytorch-deepspeed 2.3.110
    # Jinja2                                3.1.4
    # MarkupSafe                            2.1.5
    # mpmath                                1.3.0
    # networkx                              3.3
    # numpy                                 1.26.4
    # packaging                             24.1
    # pillow                                10.4.0
    # pip                                   22.0.2
    # psutil                                6.0.0
    # pydantic                              2.8.2
    # pydantic_core                         2.20.1
    # PyYAML                                6.0.1
    # regex                                 2024.5.15
    # requests                              2.32.3
    # safetensors                           0.4.3
    # setuptools                            59.6.0
    # sympy                                 1.13.0
    # tokenizers                            0.19.1
    # torch                                 2.3.0+cxx11.abi
    # torchaudio                            2.3.0+952ea74
    # torchvision                           0.18.0a0+6043bc2
    # tqdm                                  4.66.4
    # transformers                          4.42.4
    # typing_extensions                     4.12.2
    # urllib3                               2.2.2

# IPEX API: https://intel.github.io/intel-extension-for-pytorch/xpu/latest/tutorials/api_doc.html
# $ python
# import torch
# import intel_extension_for_pytorch as ipex
# import torchvision
# import torchaudio
# print(torch.__version__)
# print(ipex.__version__)
# print(torchvision.__version__)
# print(torchaudio.__version__)
# print(torch.xpu.current_device())
# print(torch.xpu.get_device_name(0))
# print(torch.xpu.get_device_properties(0))
# print(ipex.xpu.has_onemkl())
# print(ipex.xpu.has_fp64_dtype())
# print(ipex.xpu.has_xetla())
# print(ipex.xpu.has_xmx())
# print(ipex.xpu.has_xpu())
# print(ipex.xpu.get_fp32_math_mode())
