#!/usr/bin/env bash
# Public entry point. Run on the Arduino, from any working directory.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"
fail() { echo "Error: $*" >&2; exit 1; }
case "${1:-help}" in
    stats)
        shopt -s nullglob
        files=("$ROOT"/logs/metrics/metrics.jsonl*)
        (( ${#files[@]} )) || fail 'No metrics collected yet.'
        exec python3 "$ROOT/gateway/latency_stats.py" "${files[@]}"
        ;;
    check|prepare|prepare-audio|start|status|stop|sound-test) action=$1 ;;
    *) echo 'Usage: ./run.sh check | prepare | prepare-audio | start | status | stop | stats | sound-test'
       echo 'First: cp .env.example .env, then edit .env.'; exit 0 ;;
esac
[[ -f .env ]] || fail 'Copy .env.example to .env and configure the paths.'
set -a
source .env
NCTX="${NCTX:-10240}"
recipe_tokenizer=0
if [[ -z ${TOKENIZER:-} && -z ${COUNTER_URL:-} ]]; then
    TOKENIZER="$ROOT/assets/qwen-tokenizer"
    recipe_tokenizer=1
fi
GENIEX_BIN="${GENIEX_BIN:-$HOME/.local/bin/geniex}"
CACHE_POLICY="${CACHE_POLICY:-reuse}"
AGENT_LANGUAGE="${AGENT_LANGUAGE:-pl}"
STT_PROVIDER="${STT_PROVIDER:-whisper-qnn}"
TTS_PROVIDER="${TTS_PROVIDER:-pocket}"
set +a
case "$action" in
    check|prepare|start|status|stop)
        (cd "$ROOT/gateway" && python3 -B -m languages -- "$AGENT_LANGUAGE") ;;
esac

[[ $STT_PROVIDER == whisper-qnn || $STT_PROVIDER == external ]] || fail 'STT_PROVIDER must be whisper-qnn or external.'
case "$TTS_PROVIDER" in
    pocket|pocket-pl|external) ;;
    *) fail 'TTS_PROVIDER must be pocket or external (legacy pocket-pl is accepted).' ;;
esac
tts_assets=tts-pocket-wyoming/assets
tts_tokenizer=pl_bpe4000.model
tts_reference=reference.wav
if [[ $AGENT_LANGUAGE != pl ]]; then
    tts_assets+="/$AGENT_LANGUAGE"
    tts_tokenizer=tokenizer.json
    tts_reference=reference.safetensors
fi
compose() { docker compose --env-file "$ROOT/.env" -f "$ROOT/$1/docker-compose.yml" "${@:2}"; }

prepare_audio() {
    [[ ${LLM_WAIT_SOUND:-0} == 0 || ${LLM_WAIT_SOUND:-0} == 1 ]] || fail 'LLM_WAIT_SOUND must be 0 or 1.'
    [[ ${LLM_WAIT_SOUND:-0} == 1 ]] || { echo 'LLM waiting sound disabled. No host audio packages needed.'; return; }
    if ! command -v paplay >/dev/null; then
        command -v apt-get >/dev/null || fail 'Install pulseaudio-utils for your distribution, then retry.'
        local privilege=()
        if (( EUID != 0 )); then
            command -v sudo >/dev/null || fail 'sudo is required to install pulseaudio-utils.'
            privilege=(sudo)
        fi
        echo 'Installing the optional paplay client (pulseaudio-utils).'
        "${privilege[@]}" apt-get update
        "${privilege[@]}" apt-get install -y --no-install-recommends pulseaudio-utils
        command -v paplay >/dev/null || fail 'paplay still unavailable after package installation.'
    fi
    if [[ ${LLM_WAIT_SOUND_VOLUME:-lva} == lva ]]; then
        command -v uv >/dev/null || fail 'Install uv following the README.'
        [[ -x "$ROOT/.venv/bin/python" ]] || uv venv --python python3 "$ROOT/.venv"
        uv pip install --python "$ROOT/.venv/bin/python" websockets==15.0.1
    fi
}

case "$action" in
prepare-audio)
    prepare_audio
    ;;
sound-test)
    sound_python=python3
    [[ ! -x "$ROOT/.venv/bin/python" ]] || sound_python="$ROOT/.venv/bin/python"
    exec "$sound_python" "$ROOT/gateway/wait_sound.py"
    ;;
