# Configuration

Everything user-facing is configured in one `.env` file:

```bash
cp .env.example .env
chmod 600 .env
```

`./run.sh` sources `.env`, validates the selected profile and passes it to
`setup.sh`. The generated systemd unit also reads `.env` through
`EnvironmentFile=`, so runtime switches can be changed without regenerating the
unit. Paths with spaces are supported; systemd-unit values reject `"`, `$`, `%`,
`\` and newlines.

## Required paths

| Variable | Default | Meaning |
| --- | --- | --- |
| `GGUF` | empty | Full path to the Qwen2.5-3B-Instruct Q4_0 GGUF file reported by `geniex pull` |
| `QNN_LIB_DIR` | empty | Host directory containing QAIRT 2.50 HTP libraries, including `libQnnHtp.so`; mounted read-only into STT |
| `GENIEX_BIN` | `~/.local/bin/geniex` | GenieX CLI path; only needed when it is not on `PATH` |

`GGUF` and `QNN_LIB_DIR` are checked by `./run.sh check`. The repository does not
contain the board image, the drivers or the model weights.

## Model and router

| Variable | Default | Meaning |
| --- | --- | --- |
| `NCTX` | `10240` | Model context size. Allowed range: 2048–10240. |
| `AGENT_LANGUAGE` | `pl` | Router language: `pl` or `en`. Does not change the speech models. |
| `DECIDE_APPLY` | `1` | `1` lets the router answer recognized commands locally; `0` sends everything to Qwen. |
| `RESPONSE_STYLE` | `concise` | `concise` prepends one stable short-answer instruction; `original` leaves the HA prompt unchanged. |
| `CACHE_POLICY` | `reuse` | `reuse` keeps the KV prefix on GenieX 0.11.0; `reset` inserts the legacy reset turn, required for GenieX 0.9.3. |

`NCTX` must fit the board's memory. The default 10240 is what the tested HA
context needed; a smaller context means fewer exposed entities or shorter tool
schemas.

### Prompt counter

The gateway **always** requires a prompt counter. It runs on the CPU, renders the
Qwen chat template with the actual tool schemas and messages, and returns a
conservative token upper bound. It is not an LLM and does not use the NPU.

- Local: `GGUF` + `TOKENIZER` make `setup.sh` create
  `qwen-ha-counter.service` on `127.0.0.1:18193`. It installs the pinned
  `transformers` package.
- External: set `COUNTER_URL` to an existing loopback counter. This is mutually
  exclusive with local `GGUF` + `TOKENIZER`.
- Neither: `setup.sh` fails, and the gateway CLI refuses to start.

Before inference the gateway checks:

```text
prompt_upper_bound + max_completion_tokens + 64 > NCTX  → HTTP 413
counter unavailable                                      → HTTP 503
```

Both fail before the backend is contacted. The estimate includes a 1024-token
reserve plus per-message and per-tool reserves; it is not a certified native
tokenizer parity.

The router is optional and fail-open. `DECIDE_APPLY=1` only changes commands it
can resolve to exactly one entity and one supported tool; all other requests
reach the LLM unchanged. Router-handled commands do not contact the backend, so
they do not consume the prompt budget. The grammar, vocabulary format and fallback rules are in
[router.md](router.md).

## Speech providers

| Variable | Default | Meaning |
| --- | --- | --- |
| `STT_PROVIDER` | `whisper-qnn` | `whisper-qnn` runs the local Whisper Large-v3-Turbo adapter; `external` means HA manages STT elsewhere. |
| `TTS_PROVIDER` | `pocket-pl` | `pocket-pl` runs the local Polish Pocket TTS server; `external` means HA manages TTS elsewhere. |

Each selection is independent, so a Polish pipeline can combine one local and one
external stage. `external` means “managed outside these scripts”, not necessarily
cloud-hosted.

For a local stage, `prepare` downloads its assets, `check` verifies them, and
`start`/`stop` build or manage its container/unit. For `external`, that stage is
skipped completely. Changing the setting does not remove an already installed
unit or container; old deployments remain under their current owner's control.

The local Whisper adapter explicitly selects Polish, and `pocket-pl` is a Polish
checkpoint with Polish number normalization. The full-stack scripts therefore
reject English with either local recipe selected. For an English router, use:

```dotenv
AGENT_LANGUAGE=en
STT_PROVIDER=external
TTS_PROVIDER=external
```

and select English STT/TTS in the HA Assist pipeline. No English speech model is
downloaded by this repository.

## Audio and LVA

| Variable | Default | Meaning |
| --- | --- | --- |
| `LVA_USER_ID`, `LVA_USER_GROUP` | `1000` | `id -u` / `id -g` on the board; used for file ownership and the PulseAudio socket |
| `AUDIO_INPUT_DEVICE`, `AUDIO_OUTPUT_DEVICE` | `default`, `pulse` | LVA microphone/speaker selectors |
| `ENABLE_THINKING_SOUND` | `0` | `1` enables LVA's processing cue at startup; `0` leaves the HA/LVA preference unchanged |
| `PROCESSING_SOUND` | empty | Optional audio file **inside** the LVA container; empty uses LVA's bundled sound |
| `LIST_DEVICES` | empty | Set to `1` temporarily to list audio devices instead of normal LVA startup |

Audio uses PulseAudio or PipeWire-Pulse. The default socket is
`/run/user/<LVA_USER_ID>/pulse/native`. If `LIST_DEVICES=1`, start LVA normally
and read its logs, then restore the empty value.

## Optional waiting cue

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_WAIT_SOUND` | `0` | `1` plays one short cue immediately before a real LLM request. Router actions, ACKs and clarifications stay silent. |
| `LLM_WAIT_SOUND_VOLUME` | `lva` | `lva` follows LVA's output volume/mute; `0`–`100` uses a fixed PulseAudio level |
| `LVA_VOLUME_URL` | `ws://127.0.0.1:6055` | LVA peripheral WebSocket used in `lva` volume mode |
| `LLM_WAIT_SOUND_FILE` | empty | Empty generates a 180 ms cue; otherwise an absolute host WAV path |

