FROM python:3.12-slim
# ffmpeg converts Opus/AAC/M4A to 16 kHz mono PCM — without it recognition
# fails on anything that is not WAV.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "onnx-asr[cpu,hub]==0.12.0"
# Piper: standalone binary, no Python needed.
RUN mkdir -p /opt/piper && cd /opt/piper && \
    curl -sL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz \
      | tar xz --strip-components=1 && \
    curl -sL -o de-thorsten-high.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx && \
    curl -sL -o de-thorsten-high.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json && \
    curl -sL -o de-thorsten-medium.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx && \
    curl -sL -o de-thorsten-medium.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json && \
    curl -sL -o de-eva_k-x_low.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/eva_k/x_low/de_DE-eva_k-x_low.onnx && \
    curl -sL -o de-eva_k-x_low.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/eva_k/x_low/de_DE-eva_k-x_low.onnx.json && \
    curl -sL -o en-amy-medium.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx && \
    curl -sL -o en-amy-medium.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
ENV LD_LIBRARY_PATH=/opt/piper
# The recognizer's weights are part of the image: a recreated container
# used to fetch ~600 MB on its first request and answer nothing for a minute.
RUN python -c "import onnx_asr; onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v3', quantization='int8')"
COPY server.py /opt/voice/server.py
EXPOSE 8770
CMD ["python", "-u", "/opt/voice/server.py"]
