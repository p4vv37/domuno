# Configuration

Configure the stack in `.env`:

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
| `AGENT_LANGUAGE` | `pl` | Router and local speech language: `pl`, `en` or `es`. Selects the Pocket TTS model and the Whisper decoder language, not the Qwen or Whisper weights. |
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
  `transformers` package. Set `TOKENIZER` in `.env` to the absolute path of a
  local directory containing `tokenizer.json` for the selected GGUF. When
  `TOKENIZER` is empty, `./run.sh` uses `assets/qwen-tokenizer` and `prepare`
  downloads the bundled recipe's tokenizer there. With an explicit path,
  `prepare` checks for `tokenizer.json` without downloading into that directory.
- External: set `COUNTER_URL` to an existing loopback counter. This is mutually
  exclusive with local `GGUF` + `TOKENIZER`.
- Neither: `setup.sh` fails, and the gateway CLI refuses to start.

Before inference the gateway checks:

```text
prompt_upper_bound + max_completion_tokens + 64 > NCTX  → HTTP 413
counter unavailable                                      → HTTP 503
```

Both fail before the backend is contacted. Keep this check with GenieX 0.11.0:
with `NCTX=10240`, a synthetic HA tool-result prompt over the context limit
aborted the backend (`SIGABRT`) instead of returning an HTTP error. A subsequent
short request could not connect.

The estimate includes a 1024-token reserve plus per-message and per-tool
reserves; it is not a certified match for native token counting.

The router is optional and fail-open. `DECIDE_APPLY=1` only changes commands it
can resolve to exactly one entity and one supported tool; all other requests
reach the LLM unchanged. Router-handled commands do not contact the backend, so
they do not consume the prompt budget. The grammar, vocabulary format and fallback rules are in
[router.md](router.md).

## Speech providers

| Variable | Default | Meaning |
| --- | --- | --- |
| `STT_PROVIDER` | `whisper-qnn` | `whisper-qnn` runs the local Whisper Large-v3-Turbo adapter; `external` means HA manages STT elsewhere. |
| `STT_PROMPT` | empty | Optional static decoder prompt for local Whisper. Empty or unset means no prompt. |
| `TTS_PROVIDER` | `pocket` | `pocket` runs local Pocket TTS for `AGENT_LANGUAGE`; `external` means HA manages TTS elsewhere. `pocket-pl` remains a legacy alias for `pocket`. |

Keep `STT_PROMPT` empty unless you are deliberately testing decoder bias. A
nonempty value is not derived from the HA catalog and can cause empty transcripts
on this quantized decoder.

Each selection is independent, so a pipeline in any language can combine one local and one
external stage. `external` means “managed outside these scripts”, not necessarily
cloud-hosted.

For a local stage, `prepare` downloads its assets, `check` verifies them, and
`start`/`stop` build or manage its container/unit. For `external`, that stage is
skipped completely. Changing the setting does not remove an already installed
unit or container; old deployments remain under their current owner's control.

Local Whisper loads the same multilingual artifact for all three languages and
advertises the configured language; it does not autodetect language. Pocket TTS
uses a language-specific recipe. Match both services to your Assist pipeline.
Native English and Spanish synthesis and end-to-end board validation are pending.

### Preparing local Pocket TTS

1. Set `TTS_PROVIDER=pocket` and `AGENT_LANGUAGE=pl`, `en` or `es` in `.env`.
2. Run `./run.sh prepare` to download the selected assets. Polish uses the public
   [Polish fine-tune](https://huggingface.co/shefowl/pocket-tts-polish-6l).
   English and Spanish use the public, non-cloning
   [official Kyutai weights](https://huggingface.co/kyutai/pocket-tts) and preset
   voices. The bundled recipes do not need a Hugging Face account or `HF_TOKEN`.
3. Run `./run.sh check`, then start the stack or follow the
   [migration steps](operations.md#speech-migration-checks) for an existing deployment.

Preparation needs Internet access for downloads. After preparation and image
setup, Pocket TTS loads local files and synthesizes offline.

Pocket TTS accepts one complete text request at a time, up to 4,000 characters
before and after normalization. Concurrent requests receive `busy`; streamed
text is not supported, but PCM output is streamed.

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
| `ROUTER_VOCABULARY` | empty | Absolute path to a complete replacement for the selected language's bundled vocabulary |
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
| `GATEWAY_API_KEY` | empty | Optional bearer token; set the same value in the HA integration |

Existing deployments may still use `QWEN_GATEWAY_API_KEY`. The gateway uses it
when `GATEWAY_API_KEY` is unset or empty. To migrate, rename the variable in
`.env` without changing its value, then restart the gateway.

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
| `FULL_STACK` | `0` | `1` installs/starts the LLM backend and selected STT unit; `0` updates only the gateway and its local counter, if configured |

Changing `MODEL` alone does not switch models. `./run.sh prepare` downloads the
shipped Qwen recipe. A different model needs a compatible backend and a prompt
counter using the same model's GGUF metadata, tokenizer and chat template.
Update the model ID in Home Assistant as well.

## Applying changes

| Change | What to do |
| --- | --- |
| `ROUTER_MISS_LOGGING`, `ROUTER_MISS_UNTIL`, `ROUTER_VOCABULARY`, API key, metric labels | Restart only `qwen-ha-gateway.service` |
| `AGENT_LANGUAGE` | Prepare the selected local assets, restart the gateway (install dependencies if needed), recreate the enabled local speech containers and match the language and voice in HA. |
| `STT_PROMPT` | Recreate the local Whisper container so it receives the changed environment |
| `CONTEXT_AUDIT`, `CONTEXT_CAPTURE`, `CONTEXT_CAPTURE_LIMIT` | Restart only the gateway |
| `CACHE_POLICY`, `RESPONSE_STYLE`, `DECIDE_APPLY`, listener/counter settings | Re-run the gateway-only installer to regenerate its unit |
| `STT_PROVIDER`, `TTS_PROVIDER` | Apply during a planned stack installation; external stages remain separately managed |
| Qwen model/context/runtime settings | Update the backend separately and perform a controlled cold boot |

For the gateway-only update command, generated unit list and image rebuild
steps, see [Operations](operations.md#updating-an-existing-deployment).

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
