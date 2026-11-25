FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libgomp1 libatomic1 && rm -rf /var/lib/apt/lists/*
# Python only speaks Wyoming and HTTP; the official Rust binary runs the model.
RUN uv pip install --system aiohttp==3.14.5 wyoming==1.8.0 num2words==0.5.14
COPY wyoming_tts_server.py text_normalization.py THIRD_PARTY_NOTICES.md ./
COPY assets/pocket-tts /app/pocket-tts-rust
COPY assets/LICENSE /app/LICENSE-pocket-tts
RUN /app/pocket-tts-rust --version
ENV POCKET_TTS_BACKEND_BINARY=/app/pocket-tts-rust \
    POCKET_TTS_VERSION=kyutai-v4.0.1 \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 10201
CMD ["python", "wyoming_tts_server.py", "--host", "0.0.0.0", "--port", "10201"]
