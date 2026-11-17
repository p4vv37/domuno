# Ventuno Voice

**A local, Polish-first voice assistant for Home Assistant on Arduino VENTUNO Q.**

Whisper, Qwen2.5 and Pocket TTS run on one small board. Home Assistant keeps the
conversation, the device catalog and the actual device control. No cloud inference,
no cloud API keys, no GPU machine running 24/7.

<p align="center">
  <img src="docs/images/arduino-ventuno-q.jpg" alt="Arduino VENTUNO Q with a USB conference speaker" width="85%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="Platform: ARM64" src="https://img.shields.io/badge/platform-ARM64-lightgrey.svg">
  <img alt="Status: tested recipe" src="https://img.shields.io/badge/status-tested%20recipe-green.svg">
</p>

## What this is

This repository is the glue between Home Assistant and the local AI runtimes on
Arduino VENTUNO Q:

| Stage | Model | Runs on |
| --- | --- | --- |
| Speech to text | Whisper Large-v3-Turbo W8A16 | Hexagon NPU |
| Conversation and tool calls | Qwen2.5-3B-Instruct Q4_0 via GenieX | Hexagon NPU |
| Text to speech | Pocket TTS Polish (6L, Rust runtime) | CPU |
| Wake word, microphone, playback | Linux Voice Assistant 1.1.18 | CPU / USB audio |

Home Assistant orchestrates the pipeline: it sends audio to STT, sends the text to
the OpenAI-compatible gateway, executes the returned tool calls, and sends the
answer to TTS. The gateway only *proposes* tool calls. It never touches a device.

## How it works

<p align="center">
  <img src="docs/images/architecture.svg" alt="Home Assistant, LVA, Whisper, the Qwen gateway and Pocket TTS on one board" width="90%">
</p>

1. LVA detects “Okay Nabu” and streams the recording to Home Assistant.
2. HA calls Whisper through Wyoming and gets the transcript.
3. HA sends the transcript, the exposed entities and the tool schemas to the
   gateway. The router handles a few simple commands locally; everything else
   goes to Qwen with the original request.
4. HA executes the returned tool call. If HA starts another round, the gateway
   forwards the tool result back to the model.
5. HA sends the final sentence to Pocket TTS; LVA plays the audio.

The router handles on/off for lights, switches, fans and input booleans,
open/close for covers, and absolute cover positions. Negation, conditions,
multiple actions and unsupported parameters go to the LLM. Details:
[docs/router.md](docs/router.md).

The gateway never caches answers and never replays actions. It may reuse a token
prefix through GenieX; that is a runtime optimization, not an application cache.

## Tested configuration

| Component | Version / model |
| --- | --- |
| Board | Arduino VENTUNO Q, Qualcomm Dragonwing IQ-8275, 16 GB RAM |
| Host | Ubuntu ARM64 with Qualcomm NPU drivers and QAIRT 2.50 HTP libraries |
| LLM runtime | GenieX CLI **0.11.0** (0.9.3 only with `CACHE_POLICY=reset`) |
| LLM | `bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0` |
| STT | Whisper Large-v3-Turbo W8A16, ONNX Runtime 1.30.0 + `onnxruntime-qnn` 2.6.0 |
| TTS | `shefowl/pocket-tts-polish-6l` with the Rust ARM64 runtime |
| Satellite | Linux Voice Assistant 1.1.18, wake word `okay_nabu` |
| HA integration | [Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm) 1.15.1 via HACS |
| Audio | USB conference speaker (microphone and speaker in one device) |

## Results on the test board

These are individual observations from the tested stack, not a benchmark suite or
a promise for your hardware.

| Measurement | Result |
| --- | --- |
| Whisper Turbo: 2.83 s of speech | **~0.43 s** transcription |
| Router path, no LLM | **~0.9 ms** decision; **~2.6 ms** including gateway handling |
| TTS first PCM, warm Rust process | **0.79–0.96 s** |
| LLM fixed-sentence test, 3550-token context | median **9.97 s** |
| KV cache, first request (empty prefix) | prefill **6823 ms**; total **7035 ms** |
| KV cache, cached request | prefill **153 ms**; total **294 ms** (3156 tokens reused) |
| Cold cache: first real HA request | total **~9.30 s**; prefill **7.31 s** |
| Warm cache: next real HA request | total **~3.73 s**; prefill **0.16 s** |
| Warm cache: LLM tool call | total **~3.86 s**; prefill **0.16 s** |

Single observations on the test board, not a benchmark. **Cold** means an empty
KV prefix with Qwen already loaded; **warm** means the prefix was reused. Cold
boot, including model loading, takes 12–15 s. Details:
[docs/performance.md](docs/performance.md).

