#!/usr/bin/env bash
# Run as a regular user with an active systemd --user session.
set -euo pipefail
umask 077

DEPLOY_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MODEL="${MODEL:-bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0}"
NCTX="${NCTX:-4096}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:18192}"
GATEWAY_HOST="${GATEWAY_HOST:-0.0.0.0}"
GATEWAY_PORT="${GATEWAY_PORT:-18191}"
COUNTER_URL="${COUNTER_URL:-}"
GGUF="${GGUF:-}"
TOKENIZER="${TOKENIZER:-}"
COUNTER_PORT="${COUNTER_PORT:-18193}"
DECIDE_APPLY="${DECIDE_APPLY:-1}"
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
else
    LOCAL_COUNTER=0
fi
(( NCTX <= 4096 )) || [[ -n $COUNTER_URL ]] || fail 'NCTX > 4096 requires GGUF + TOKENIZER or COUNTER_URL.'

cd "$DEPLOY_DIR"
[[ -x .venv/bin/python ]] || uv venv --python python3 .venv
uv pip install --python "$DEPLOY_DIR/.venv/bin/python" simplemma==2.0.0
if (( LOCAL_COUNTER )); then
    uv pip install --python "$DEPLOY_DIR/.venv/bin/python" transformers==4.57.1
fi
mkdir -p "$LOG_DIR/decision-shadow" "$UNIT_DIR"
chmod 700 "$LOG_DIR" "$LOG_DIR/decision-shadow"

if (( LOCAL_COUNTER )); then
    cat > "$UNIT_DIR/qwen-ha-counter.service" <<EOF
[Unit]
Description=Qwen HA offline prompt counter (CPU)

[Service]
Type=exec
WorkingDirectory="$DEPLOY_DIR"
ExecStart="$DEPLOY_DIR/.venv/bin/python" -u "$DEPLOY_DIR/gateway/qwen_dynamic_budget_server.py" --port $COUNTER_PORT --gguf "$GGUF" --tokenizer "$TOKENIZER"
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
EOF
fi

EXEC="\"$DEPLOY_DIR/.venv/bin/python\" -u \"$DEPLOY_DIR/gateway/qwen_npu_gateway.py\""
EXEC+=" --host \"$GATEWAY_HOST\" --port $GATEWAY_PORT --upstream \"$BACKEND_URL\""
EXEC+=" --model \"$MODEL\" --nctx $NCTX --decide-shadow-dir \"$LOG_DIR/decision-shadow\""
[[ -z $COUNTER_URL ]] || EXEC+=" --prompt-counter-url \"$COUNTER_URL\""
[[ $DECIDE_APPLY == 0 ]] || EXEC+=' --decide-apply'
cat > "$UNIT_DIR/qwen-ha-gateway.service" <<EOF
[Unit]
Description=Qwen HA gateway and rule router
After=network-online.target

[Service]
Type=exec
WorkingDirectory="$DEPLOY_DIR"
ExecStart=$EXEC
Restart=on-failure
RestartSec=3
UMask=0077
EnvironmentFile="-$DEPLOY_DIR/.env"

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
ExecStart="$GENIEX_BIN" --skip-update --log warn serve --host 127.0.0.1:18192 --compute HTP0 --ngl -1 --nctx $NCTX --keepalive 86400
ExecStartPost=/usr/bin/python3 "$DEPLOY_DIR/llm/backend.py" probe --nctx $NCTX
Restart=no
TimeoutStartSec=180
TimeoutStopSec=45

[Install]
WantedBy=default.target
EOF
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

systemctl --user daemon-reload
if (( LOCAL_COUNTER )); then
    systemctl --user enable qwen-ha-counter.service
    systemctl --user restart qwen-ha-counter.service
fi
if [[ $FULL_STACK == 1 ]]; then
    systemctl --user enable qwen25-3b-backend.service qwen25-whisper-npu.service
    systemctl --user start qwen25-3b-backend.service
    systemctl --user start qwen25-whisper-npu.service
fi
systemctl --user enable qwen-ha-gateway.service
# enable --now does not reload a running service after configuration changes.
systemctl --user restart qwen-ha-gateway.service
sleep 2
systemctl --user is-active --quiet qwen-ha-gateway.service || fail 'Gateway failed to start. Check journalctl --user -u qwen-ha-gateway -n 30.'
echo "Gateway is running. Home Assistant: http://<Arduino-IP>:$GATEWAY_PORT/v1"
echo 'Also run the inference check from the README. Service status alone does not verify model inference.'
echo 'Start automatically without logging in: sudo loginctl enable-linger "$USER"'
