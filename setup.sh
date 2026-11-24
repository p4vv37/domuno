#!/usr/bin/env bash
# Run as a regular user with an active systemd --user session.
set -euo pipefail
umask 077

DEPLOY_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MODEL="${MODEL:-bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0}"
NCTX="${NCTX:-10240}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:18192}"
GATEWAY_HOST="${GATEWAY_HOST:-0.0.0.0}"
GATEWAY_PORT="${GATEWAY_PORT:-18191}"
COUNTER_URL="${COUNTER_URL:-}"
GGUF="${GGUF:-}"
TOKENIZER="${TOKENIZER:-}"
COUNTER_PORT="${COUNTER_PORT:-18193}"
DECIDE_APPLY="${DECIDE_APPLY:-1}"
CACHE_POLICY="${CACHE_POLICY:-reuse}"
RESPONSE_STYLE="${RESPONSE_STYLE:-concise}"
ROUTER_MISS_LOGGING="${ROUTER_MISS_LOGGING:-0}"
AGENT_LANGUAGE="${AGENT_LANGUAGE:-pl}"
STT_PROVIDER="${STT_PROVIDER:-whisper-qnn}"
TTS_PROVIDER="${TTS_PROVIDER:-pocket-pl}"
FULL_STACK="${FULL_STACK:-0}"
GENIEX_BIN="${GENIEX_BIN:-$HOME/.local/bin/geniex}"
LOG_DIR="$DEPLOY_DIR/logs"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

fail() { echo "Error: $*" >&2; exit 1; }
command -v python3 >/dev/null || fail 'Install Python 3.10+.'
command -v uv >/dev/null || fail 'Install uv following the README.'
command -v systemctl >/dev/null || fail 'systemd is required.'
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail 'Python 3.10+ is required.'
[[ $EUID -ne 0 ]] || fail 'Run without sudo, as the service user.'
[[ $NCTX =~ ^[0-9]+$ ]] && (( NCTX >= 2048 && NCTX <= 10240 )) || fail 'NCTX must be between 2048 and 10240.'
[[ $DECIDE_APPLY == 0 || $DECIDE_APPLY == 1 ]] || fail 'DECIDE_APPLY must be 0 or 1.'
[[ $CACHE_POLICY == reuse || $CACHE_POLICY == reset ]] || fail 'CACHE_POLICY must be reuse or reset.'
[[ $RESPONSE_STYLE == concise || $RESPONSE_STYLE == original ]] || fail 'RESPONSE_STYLE must be concise or original.'
[[ $ROUTER_MISS_LOGGING == 0 || $ROUTER_MISS_LOGGING == 1 ]] || fail 'ROUTER_MISS_LOGGING must be 0 or 1.'
(cd "$DEPLOY_DIR/gateway" && python3 -B -m languages -- "$AGENT_LANGUAGE")

[[ $STT_PROVIDER == whisper-qnn || $STT_PROVIDER == external ]] || fail 'STT_PROVIDER must be whisper-qnn or external.'
[[ $TTS_PROVIDER == pocket-pl || $TTS_PROVIDER == external ]] || fail 'TTS_PROVIDER must be pocket-pl or external.'
if [[ $FULL_STACK == 1 && $AGENT_LANGUAGE != pl ]]; then
    [[ $STT_PROVIDER == external && $TTS_PROVIDER == external ]] || fail 'Local speech recipes require Polish; select external STT and TTS for other languages.'
fi
for port in "$GATEWAY_PORT" "$COUNTER_PORT"; do
    [[ $port =~ ^[0-9]+$ ]] && (( port > 0 && port < 65536 )) || fail 'Invalid port.'
