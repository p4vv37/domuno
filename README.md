# Local Home Assistant voice assistant on Arduino VENTUNO Q

This repository contains the scripts and tools needed to run a local voice assistant on Arduino VENTUNO Q that communicates with Home Assistant to control your home.
It implements the setup described in [Local voice assistant on Arduino VENTUNO Q](https://pkowalski.com/arduino-local-agent/) (article in Polish).

Whisper Turbo and Qwen2.5-3B run on the NPU, while Polish Pocket TTS runs on the CPU.
LVA handles the microphone, the "Okay Nabu" wake word and audio playback. Home Assistant
manages the conversation and executes actions. It can run on a separate computer.

Configure **one `.env` file** and manage the stack through **`./run.sh`**.
These instructions cover a fresh installation. Do not run it alongside an existing
deployment that uses the same ports and container names.

## 1. Prepare the Arduino

You need Ubuntu ARM64 with the Qualcomm drivers for VENTUNO Q, Docker with Compose,
Python 3.10+, `uv`, a `systemd --user` session and several GB of free disk space.
Downloading models and building images requires internet access.

```bash
sudo apt update
sudo apt install -y curl git python3 libatomic1 libglib2.0-0
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh
sh /tmp/install-uv.sh
export PATH="$HOME/.local/bin:$PATH"
git clone https://github.com/p4vv37/ha-local-agent.git
cd ha-local-agent
```

Install GenieX following the [vendor's instructions](https://geniex.aihub.qualcomm.com/en/run/cli/install).
On a correctly prepared Arduino system image:

```bash
curl -fsSL https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-geniex/install.sh -o /tmp/install-geniex.sh
sh /tmp/install-geniex.sh --version v0.9.3
geniex --version
docker ps
systemctl --user show-environment
```

Verify that GenieX reports version 0.9.3. The usual path is `~/.local/bin/geniex`.
Set `GENIEX_BIN` in `.env` if yours differs. The drivers must come from the system
image/BSP for your board. Installing GenieX alone does not install the NPU drivers.

## 2. Download assets and configure paths

```bash
cp .env.example .env
chmod 600 .env
./run.sh prepare
```

`prepare` downloads Qwen Q4_0, its tokenizer, the Whisper Turbo artifact and the
Polish Pocket TTS model, reference voice and Rust binary. It does not run inference.
Preparation is separate from startup so normal starts do not repeat downloads.

Set these values in `.env`:

| Field | Value |
|---|---|
| `GGUF` | Full path to the Qwen Q4_0 file reported by `geniex pull` |
| `QNN_LIB_DIR` | Directory containing compatible HTP libraries, including `libQnnHtp.so` |
| `LVA_USER_ID`, `LVA_USER_GROUP` | Output of `id -u` and `id -g` |
| `AUDIO_INPUT_DEVICE`, `AUDIO_OUTPUT_DEVICE` | Microphone and speaker devices |

STT requires **QAIRT 2.50.0.260828221209**, **ONNX Runtime 1.30.0** and a compatible
QNN EP plugin. Place the Linux ARM64 / CPython 3.12 `.whl` packages in
`stt/runtime/`, following the [ONNX Runtime/QNN instructions](https://onnxruntime.ai/docs/execution-providers/QNN-ExecutionProvider.html).
Vendor libraries are not included in this repository. HTP libraries may be available
at `~/.local/share/geniex/qairt/htp-files`, but you must verify their version.
This step depends on your system image and access to the vendor packages.

Audio uses PulseAudio or PipeWire-Pulse. The default socket is
`/run/user/<LVA_USER_ID>/pulse/native`. To list audio devices, temporarily set
`LIST_DEVICES=1`, start LVA and check its logs, then restore the empty value.

## 3. Check and start

```bash
./run.sh check
./run.sh start
./run.sh status
```

`check` does not install or start anything. It prints `OK` or `MISSING`
for the required tools, files, NPU environment and audio socket.
`start` will not proceed until all these requirements are available.

`start` builds containers and installs services. It starts them in this order:

1. Qwen, with one loading attempt and a short response check.
2. Whisper, once Qwen has finished starting.
3. The context counter and gateway with the router, using a virtual environment created by `uv`.
4. Pocket TTS and LVA.

Services use the actual repository paths. You do not need to copy or edit systemd
unit files. `setup.sh` is the internal installer called by `run.sh`.
It does not restart an already running Qwen backend. Apply model or context changes
with a separate cold boot of the board, rather than repeated loading attempts.

Add these integrations in HA:

| Integration | Address |
|---|---|
| Wyoming STT | Host: Arduino IP, port `10301` |
| Wyoming TTS | Host: Arduino IP, port `10201`, voice `shefowl`, language `pl` |
| [Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm), through HACS | `http://<Arduino-IP>:18191/v1` |
| ESPHome, for LVA | Host: Arduino IP, port `6053`, without `http://` |

In Local OpenAI, select model `bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0`,
type `llama.cpp` and **Assist / Device control** tools. Expose the entities you want
to control. In the assistant settings, select Polish STT, the conversation agent
and TTS, then assign this assistant to the LVA satellite. Try a neutral question
first, then a device command.

## Daily use

```bash
./run.sh status
./run.sh stop
./run.sh start
journalctl --user -u qwen-ha-gateway -n 30 --no-pager
docker logs --tail 30 linux-voice-assistant
```

After you manually stop the entire stack, the guard blocks another Qwen cold load
within the same boot. Reboot the board in that case. Do not clear the guard's lock
to force repeated attempts after FastRPC ENOMEM.

To start user services without logging in:

```bash
sudo loginctl enable-linger "$USER"
```

The installer enables the Qwen, STT, gateway and counter units. TTS and LVA use
Docker's `unless-stopped` restart policy. Verify a full reboot on your Arduino.

## Code layout

```text
run.sh + .env.example   startup and shared configuration
setup.sh               internal service installation using uv
llm/backend.py         model startup and readiness checks
stt/                   QNN engine, model validation, Wyoming protocol
gateway/               proxy, router, HA catalog, context counter
tts-pocket-wyoming/    Rust adapter, protocol and PCM streaming
lva/                   configuration for the existing satellite
tests/                 offline tests
```

The router handles simple Polish on/off and open/close commands.
It uses current HA names and schemas, tolerates minor STT errors and asks for
clarification when a command is ambiguous. Complex commands go to Qwen.
Set `DECIDE_APPLY=0` in `.env` to disable the router. The default context size of
10240 uses a counter with a conservative margin. Requests exceeding the budget
receive HTTP 413, and an unavailable counter produces HTTP 503 before inference.
The estimate is not a certified equivalent of the native tokenizer.

By default, the gateway runs without an API key on a trusted LAN. To enable
optional authentication, set `QWEN_GATEWAY_API_KEY` in `.env` and in the HA integration.
No cloud API key is required. Raw audio is not saved. Git ignores `.env`, models,
vendor packages and logs. The code uses models and runtimes from their respective
authors. The TTS server was adapted from
[Pocket TTS Wyoming](https://github.com/ikidd/pocket-tts-wyoming), and the satellite
comes from [Linux Voice Assistant](https://github.com/OHF-Voice/linux-voice-assistant).

Before synthesis, `num2words` converts standalone numbers to Polish words.
Decimal digits retain their zeros, and percentages and Celsius temperatures are
expanded for speech. Dates, times, URLs and identifiers are left unchanged.
Normalization runs before sentence splitting and the final text length check.

Run tests without downloading anything or starting services:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Use `.venv/bin/python` instead of `python3` to also test the installed `simplemma`.
Number normalization tests require `num2words==0.5.14`, included in the TTS image.
Protocol tests use fake models. New ARM64 images and their NPU integration need
to be tested on the target board.
