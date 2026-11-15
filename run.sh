#!/usr/bin/env bash
# Public entry point. Run on the Arduino, from any working directory.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"
fail() { echo "Error: $*" >&2; exit 1; }
case "${1:-help}" in
    check|prepare|start|status|stop) action=$1 ;;
    *) echo 'Usage: ./run.sh check | prepare | start | status | stop'
       echo 'First: cp .env.example .env, then edit .env.'; exit 0 ;;
esac
[[ -f .env ]] || fail 'Copy .env.example to .env and configure the paths.'
set -a
source .env
NCTX="${NCTX:-10240}"
TOKENIZER="${TOKENIZER:-$ROOT/assets/qwen-tokenizer}"
GENIEX_BIN="${GENIEX_BIN:-$HOME/.local/bin/geniex}"
set +a
compose() { docker compose --env-file "$ROOT/.env" -f "$ROOT/$1/docker-compose.yml" "${@:2}"; }

case "$action" in
check)
    errors=0
    check() {
        local label=$1; shift
        if "$@" >/dev/null 2>&1; then echo "OK: $label"; else echo "MISSING: $label"; errors=1; fi
    }
    check 'Arduino ARM64' test "$(uname -m)" = aarch64
    for tool in python3 uv docker systemctl; do check "$tool" command -v "$tool"; done
    check 'GenieX' test -x "$GENIEX_BIN"
    check 'Docker daemon' docker info
    check 'user systemd session' systemctl --user show-environment
    check 'Qwen GGUF (GGUF in .env)' test -f "${GGUF:-}"
    check 'Qwen tokenizer' test -f "$TOKENIZER/tokenizer.json"
    check 'QAIRT (QNN_LIB_DIR in .env)' test -f "${QNN_LIB_DIR:-}/libQnnHtp.so"
    check 'ORT 1.30/QNN packages for CPython 3.12 ARM64 in stt/runtime/' python3 -c 'from pathlib import Path; import sys; names=[p.name for p in Path("stt/runtime").glob("*cp312*aarch64.whl")]; sys.exit(not (any(n.startswith("onnxruntime-1.30.0-") for n in names) and any(n.startswith("onnxruntime_qnn-") for n in names)))'
    check 'STT tokenizer' test -f stt/assets/tokenizer.json
    check 'STT model' test -f stt/assets/model/whisper_large_v3_turbo_quantized-precompiled_qnn_onnx-w8a16-qualcomm_qcs8275/decoder.bin
    check 'TTS configuration' test -f tts-pocket-wyoming/assets/config.yaml
    check 'TTS model' test -f tts-pocket-wyoming/assets/model.safetensors
    check 'TTS voice' test -f tts-pocket-wyoming/assets/reference.wav
    check 'Rust ARM64' test -x tts-pocket-wyoming/assets/pocket-tts
    check 'PulseAudio/PipeWire' test -S "/run/user/${LVA_USER_ID:-1000}/pulse/native"
    exit "$errors"
    ;;
prepare)
    [[ $(uname -m) == aarch64 ]] || fail 'Run preparation on the ARM64 Arduino.'
    [[ -x $GENIEX_BIN ]] || fail 'Install GenieX following the README.'
    "$GENIEX_BIN" --skip-update pull bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0
    mkdir -p "$TOKENIZER"
    for file in tokenizer.json tokenizer_config.json special_tokens_map.json; do
        [[ -f $TOKENIZER/$file ]] || curl -fL "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/resolve/main/$file" -o "$TOKENIZER/$file"
    done
    if [[ ! -d stt/assets ]]; then
        python3 stt/download_whisper_turbo_on_board.py --output "$ROOT/stt/assets" --staging /dev/shm/ha-whisper-download
    fi
    python3 tts-pocket-wyoming/prepare_assets.py
    echo 'Assets downloaded. Set GGUF and QNN_LIB_DIR in .env and place ORT/QNN packages in stt/runtime/.'
    ;;
start)
    "$ROOT/run.sh" check || fail 'First resolve the items marked MISSING.'
    compose stt build
    compose stt create
    compose tts-pocket-wyoming build
    FULL_STACK=1 "$ROOT/setup.sh"
    compose tts-pocket-wyoming up -d
    compose lva up -d
    echo 'Started. Home Assistant configuration: README, section 3.'
    ;;
status)
    systemctl --user --no-pager status qwen25-3b-backend qwen25-whisper-npu qwen-ha-counter qwen-ha-gateway || true
    docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
stop)
    systemctl --user stop qwen25-whisper-npu qwen-ha-gateway qwen-ha-counter qwen25-3b-backend
    compose tts-pocket-wyoming stop
    compose lva stop
    ;;
esac
