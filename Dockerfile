FROM python:3.12-slim

# System packages needed by OpenCV, v4l2, and supervisord
RUN apt-get update && apt-get install -y --no-install-recommends \
        v4l-utils \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch — much smaller image than the CUDA/ROCm variants.
# To use a GPU, replace the --index-url with the appropriate CUDA/ROCm wheel
# URL and set inference.use_gpu = true in config.json.
RUN pip install --no-cache-dir \
        torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu

# Remaining Python deps.
# opencv-python-headless omits GUI backends (no X11 needed in Docker).
# byml from PyPI; state_machine.py's local sys.path.insert is a harmless
# no-op when that directory doesn't exist.
# supervisor manages all three processes inside the single container.
RUN pip install --no-cache-dir \
        fastai \
        ipython \
        opencv-python-headless \
        numpy \
        byml \
        supervisor

# Copy project files. .dockerignore excludes training data, env/, etc.
COPY . .

CMD ["supervisord", "-c", "/app/supervisord.conf"]
