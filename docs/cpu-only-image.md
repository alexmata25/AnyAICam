# CPU-only VMS image

The Docker build installs `torch==2.5.1+cpu` and
`torchvision==0.20.1+cpu` from PyTorch's official CPU wheel index before
installing `ultralytics==8.3.40`. This prevents pip from resolving CUDA
variants or NVIDIA runtime wheels. The remaining Python dependencies are
pinned in `requirements.txt`.

The Python 3.12 slim base is pinned to OCI digest
`sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
so a tag update cannot silently change the operating-system layer.

License plate recognition is disabled by default. Core startup does not
install or import `pytesseract`, and the image does not contain the
Tesseract executable. A separate LPR-capable image may add both dependencies
and explicitly set `ANYAICAM_LPR_ENABLED=true`. When either dependency is
missing, `lpr.capability()` reports LPR as unavailable and recognition calls
return `None` without interrupting cameras, recording, events, or portals.

Verify a built image with:

```sh
docker run --rm IMAGE python -c "import torch, ultralytics; print(torch.__version__, ultralytics.__version__, torch.cuda.is_available())"
docker run --rm IMAGE python -c "import importlib.metadata as m; print([d.metadata['Name'] for d in m.distributions() if d.metadata['Name'].lower().startswith('nvidia-')])"
```
