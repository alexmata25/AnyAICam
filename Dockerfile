FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr espeak-ng curl wireguard-tools iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-cpu.txt /tmp/requirements-cpu.txt
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements-cpu.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt
COPY ./app /app
# PPE model weights (2026-09-17): ppe.py's PPE_MODEL_NAME default
# has never been an Ultralytics-hosted model name -- unlike main.py's
# own YOLO_MODEL_NAME ("yolov8n.pt", a real Ultralytics pretrained
# model Ultralytics auto-downloads on first use), so every _get_model()
# call here always failed and PPE detection had silently never worked
# anywhere, confirmed live on the real Ryzen appliance. Fetched once at
# build time (not at container runtime, so a real deployment never
# depends on Hugging Face being reachable) from the MIT-licensed
# community model this module's own docstring already names
# (Tanishjain9/yolov8n-ppe-detection-6classes), checksum-pinned against
# the exact file this fix was verified against.
#
# Placed OUTSIDE /app (2026-09-17, real bug confirmed live on Ryzen):
# the real appliance's docker-compose.yml bind-mounts the host's own
# `./app` source tree over the image's own `/app` at container runtime
# (`- ./app:/app`, the same mechanism that makes an installed appliance
# run the exact release payload's Python source) -- so anything baked
# into the image's own `/app` by a Dockerfile RUN step, rather than
# copied from the git-tracked `./app` source, is invisible the moment
# the container actually starts, not just theoretically shadowed. This
# is exactly why the model loaded fine in isolated image-only testing
# but FileNotFoundError'd on the real running appliance. /opt/anyaicam-
# ppe-model is never bind-mounted over by anything in docker-compose.
# yml, so the baked file survives into the real running container.
RUN mkdir -p /opt/anyaicam-ppe-model \
    && curl -fsSL -o /opt/anyaicam-ppe-model/yolov8n-ppe.pt "https://huggingface.co/Tanishjain9/yolov8n-ppe-detection-6classes/resolve/main/best.pt" \
    && echo "07172ef3ae9e256c40a1fb0ce3eefe5547d90170645aa73dded0fffc382cdb31  /opt/anyaicam-ppe-model/yolov8n-ppe.pt" | sha256sum -c -

# Face Access (AAC) production engine models (2026-09-25): YuNet detector,
# SFace aligner and ArcFace embedding, exactly the URLs and SHA-256 values
# pinned in app/facial_engine_onnx.py (app/tests/test_facial_model_packaging.py
# keeps the two in sync). Baked outside /app for the same bind-mount
# reason as the PPE model above. Only used when ANYAICAM_FACE_ENGINE
# selects arcface/onnx/auto; the default Haar engine never loads them.
RUN mkdir -p /opt/anyaicam-aac-models \
    && curl -fsSL -o /opt/anyaicam-aac-models/face_detection_yunet_2023mar.onnx "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
    && echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  /opt/anyaicam-aac-models/face_detection_yunet_2023mar.onnx" | sha256sum -c - \
    && curl -fsSL -o /opt/anyaicam-aac-models/face_recognition_sface_2021dec.onnx "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
    && echo "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  /opt/anyaicam-aac-models/face_recognition_sface_2021dec.onnx" | sha256sum -c - \
    && curl -fsSL -o /opt/anyaicam-aac-models/arcfaceresnet100-11-int8.onnx "https://media.githubusercontent.com/media/onnx/models/main/validated/vision/body_analysis/arcface/model/arcfaceresnet100-11-int8.onnx" \
    && echo "c625ca68a422418c48aa84f73341337e0a92b111f327909005d1eec07c95f936  /opt/anyaicam-aac-models/arcfaceresnet100-11-int8.onnx" | sha256sum -c -
# Licence-plate detector (2026-09-27): the Haar cascade never fires on US
# plates, so app/lpr.py's detect_plate() uses this YOLOv11n plate model
# (morsetechlab/yolov11-license-plate-detection, AGPL-3.0), pinned to an
# exact Hugging Face commit and checksum. Baked outside /app for the same
# bind-mount reason as the PPE model; lpr.py falls back to the cascade if
# the file is missing.
RUN mkdir -p /opt/anyaicam-lpr-model \
    && curl -fsSL -o /opt/anyaicam-lpr-model/license-plate-finetune-v1n.pt "https://huggingface.co/morsetechlab/yolov11-license-plate-detection/resolve/251a30d7daedca065f56e04b0af04052c907c68f/license-plate-finetune-v1n.pt" \
    && echo "0aec75976c56eb6f26dfb274c430620ec65137915ff1ae47c3a48c7af8afb7b2  /opt/anyaicam-lpr-model/license-plate-finetune-v1n.pt" | sha256sum -c -

# Offline speech-to-text for AAC Voice Call visitor answers (2026-09-28):
# Vosk small English model (Apache-2.0), checksum-pinned; used by
# app/aac_voice_call_listen.py. Baked outside /app for the same bind-mount
# reason as the models above; listening is skipped if it is missing.
RUN mkdir -p /opt/anyaicam-stt-model \
    && curl -fsSL -o /tmp/vosk-model.zip "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip" \
    && echo "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498  /tmp/vosk-model.zip" | sha256sum -c - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/vosk-model.zip').extractall('/opt/anyaicam-stt-model')" \
    && rm /tmp/vosk-model.zip \
    && test -d /opt/anyaicam-stt-model/vosk-model-small-en-us-0.15/am

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