check)
    errors=0
    check() {
        local label=$1; shift
        if "$@" >/dev/null 2>&1; then echo "OK: $label"; else echo "MISSING: $label"; errors=1; fi
    }
    check 'Arduino ARM64' test "$(uname -m)" = aarch64
    for tool in python3 uv docker systemctl curl; do check "$tool" command -v "$tool"; done
    check 'GenieX version compatible with CACHE_POLICY' python3 llm/backend.py version --geniex-bin "$GENIEX_BIN" --cache-policy "$CACHE_POLICY"
    check 'Docker daemon' docker info
    check 'Docker Compose plugin' docker compose version
    check 'user systemd session' systemctl --user show-environment
    check 'Qwen GGUF (GGUF in .env)' test -f "${GGUF:-}"
    if [[ -n ${COUNTER_URL:-} ]]; then
        check 'Prompt counter service (COUNTER_URL)' \
            curl --fail --silent --show-error --max-time 5 "${COUNTER_URL%/count}/healthz"
    else
        check 'Prompt counter tokenizer (TOKENIZER/tokenizer.json)' test -f "$TOKENIZER/tokenizer.json"
    fi
    for device in /dev/fastrpc-cdsp /dev/fastrpc-cdsp-secure /dev/dma_heap/system /dev/dma_heap/qcom,system; do
        check "NPU device $device" test -c "$device"
    done
    if [[ $STT_PROVIDER == whisper-qnn ]]; then
        check 'QAIRT (QNN_LIB_DIR in .env)' test -f "${QNN_LIB_DIR:-}/libQnnHtp.so"
        check 'STT tokenizer' test -f stt/assets/tokenizer.json
        check 'STT model' test -f stt/assets/model/whisper_large_v3_turbo_quantized-precompiled_qnn_onnx-w8a16-qualcomm_qcs8275/decoder.bin
    else
        echo 'EXTERNAL: select your STT integration and language in Home Assistant.'
    fi
    if [[ $TTS_PROVIDER != external ]]; then
        check 'TTS configuration' test -f "$tts_assets/config.yaml"
        check 'TTS model' test -f "$tts_assets/model.safetensors"
        check 'TTS tokenizer' test -f "$tts_assets/$tts_tokenizer"
        check 'TTS voice' test -f "$tts_assets/$tts_reference"
        check 'Rust ARM64' test -x tts-pocket-wyoming/assets/pocket-tts
        check 'Rust license for image build' test -f tts-pocket-wyoming/assets/LICENSE
    else
        echo 'EXTERNAL: select your TTS integration and language in Home Assistant.'
    fi
    check 'PulseAudio/PipeWire' test -S "/run/user/${LVA_USER_ID:-1000}/pulse/native"
    if [[ ${LLM_WAIT_SOUND:-0} == 1 ]]; then
        check 'LLM waiting sound player (run ./run.sh prepare-audio)' command -v paplay
        if [[ -n ${LLM_WAIT_SOUND_FILE:-} ]]; then
            check 'LLM waiting sound file' test -r "$LLM_WAIT_SOUND_FILE"
        fi
        if [[ ${LLM_WAIT_SOUND_VOLUME:-lva} == lva ]]; then
            check 'LVA volume sync library (run ./run.sh prepare-audio)' \
                "$ROOT/.venv/bin/python" -c 'from websockets.sync.client import connect'
        fi
    fi
    exit "$errors"
    ;;
prepare)
    [[ $(uname -m) == aarch64 ]] || fail 'Run preparation on the ARM64 Arduino.'
    [[ -x $GENIEX_BIN ]] || fail 'Install GenieX following the README.'
    if [[ -z ${COUNTER_URL:-} ]] && (( ! recipe_tokenizer )) && [[ ! -f $TOKENIZER/tokenizer.json ]]; then
        fail 'Provide tokenizer.json in the configured TOKENIZER directory, or leave TOKENIZER empty for the bundled recipe.'
    fi
    "$GENIEX_BIN" --skip-update pull bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0
    if (( recipe_tokenizer )); then
        mkdir -p "$TOKENIZER"
        for file in tokenizer.json tokenizer_config.json special_tokens_map.json; do
            [[ -f $TOKENIZER/$file ]] || curl -fL "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/resolve/main/$file" -o "$TOKENIZER/$file"
        done
    fi
    if [[ $STT_PROVIDER == whisper-qnn && ! -d stt/assets ]]; then
        python3 stt/download_whisper_turbo_on_board.py --output "$ROOT/stt/assets" --staging /dev/shm/ha-whisper-download
    fi
    if [[ $TTS_PROVIDER != external ]]; then
        python3 tts-pocket-wyoming/prepare_assets.py --language "$AGENT_LANGUAGE"
    fi
    prepare_audio
    echo 'Assets downloaded. Set GGUF and QNN_LIB_DIR in .env. ORT/QNN Python packages are installed during the STT image build.'
    ;;
start)
    "$ROOT/run.sh" check || fail 'First resolve the items marked MISSING.'
    if [[ $STT_PROVIDER == whisper-qnn ]]; then
        compose stt build
        compose stt create
    fi
    if [[ $TTS_PROVIDER != external ]]; then
        compose tts-pocket-wyoming build
    fi
    FULL_STACK=1 "$ROOT/setup.sh"
    if [[ $TTS_PROVIDER != external ]]; then
        compose tts-pocket-wyoming up -d
    fi
    compose lva up -d
    echo 'Started. Home Assistant configuration: README, section 3.'
    ;;
status)
    units=(qwen25-3b-backend qwen-ha-gateway)
    [[ -n ${COUNTER_URL:-} ]] || units+=(qwen-ha-counter)
    [[ $STT_PROVIDER != whisper-qnn ]] || units+=(qwen25-whisper-npu)
    systemctl --user --no-pager status "${units[@]}" || true
    docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
stop)
    if [[ $STT_PROVIDER == whisper-qnn ]]; then
        systemctl --user stop qwen25-whisper-npu
    fi
    for unit in qwen-ha-gateway qwen-ha-counter qwen25-3b-backend; do
        systemctl --user stop "$unit" 2>/dev/null || true
    done
    if [[ $TTS_PROVIDER != external ]]; then
        compose tts-pocket-wyoming stop
    fi
    compose lva stop
    ;;
esac