`prepare` installs `paplay` (`pulseaudio-utils`) only when the cue is enabled and
the client is missing. In `lva` mode it also installs the pinned
`websockets==15.0.1` client into `.venv`. For an existing deployment:

```bash
./run.sh prepare-audio
./run.sh sound-test
systemctl --user restart qwen-ha-gateway.service
```

The cue is best-effort: a playback failure logs a warning and never fails or
retries inference. It adds `wait_sound_started` and `wait_sound_ms` to metrics
but does not change prompts, history, KV reuse or response bytes.

`ENABLE_THINKING_SOUND` is an LVA-level alternative that fires for every HA
intent. Do not enable both options for the same pipeline unless two cues are
acceptable.

## Private diagnostics

These files are written under `logs/` with `0700` directories, `0600` files, a
three-file rotation and a 5 MiB limit per file. They are ignored by git.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ROUTER_MISS_LOGGING` | `0` | `1` records the latest user utterance (up to 512 chars), routing reason and metadata for requests the router passes toward the LLM, before the budget gate. Never publish it. |
| `ROUTER_MISS_UNTIL` | empty | Optional fixed ISO 8601 cutoff with timezone, e.g. `2026-11-24T00:00:00Z`; empty collects until disabled |
| `ROUTER_VOCABULARY` | empty | Absolute path to a complete replacement for `router_pl.json` / `router_en.json` |
| `CONTEXT_AUDIT` | `1` | Metadata and SHA-256 fingerprints of the normalized request, without names or prompts |
| `CONTEXT_CAPTURE` | `0` | Full normalized requests, including private home data; explicit opt-in |
| `CONTEXT_CAPTURE_LIMIT` | `5` | `1`–`20` incoming requests per gateway process |

Changing these switches requires only a gateway restart:

```bash
systemctl --user restart qwen-ha-gateway.service
```

The installed unit provides fallback values for the environment variables that
control private logs. This matters if an older `.env` does not mention them: an
explicit directory argument must not silently enable transcript capture.

## Gateway access

| Variable | Default | Meaning |
| --- | --- | --- |
| `QWEN_GATEWAY_API_KEY` | empty | Optional bearer token; set the same value in the HA integration |

The gateway listens on the LAN and is unauthenticated by default. This matches
the trusted-LAN deployment. Never expose port `18191` to the public Internet.
`GET /healthz` returns `inference_verified: false`; it proves the HTTP process is
alive, not that the model can infer. `/v1/models` is available for integration
discovery.

## Metrics labels

| Variable | Default | Meaning |
| --- | --- | --- |
| `METRICS_BACKEND_VERSION` | `0.11.0` | Operator-supplied label; verify with `geniex --skip-update --version` |
| `METRICS_PROFILE` | `baseline` | Free-form comparison label; change it when the model, context, tools or workload changes |

Metrics are payload-free and always written by the installed gateway. Labels are
not verified automatically. Keep them honest when comparing runs.

## Installer-level variables

`setup.sh` also reads these when invoked directly. `./run.sh` fills in the ones it
needs and should be preferred.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL` | `bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0` | Model id exposed by the gateway and sent to GenieX |
| `BACKEND_URL` | `http://127.0.0.1:18192` | GenieX/llama.cpp-compatible backend |
| `GATEWAY_HOST` | `0.0.0.0` | Gateway listener; use `127.0.0.1` to keep it on the board |
| `GATEWAY_PORT` | `18191` | Gateway port |
| `COUNTER_PORT` | `18193` | Local prompt counter port |
| `COUNTER_URL` | derived | External loopback counter URL; required when not using local `GGUF` + `TOKENIZER` |
| `TOKENIZER` | empty | Local Qwen tokenizer directory for the counter, normally `assets/qwen-tokenizer` |
| `FULL_STACK` | `0` | `1` installs/starts Qwen and Whisper units; `0` is a gateway-only update |