done
# systemd has its own quoting and expansion rules. Reject unsupported special
# characters to avoid invalid units or unintended configuration expansion.
for value in "$DEPLOY_DIR" "$MODEL" "$BACKEND_URL" "$GATEWAY_HOST" "$COUNTER_URL" "$GGUF" "$TOKENIZER" "$GENIEX_BIN"; do
    [[ ! $value =~ [\"\$\%\\] && $value != *$'\n'* && $value != *$'\r'* ]] || fail 'Configuration contains a character unsupported by the installer.'
done
systemctl --user show-environment >/dev/null || fail 'No systemd --user session. Run from a terminal on the Arduino.'
if [[ -n $GGUF || -n $TOKENIZER ]]; then
    [[ -z $COUNTER_URL ]] || fail 'Choose GGUF + TOKENIZER or an external COUNTER_URL.'
    [[ -f $GGUF && -f $TOKENIZER/tokenizer.json ]] || fail 'Provide an existing GGUF file and a TOKENIZER directory containing tokenizer.json.'
    COUNTER_URL="http://127.0.0.1:$COUNTER_PORT/count"
    LOCAL_COUNTER=1
elif [[ -n $COUNTER_URL ]]; then
    LOCAL_COUNTER=0
else
    fail 'A prompt counter is required: set GGUF + TOKENIZER or COUNTER_URL.'
fi

if [[ $FULL_STACK == 1 ]]; then
    [[ -x $GENIEX_BIN ]] || fail 'GenieX binary not found. Check GENIEX_BIN.'
    GENIEX_VERSION="$(python3 "$DEPLOY_DIR/llm/backend.py" version --geniex-bin "$GENIEX_BIN" --cache-policy "$CACHE_POLICY")"
    BACKEND_FLAGS=""
    [[ $GENIEX_VERSION != 0.11.0 ]] || BACKEND_FLAGS=" --think=false --metrics"
fi

cd "$DEPLOY_DIR"
[[ -x .venv/bin/python ]] || uv venv --python python3 .venv
uv pip install --python "$DEPLOY_DIR/.venv/bin/python" \
    simplemma==2.0.0 num2words==0.5.14 jsonschema==4.25.1 RapidFuzz==3.14.1
if [[ ${LLM_WAIT_SOUND:-0} == 1 && ${LLM_WAIT_SOUND_VOLUME:-lva} == lva ]]; then
    uv pip install --python "$DEPLOY_DIR/.venv/bin/python" websockets==15.0.1
fi
# Polish declension (SGJP data) lets the router match inflected names. It is
# optional at runtime: the gateway falls back to lexical matching when absent.
if [[ $AGENT_LANGUAGE == pl ]]; then
    uv pip install --python "$DEPLOY_DIR/.venv/bin/python" polish-inflection==0.7.3 polish-inflection-data==2026.6.28 \
        || echo 'Warning: polish-inflection unavailable; router stays on lexical matching.' >&2
fi
if (( LOCAL_COUNTER )); then
    uv pip install --python "$DEPLOY_DIR/.venv/bin/python" transformers==4.57.1
fi
mkdir -p "$LOG_DIR" "$UNIT_DIR"
chmod 700 "$LOG_DIR"

if (( LOCAL_COUNTER )); then
    cat > "$UNIT_DIR/qwen-ha-counter.service" <<EOF
[Unit]
Description=Qwen HA offline prompt counter (CPU)

[Service]
Type=exec
WorkingDirectory=$DEPLOY_DIR
ExecStart="$DEPLOY_DIR/.venv/bin/python" -u "$DEPLOY_DIR/gateway/prompt_counter.py" --port $COUNTER_PORT --gguf "$GGUF" --tokenizer "$TOKENIZER"
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
EOF
fi

EXEC="\"$DEPLOY_DIR/.venv/bin/python\" -u \"$DEPLOY_DIR/gateway/chat_gateway.py\""
EXEC+=" --host \"$GATEWAY_HOST\" --port $GATEWAY_PORT --upstream \"$BACKEND_URL\""
EXEC+=" --model \"$MODEL\" --nctx $NCTX"
EXEC+=" --cache-policy $CACHE_POLICY --response-style $RESPONSE_STYLE"
# The directory is stable. The gateway reads the on/off switch and optional
# cutoff from EnvironmentFile at startup, so editing .env needs only a restart.
EXEC+=" --router-miss-log-dir \"$LOG_DIR/router-misses\""
EXEC+=" --metrics-dir \"$LOG_DIR/metrics\""
EXEC+=" --context-audit-dir \"$LOG_DIR/context-audit\" --request-capture-dir \"$LOG_DIR/request-capture\""
[[ -z $COUNTER_URL ]] || EXEC+=" --prompt-counter-url \"$COUNTER_URL\""
[[ $DECIDE_APPLY == 0 ]] || EXEC+=' --decide-apply'
cat > "$UNIT_DIR/qwen-ha-gateway.service" <<EOF
[Unit]
Description=Qwen HA gateway and rule router
After=network-online.target

[Service]
Type=notify
NotifyAccess=main
TimeoutStartSec=60
WorkingDirectory=$DEPLOY_DIR
ExecStart=$EXEC
Restart=on-failure
RestartSec=3
UMask=0077
# Safe fallbacks for an older .env that lacks these switches. EnvironmentFile
# is read afterwards, so values in .env take precedence.
Environment=ROUTER_MISS_LOGGING=$ROUTER_MISS_LOGGING
Environment=CONTEXT_AUDIT=1 CONTEXT_CAPTURE=0
Environment=AGENT_LANGUAGE=$AGENT_LANGUAGE
EnvironmentFile=-$DEPLOY_DIR/.env

[Install]
WantedBy=default.target
EOF

if [[ $FULL_STACK == 1 ]]; then
    [[ -x $GENIEX_BIN ]] || fail 'GenieX binary not found. Check GENIEX_BIN.'
    cat > "$UNIT_DIR/qwen25-3b-backend.service" <<EOF
[Unit]
Description=Qwen local NPU backend
After=network-online.target

[Service]
Type=exec
Environment=GGML_HEXAGON_DEVICES=1
ExecStartPre=/usr/bin/python3 "$DEPLOY_DIR/llm/backend.py" guard
ExecStart="$GENIEX_BIN" --skip-update --log warn serve --host 127.0.0.1:18192 --compute HTP0 --ngl -1 --nctx $NCTX --keepalive 86400$BACKEND_FLAGS
ExecStartPost=/usr/bin/python3 "$DEPLOY_DIR/llm/backend.py" probe --nctx $NCTX
Restart=no
LimitCORE=0
TimeoutStartSec=180
TimeoutStopSec=45

[Install]
WantedBy=default.target
EOF
    if [[ $STT_PROVIDER == whisper-qnn ]]; then
        cat > "$UNIT_DIR/qwen25-whisper-npu.service" <<EOF
[Unit]
Description=Whisper Turbo after Qwen cold load
Requires=qwen25-3b-backend.service
After=qwen25-3b-backend.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker start whisper-turbo-qnn
ExecStartPost=/usr/bin/python3 "$DEPLOY_DIR/llm/backend.py" stt-ready
ExecStop=/usr/bin/docker stop whisper-turbo-qnn
TimeoutStartSec=100
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF
    fi
fi

systemctl --user daemon-reload
if (( LOCAL_COUNTER )); then
    systemctl --user enable qwen-ha-counter.service
    systemctl --user restart qwen-ha-counter.service
fi
if [[ $FULL_STACK == 1 ]]; then
    systemctl --user enable qwen25-3b-backend.service
    if [[ $STT_PROVIDER == whisper-qnn ]]; then
        systemctl --user enable qwen25-whisper-npu.service
    fi
    systemctl --user start qwen25-3b-backend.service
    if [[ $STT_PROVIDER == whisper-qnn ]]; then
        systemctl --user start qwen25-whisper-npu.service
    fi
fi
systemctl --user enable qwen-ha-gateway.service
# enable --now does not reload a running service after configuration changes.
systemctl --user restart qwen-ha-gateway.service
systemctl --user is-active --quiet qwen-ha-gateway.service || fail 'Gateway failed to start. Check journalctl --user -u qwen-ha-gateway -n 30.'
echo "Gateway is running. Home Assistant: http://<Arduino-IP>:$GATEWAY_PORT/v1"
echo 'Also run the inference check from the README. Service status alone does not verify model inference.'
echo 'Start automatically without logging in: sudo loginctl enable-linger "$USER"'