## Requirements

The repository does **not** ship the board's Linux image, Qualcomm drivers or
model weights. You need:

| Dependency | How to get it |
| --- | --- |
| Arduino VENTUNO Q with Ubuntu ARM64 and NPU drivers | Board system image / BSP |
| QAIRT 2.50 HTP libraries (`libQnnHtp.so`) | Board image or GenieX installation |
| Docker Engine + Compose plugin | Board image or [Docker docs](https://docs.docker.com/engine/install/ubuntu/) |
| Python 3.10+, `git`, `curl`, `libatomic1`, `libglib2.0-0` | Ubuntu packages |
| [`uv`](https://docs.astral.sh/uv/) | Official installer |
| GenieX 0.11.0 | [Vendor instructions](https://geniex.aihub.qualcomm.com/en/run/cli/install) |
| Home Assistant + HACS + Local OpenAI LLM | Separate HA instance on the LAN |
| PulseAudio or PipeWire-Pulse user session | Existing board user session |

Home Assistant and its integrations are configured separately. The scripts check
the host prerequisites; they do not replace drivers or install another audio
server.

## Quick start

### 1. Prepare the board

```bash
sudo apt update
sudo apt install -y curl git python3 libatomic1 libglib2.0-0
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh
sh /tmp/install-uv.sh
export PATH="$HOME/.local/bin:$PATH"
```

Install GenieX 0.11.0 using the vendor instructions. On a prepared board image:

```bash
curl -fsSL https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-geniex/install.sh \
  -o /tmp/install-geniex.sh
sh /tmp/install-geniex.sh --version v0.11.0
geniex --skip-update --version   # expect 0.11.0
docker ps
systemctl --user show-environment
```

The drivers must come from the board's system image/BSP. Installing GenieX alone
does not install the NPU drivers.

### 2. Clone and configure

```bash
git clone https://github.com/p4vv37/ha-local-agent.git
cd ha-local-agent
cp .env.example .env
chmod 600 .env
```

Set at least:

| Variable | Value |
| --- | --- |
| `GGUF` | Full path to the Qwen Q4_0 file reported by `geniex pull` |
| `QNN_LIB_DIR` | Directory containing `libQnnHtp.so` |
| `LVA_USER_ID`, `LVA_USER_GROUP` | `id -u`, `id -g` |
| `AUDIO_INPUT_DEVICE`, `AUDIO_OUTPUT_DEVICE` | Your microphone and speaker |

Then download the tokenizer, Whisper artifact and TTS assets:

```bash
./run.sh prepare
```

`prepare` does not run inference. For `STT_PROVIDER=external` or
`TTS_PROVIDER=external`, it skips that stage.

### 3. Check and start

```bash
./run.sh check
./run.sh start
./run.sh status
```

`check` prints `OK` or `MISSING` for tools, model files, NPU devices, HTP
libraries, the audio socket, the prompt counter and the GenieX version. `start`
refuses to continue while anything required is missing.

The stack starts in this order: the prompt counter (when it is local), Qwen
(one guarded cold load and a short control inference), Whisper, the gateway,
then Pocket TTS and LVA. The gateway warms up the router before binding its port;
systemd marks it ready only after that.

### 4. Add the integrations in Home Assistant

Home Assistant needs three integrations:

| Purpose | Integration | Where it comes from |
| --- | --- | --- |
| STT and TTS | **Wyoming Protocol** | Built into Home Assistant |
| LVA satellite | **ESPHome** | Built into Home Assistant |
| Conversation and tools | **Local OpenAI LLM** | Custom integration via HACS |

Only the conversation agent needs HACS. It must support a custom
OpenAI-compatible `/v1/chat/completions` endpoint, tool schemas and streamed
responses. The official **OpenAI** integration only supports the official OpenAI
endpoint, and **Ollama** expects an Ollama server. The tested integration is
[Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm) 1.15.1;
another generic OpenAI-compatible integration may work, but is not tested.

1. Install [HACS](https://hacs.xyz/docs/use/) if needed. In HACS → Integrations →
   three-dot menu → **Custom repositories**, add
   `https://github.com/skye-harris/hass_local_openai_llm` as **Integration**,
   download it and restart HA.
2. Settings → Devices & services → **Add Integration** → **Local OpenAI LLM**.
   Server URL: `http://<Arduino-IP>:18191/v1`; API key: empty unless
   `QWEN_GATEWAY_API_KEY` is set; server type: `llama.cpp`; model:
   `bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0`. Enable **Device control** tools.
3. Add **Wyoming Protocol** for `<Arduino-IP>:10301` (STT) and
   `<Arduino-IP>:10201` (TTS).
4. Add **ESPHome** for `<Arduino-IP>:6053`; do not prefix it with `http://`.
5. Create an Assist pipeline in Polish with the Local OpenAI agent, Whisper STT
   and Pocket TTS voice `shefowl`. Enable **Prefer local intents** and assign the
   assistant to the LVA satellite.
6. Expose only the entities you want to control. The **Device control** tool sees
   only exposed entities, and a smaller list means a smaller prompt.

The LVA satellite keeps its own **Okay Nabu** wake word; HA handles the pipeline
after the wake word. The screenshots below show the tested configuration:

<p align="center">
  <img src="docs/images/ha-assistant-config.jpg" alt="Assist pipeline with Polish STT, Local OpenAI agent and Pocket TTS voice shefowl" width="45%">
</p>

Full walkthrough, including the HACS screens, agent options and alternatives:
[docs/home-assistant.md](docs/home-assistant.md).

## Daily use

```bash
./run.sh status
./run.sh stop
./run.sh start
./run.sh stats
journalctl --user -u qwen-ha-gateway -n 30 --no-pager
docker logs --tail 30 linux-voice-assistant
```

To let user services start without logging in:

```bash
sudo loginctl enable-linger "$USER"
```

After a manual full stop, the boot guard blocks another Qwen cold load within the
same boot. Reboot the board instead of deleting the guard file.

## Configuration

The tested defaults are in [`.env.example`](.env.example). The most important
values:

| Variable | Default | Meaning |
| --- | --- | --- |
| `GGUF` | — | Qwen Q4_0 GGUF path |
| `QNN_LIB_DIR` | — | QAIRT 2.50 HTP library directory |
| `NCTX` | `10240` | Model context size |
| `AGENT_LANGUAGE` | `pl` | Router language: `pl` or `en` |
| `STT_PROVIDER` | `whisper-qnn` | `whisper-qnn` or `external` |
| `TTS_PROVIDER` | `pocket-pl` | `pocket-pl` or `external` |
| `CACHE_POLICY` | `reuse` | `reuse` on GenieX 0.11.0; `reset` for 0.9.3 |
| `RESPONSE_STYLE` | `concise` | Short voice answers; `original` for A/B |
| `DECIDE_APPLY` | `1` | Let the router answer recognized commands |
| `QWEN_GATEWAY_API_KEY` | empty | Optional bearer token |

Every variable, including diagnostics and advanced installer settings, is
documented in [docs/configuration.md](docs/configuration.md).

The gateway always needs a **prompt counter**. With `GGUF` + `TOKENIZER` the
installer creates a local CPU counter; otherwise set `COUNTER_URL` to an existing
loopback counter. The gateway refuses to start without it.

Local Whisper and Pocket TTS are Polish-only. For an English router with speech
handled elsewhere, set:

```dotenv
AGENT_LANGUAGE=en
STT_PROVIDER=external
TTS_PROVIDER=external
```

and select English STT/TTS in Home Assistant. The full-stack script rejects
English with a local speech recipe rather than pretending it is supported.

## Limitations

- **Qwen2.5-3B is small.** It handles short commands and tool selection well, but
  it is not a multi-step planner. Complex requests can produce invented arguments.
- **Full context is expensive.** A large HA catalog plus tool schemas can take
  about 20 s of prefill. Exposing fewer entities and keeping the KV prefix stable
  matter more than prompt wording.
- **Local speech is Polish.** The router has English grammar, but the shipped
  Whisper/Pocket TTS recipes are Polish. English needs external speech services.
- **One NPU is shared.** Whisper and Qwen must not cold-load at the same time.
  There is no CPU fallback. FastRPC out-of-memory requires a reboot, not a retry.
- **One tested board image.** Other ARM64 boards, QAIRT versions or GenieX
  releases are unverified. Replacing the LLM or STT artifact is a separate
  compatibility exercise.
- **Not a Home Assistant add-on.** HA and the board are separate services on the
  LAN. The gateway is not hardened for the public Internet.

## Troubleshooting

| Symptom | First thing to check |
| --- | --- |
| `./run.sh check` prints `MISSING` | Fix the named item; `check` never installs anything |
| Gateway will not start: `prompt counter is required` | Set `GGUF` + `TOKENIZER`, or `COUNTER_URL` |
| Qwen will not load, or FastRPC `ENOMEM` | Stop Whisper, reboot; the guard allows one load per boot |
| HA cannot reach STT/LLM/TTS | `curl` the gateway `/healthz`; check ports and `GATEWAY_HOST` |
| Gateway returns 413 / 503 | 413: fewer entities or smaller prompt. 503: counter is down |
| No sound or no waiting cue | `docker logs tts-pocket-pl`; `./run.sh sound-test` |
| A simple command goes to the LLM | Expected fail-open: ambiguous target or unsupported grammar |

More detail, log locations and safe reporting: [docs/troubleshooting.md](docs/troubleshooting.md).

## Privacy and security

- Inference is local. The repository does not send prompts, audio or transcripts
  to a cloud service.
- The gateway listens on the LAN and is **unauthenticated by default**, matching
  the local-only design. Set `QWEN_GATEWAY_API_KEY` in `.env` and in HA if your
  network is not trusted. Never port-forward it to the Internet.
- Audio is not saved. Raw requests, router transcripts and request captures are
  off by default or explicitly opt-in, stored under `logs/` with private
  permissions, and ignored by git. They contain home and conversation data: keep
  them local.
- The default metrics records contain no prompts, entity names, transcripts or
  tool arguments. They are diagnostic timing data, not anonymous telemetry. A
  metadata-only cache audit is on by default; set `CONTEXT_AUDIT=0` to disable it.
- The HA integration is third-party. Review it before exposing sensitive
  entities.

## Development

Run the offline tests without a board, models or network:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Use `.venv/bin/python` to exercise the installed `simplemma`. Number
normalization tests require `num2words==0.5.14`; the TTS image includes it.
Protocol tests use fake models. The NPU/ARM64 paths still need the target board.
See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR; hardware-specific
results need to state what was actually tested.

Code layout:

```text
run.sh / .env.example   public entry point and configuration
setup.sh                internal installer (systemd units + uv venv)
llm/backend.py          GenieX version check, boot guard, readiness
stt/                    QNN Whisper engine, profile validation, Wyoming server
gateway/                OpenAI proxy, prompt budget, router, metrics
tts-pocket-wyoming/     Rust TTS adapter, text streaming, PCM delivery
lva/                    Linux Voice Assistant compose configuration
tests/                  offline unit and integration-style tests
docs/                   configuration, operations, router, speech, performance
```

Start reading at `gateway/qwen_npu_gateway.py`, then
`gateway/qwen_context_catalog.py` (HA overview parsing) and
`gateway/qwen_decision.py` (routing and tool validation). For audio, start with
the Wyoming handlers, then `stt/qnn_whisper_engine.py` or
`tts-pocket-wyoming/rust_tts_backend.py`.

## Documentation

- [Configuration](docs/configuration.md) — every `.env` variable and how to apply it
- [Operations](docs/operations.md) — startup, KV cache, updates, metrics, private logs
- [Router](docs/router.md) — grammar, vocabulary, fallback and cover positions
- [Home Assistant](docs/home-assistant.md) — integrations, HACS, Assist pipeline and screenshots
- [Performance](docs/performance.md) — measured numbers and caveats
- [Troubleshooting](docs/troubleshooting.md) — common failures and safe diagnostics

## Credits

This project stands on other people's work:

- [Home Assistant](https://www.home-assistant.io/) — conversation pipeline and device control
- [Linux Voice Assistant](https://github.com/OHF-Voice/linux-voice-assistant) — satellite and wake word
- [GenieX](https://geniex.aihub.qualcomm.com/) and [Qwen2.5](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct) — local LLM runtime and model
- [Whisper Large-v3-Turbo](https://huggingface.co/openai/whisper-large-v3-turbo) and [Qualcomm AI Hub](https://aihub.qualcomm.com/) — STT artifact
- [Pocket TTS](https://github.com/kyutai-labs/pocket-tts) and [`shefowl/pocket-tts-polish-6l`](https://huggingface.co/shefowl/pocket-tts-polish-6l) — TTS model and Rust runtime
- [Pocket TTS Wyoming](https://github.com/ikidd/pocket-tts-wyoming) — the original Wyoming adapter this server was adapted from
- [Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm) — HA integration
- [simplemma](https://github.com/adbar/simplemma), [polish-inflection](https://pypi.org/project/polish-inflection/), [num2words](https://github.com/savoirfairelinux/num2words) — router linguistics

The build and measurements are described in the article
[Local voice assistant on Arduino VENTUNO Q](https://pkowalski.com/arduino-local-agent/)
(in Polish).

## License

MIT. See [LICENSE](LICENSE). The bundled/downloaded models and runtimes keep
their own licenses; check them before redistributing assets.

This is an independent project, not affiliated with Arduino, Qualcomm, Home
Assistant or the Qwen project. Product names belong to their respective owners.
