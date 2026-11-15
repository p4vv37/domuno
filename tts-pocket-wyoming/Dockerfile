FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libgomp1 libatomic1 && rm -rf /var/lib/apt/lists/*
# Rust executes the model. Python handles Wyoming and PCM, without PyTorch.
RUN uv pip install --system numpy==2.2.6 wyoming==1.8.0
COPY wyoming_tts_server.py tts_render_worker.py rust_tts_backend.py ./
COPY assets/pocket-tts /app/pocket-tts-rust
COPY assets/LICENSE /app/LICENSE-pocket-tts
RUN /app/pocket-tts-rust --version
ENV POCKET_TTS_BACKEND=rust \
    POCKET_TTS_RUST_BINARY=/app/pocket-tts-rust \
    POCKET_TTS_RUST_CONFIG=/assets/config.yaml \
    POCKET_TTS_RUST_REFERENCE=/assets/reference.wav \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 10201
CMD ["python", "wyoming_tts_server.py", "--host", "0.0.0.0", "--port", "10201"]