## Applying changes

| Change | What to do |
| --- | --- |
| `ROUTER_MISS_LOGGING`, `ROUTER_MISS_UNTIL`, `ROUTER_VOCABULARY`, API key, metric labels | Restart only `qwen-ha-gateway.service` |
| `AGENT_LANGUAGE` | Restart the gateway; install dependencies if the language is new to this deployment; match the HA pipeline language |
| `CONTEXT_AUDIT`, `CONTEXT_CAPTURE`, `CONTEXT_CAPTURE_LIMIT` | Restart only the gateway |
| `CACHE_POLICY`, `RESPONSE_STYLE`, `DECIDE_APPLY`, listener/counter settings | Re-run the gateway-only installer to regenerate its unit |
| `STT_PROVIDER`, `TTS_PROVIDER` | Apply during a planned stack installation; external stages remain separately managed |
| Qwen model/context/runtime settings | Update the backend separately and perform a controlled cold boot |

Gateway-only update, for an existing working deployment:

```bash
set -a
. ./.env
set +a
FULL_STACK=0 NCTX=10240 GGUF= TOKENIZER= \
  COUNTER_URL=http://127.0.0.1:18193/count ./setup.sh
```

This creates/updates `qwen-ha-gateway.service`, installs its Python dependencies
and restarts only the gateway. It does not start or restart Qwen, Whisper, TTS or
LVA. Do not run `./run.sh start` over an old deployment with different containers
or unit drop-ins.

The generated user units are:

| Unit | Purpose |
| --- | --- |
| `qwen25-3b-backend.service` | GenieX/Qwen backend, one cold load per boot, readiness probe |
| `qwen-ha-counter.service` | CPU prompt counter (only for a local GGUF + tokenizer) |
| `qwen-ha-gateway.service` | OpenAI proxy, router, metrics and diagnostics |
| `qwen25-whisper-npu.service` | Starts Whisper only after Qwen is ready |

TTS (`tts-pocket-pl`) and LVA (`linux-voice-assistant`) use Docker restart
policies.

## Ports

| Port | Service |
| --- | --- |
| `18191` | OpenAI-compatible gateway |
| `18192` | GenieX backend (loopback) |
| `18193` | Prompt counter (loopback) |
| `10301` | Wyoming STT |
| `10201` | Wyoming TTS |
| `6053` | ESPHome/LVA |
| `6055` | LVA peripheral WebSocket (only for `lva` volume sync) |

If you change a port, update both `.env` and the Home Assistant integration.
