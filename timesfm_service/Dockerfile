FROM python:3.11-slim

WORKDIR /app

# Install build deps for some timesfm native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install CPU-only PyTorch first (before timesfm) to prevent CUDA wheels being pulled
RUN pip install --no-cache-dir \
    torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

# Install timesfm without torch extra (torch already installed above)
RUN pip install --no-cache-dir timesfm

# Install remaining deps
RUN pip install --no-cache-dir -r requirements.txt

# Bake model weights into the image so no HuggingFace download at runtime.
# The image will be ~2GB but pods start without any external network dependency.
RUN python3 -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('google/timesfm-2.5-200m-pytorch', local_dir='/app/model_cache')"

ENV HF_HOME=/app/model_cache
ENV TRANSFORMERS_OFFLINE=1
ENV HF_DATASETS_OFFLINE=1

COPY server.py .

RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

EXPOSE 8080
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
