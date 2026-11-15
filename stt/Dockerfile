FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libgomp1 libatomic1 && rm -rf /var/lib/apt/lists/*
# Supply matching ARM64 CPython 3.12 vendor wheels as described in README.
COPY runtime/ /wheels/
RUN uv pip install --system /wheels/*.whl numpy==2.2.6 tokenizers==0.22.1 faster-whisper==1.2.1 wyoming==1.8.0
RUN python -c "import importlib.util, onnxruntime; assert onnxruntime.__version__ == '1.30.0'; assert importlib.util.find_spec('onnxruntime_qnn')"
COPY qnn_whisper_engine.py qnn_whisper_profile.py qnn_wyoming_server.py ./
ENV LD_LIBRARY_PATH=/host_qnn:/rpc:/usr/local/lib/python3.12/site-packages/onnxruntime/capi:/usr/local/lib/python3.12/site-packages/onnxruntime_qnn \
    ADSP_LIBRARY_PATH=/host_qnn \
    OPENBLAS_NUM_THREADS=2 \
    OMP_NUM_THREADS=2 \
    PYTHONUNBUFFERED=1
HEALTHCHECK --interval=10s --start-period=60s --timeout=3s CMD python -c "import socket; socket.create_connection(('127.0.0.1',10301),2).close()"
ENTRYPOINT ["python", "qnn_wyoming_server.py"]
CMD ["--uri", "tcp://0.0.0.0:10301", "--model-dir", "/turbo/model", "--tokenizer", "/turbo/tokenizer.json", "--backend", "/host_qnn/libQnnHtp.so", "--model-profile", "large-v3-turbo-w8a16", "--metadata", "/turbo/model/whisper_large_v3_turbo_quantized-precompiled_qnn_onnx-w8a16-qualcomm_qcs8275/metadata.json"]
