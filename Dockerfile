FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-cpu.txt /tmp/requirements-cpu.txt
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements-cpu.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt
COPY ./app /app
# PPE model weights (2026-09-17): ppe.py's PPE_MODEL_NAME default
# ("yolov8n-ppe.pt", a bare relative filename Ultralytics' YOLO()
# resolves against the current working directory, /app per WORKDIR
# above) has never been an Ultralytics-hosted model name -- unlike
# main.py's own YOLO_MODEL_NAME ("yolov8n.pt", a real Ultralytics
# pretrained model Ultralytics auto-downloads on first use), so every
# _get_model() call here always failed and PPE detection had silently
# never worked anywhere, confirmed live on the real Ryzen appliance.
# Fetched once at build time (not at container runtime, so a real
# deployment never depends on Hugging Face being reachable) from the
# MIT-licensed community model this module's own docstring already
# names (Tanishjain9/yolov8n-ppe-detection-6classes), checksum-pinned
# against the exact file this fix was verified against.
RUN curl -fsSL -o /app/yolov8n-ppe.pt "https://huggingface.co/Tanishjain9/yolov8n-ppe-detection-6classes/resolve/main/best.pt" \
    && echo "07172ef3ae9e256c40a1fb0ce3eefe5547d90170645aa73dded0fffc382cdb31  /app/yolov8n-ppe.pt" | sha256sum -c -
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
