# Operations

This page covers the day-2 behavior: startup order, the Qwen boot guard, KV cache,
metrics, private logs and upgrades. For the initial installation see the
[README quick start](../README.md#quick-start).

## Startup order

`./run.sh start` performs these steps:

1. `./run.sh check` must pass; it does not install or start anything.
2. Build and create the Whisper container when local STT is selected.
3. Build the Pocket TTS image when local TTS is selected.
4. Run `FULL_STACK=1 ./setup.sh`, which installs the user units. The local prompt
   counter (when configured) starts first, then Qwen.
5. Start Whisper only after Qwen's readiness probe, then the gateway.
6. Start Pocket TTS and LVA.

The Qwen backend unit runs:

```text
llm/backend.py guard          # one startup attempt per boot
geniex ... serve              # load Qwen on HTP0
llm/backend.py probe          # one short real inference
```

`Restart=no` on the backend is deliberate. If the cold load fails, the stack does
not loop and exhaust memory. The guard records
`~/.local/state/ha-local-agent/attempt-<boot_id>` and refuses a second attempt in
the same boot. It also refuses to start while Whisper or another `geniex` process
is running, because both models share the NPU.

After a failed cold boot:

```bash
journalctl --user -u qwen25-3b-backend -n 80 --no-pager
docker ps
```

If FastRPC reported `ENOMEM`, stop Whisper/other NPU clients and **reboot**. Do
not delete the guard file to force a retry. Repeated failed loads are not a
cache test and can leave the NPU in a worse state.

Whisper's unit is a oneshot wrapper around `docker start whisper-turbo-qnn`.
Failure to become healthy is terminal; there is no CPU fallback and no automatic
retry. The gateway also refuses to start without a prompt counter; `setup.sh`
rejects a configuration with neither local `GGUF` + `TOKENIZER` nor `COUNTER_URL`.

## First request vs. warm request

A cold boot and a loaded model are different measurements.

- The backend probe proves that inference works, but it does not populate the
  real HA conversation prefix.
- The first HA request still evaluates the system prompt, entity catalog and tool
  schemas. On the test board one request needed 7.31 s of prefill for about 3340
  tokens.
- Later requests reused over 3300 tokens and needed about 0.16 s of prefill.
- Any earlier-token change, another client on the same backend or a backend
  restart reduces what can be reused.

`keepalive` keeps the model weights loaded; `cached_tokens` measures prefix reuse.
Those are separate things.

## KV cache policy

`CACHE_POLICY=reuse` is the default and requires GenieX 0.11.0. The gateway sends
one real request and leaves token-prefix reuse to GenieX. It does not cache
answers, rewrite history or replay actions.

`CACHE_POLICY=reset` inserts a small tool-free turn before each backend request.
It discards the previous long prefix without unloading weights and remains the
workaround for the empty exact-repeat response on GenieX 0.9.3. It is not a native
KV-memory-clear API, and a common prefix may still be reused. Use it for
compatibility or A/B tests, not as a performance feature.

Changing the policy requires a gateway unit regeneration (`setup.sh`), not a model
reload. The version check accepts 0.11.0, or 0.9.3 only with `reset`. An external
backend cannot be version-checked by the installer, so keep
`METRICS_BACKEND_VERSION` honest.

Place stable content early in the HA prompt where possible: instructions and
schemas before changing entity state. The gateway itself does not reorder tools,
messages, schemas or catalog rows.

## Metrics

The installed gateway always writes payload-free request metrics to:

```text
logs/metrics/metrics.jsonl
```

Rotation keeps the active file and two backups, up to 5 MiB each. Records include
a request id, run id, model, runtime, context size, cache policy, route, outcome,
status and available timings. They do not include prompts, transcripts, entity
names, tool names, tool arguments or credentials.

Summarize locally:

```bash
./run.sh stats
python3 gateway/qwen_latency_stats.py --json logs/metrics/metrics.jsonl*
```

The report groups runs by route, backend version label, profile, cache policy and
router language. It prints outcome counts and median/p95 with sample counts.
Empty, incomplete, failed and unverified responses are excluded from completed
latency statistics. p95 uses the nearest-rank method; small samples remain small.

Timing fields:

| Field | Meaning |
| --- | --- |
| `router_ms` | Rule-router decision time |
| `budget_ms` | Prompt-counter request time |
| `queue_ms` | Time waiting for the single inference slot |
| `reset_ms` | Legacy reset turn |
| `wait_sound_ms` | Launch overhead of the optional cue |
| `backend_ms` | Time from backend request to response completion, including client backpressure |
| `ttft_ms` | Streaming time to the first text/tool argument delta; absent for non-streaming JSON |
| `prefill_ms`, `decode_ms` | Backend-reported timings, when available |
| `estimated_prompt_tokens` | Conservative counter bound, not native usage |
| `cached_tokens` | Backend-reported prefix reuse |

STT and TTS emit their own payload-free lifecycle records through Docker logs:

```bash
docker logs whisper-turbo-qnn > /tmp/stt.log 2>&1
docker logs tts-pocket-pl > /tmp/tts.log 2>&1
python3 gateway/qwen_latency_stats.py /tmp/stt.log /tmp/tts.log
```

Audio records have service-local request ids and are not correlated with gateway
ids. First PCM delivery is not speaker playback. Raw container logs can contain
ordinary diagnostic text; keep exports private.

## Context audit

`CONTEXT_AUDIT=1` writes metadata and SHA-256 fingerprints to:

```text
logs/context-audit/context-audit.jsonl
```

It is computed after normalization and the optional concise style, before routing
and budget checks. Router-handled and rejected requests are included.

| Field | Meaning |
| --- | --- |
| `tools_order_hash` | Tool definitions in received list order, ignoring object-key order |
| `tools_set_hash` | Same definitions ignoring only the outer list order |
| `tools_wire_hash` | Definitions preserving object-key order |
| `system_hash` | All system messages in order, including the style instruction |
| `catalog_hash` | Parsed HA entity rows, or `null` if the format was not recognized |
| `message_count`, `message_roles`, `message_hashes` | Conversation shape and per-message fingerprints |
| `request_hash` | Complete normalized request, including generation settings |

If `tools_order_hash` changes while `tools_set_hash` is stable, the same tools
arrived in a different order. If only `tools_wire_hash` changes, object-key order
changed. A JSON change does not prove cache invalidation, and identical JSON does
not prove reuse when another client or a restart intervenes. Use backend-reported
`cached_tokens`, `evaluated_prompt_tokens` and `prefill_ms` to judge the effect.

These are JSON fingerprints, not hashes of the rendered prompt or token ids. They
contain no prompts, names, tool definitions or conversation text, but they are
still local diagnostics. Keep them local.

## Request capture

Full normalized requests are available only with explicit opt-in:

```dotenv
CONTEXT_CAPTURE=1
CONTEXT_CAPTURE_LIMIT=5
```

The capture runs after normalization, before routing and the budget gate, so it
includes router refusals and budget rejections. A captured request does not imply
a backend inference. Internal reset turns are not captured.

The limit is 1–20 incoming requests per gateway process; a restart resets the
allowance and appends to retained files. Switch `CONTEXT_CAPTURE=0` after
collecting a sample. The file contains the full request body, including messages,
tools, entity names and arbitrary user metadata. It never stores the HTTP
`Authorization` header or the environment API key. Treat it as private data.

## Router miss transcript

`ROUTER_MISS_LOGGING=1` records requests that the router passes toward the LLM (before the budget gate):

```text
logs/router-misses/router-misses.jsonl
```

Each record contains the latest user utterance (up to 512 characters), the router
reason and correlation metadata. It does not contain the HA catalog, previous
turns, tool arguments or audio. Questions are included too, so ordinary
conversation is distinguishable from missed commands.

Enable it for a collection window, restart only the gateway, then disable it:

```bash
# .env: ROUTER_MISS_LOGGING=1 (optionally ROUTER_MISS_UNTIL=2026-11-24T00:00:00Z)
systemctl --user restart qwen-ha-gateway.service
# review, then set ROUTER_MISS_LOGGING=0
```

A restart does not move `ROUTER_MISS_UNTIL`. With logging disabled, no new
directory is created; existing files remain. Never commit or publish these
transcripts.

## Updating an existing deployment

### Gateway-only

Preserve `.env`, secrets and asset paths. Copy the complete repository, including
new `gateway/` modules, then run:

```bash
set -a
. ./.env
set +a
FULL_STACK=0 NCTX=10240 GGUF= TOKENIZER= \
  COUNTER_URL=http://127.0.0.1:18193/count ./setup.sh
```

This installs the gateway's Python dependencies, regenerates
`qwen-ha-gateway.service` and restarts only that service. It does not restart Qwen,
Whisper, TTS or LVA. Wait for router warmup, then check `/healthz` and the metrics
before enabling HA traffic.

Existing systemd drop-ins take precedence over generated units. Archive a
temporary GenieX upgrade override (for example `90-geniex-v0.11.0.conf`) before a
later full migration, or it will keep pointing at the old paths.

### STT/TTS images

Copying Python files into a running container does not update it. Rebuild the
image on ARM64, then recreate only that container:

```bash
# TTS
docker compose --env-file .env -f tts-pocket-wyoming/docker-compose.yml build
docker compose --env-file .env -f tts-pocket-wyoming/docker-compose.yml up -d pocket-tts-wyoming

# STT (the systemd unit owns its lifecycle)
docker compose --env-file .env -f stt/docker-compose.yml build
docker compose --env-file .env -f stt/docker-compose.yml create
systemctl --user restart qwen25-whisper-npu.service
```

Do **not** use `./run.sh stop` followed by `./run.sh start` only to rebuild an
image: the boot guard blocks a second Qwen cold load in the same boot. Reboot if
you need a full restart. Do not run a full start over a deployment with different
container names or ports.

### GenieX upgrade

The tested upgrade from 0.9.3 to 0.11.0 needed no model conversion or driver
update. The old boot guard incorrectly expected removed Small/Medium containers;
this repository's guard handles their absence and blocks running legacy/backup
Whisper copies. Staging a second GenieX installation also needs an isolated user
launcher: the vendor installer's `--prefix` does not change its launcher location.

Native DSP queue warnings can still appear with successful inference. Do not treat
a warning alone as a failed load; use the probe result and a real HA request.

## systemd and logs

The installer enables the Qwen and gateway units plus the selected STT/counter
units. TTS and LVA use Docker restart policies. To start user services without logging in:

```bash
sudo loginctl enable-linger "$USER"
```

Useful commands:

```bash
systemctl --user status qwen25-3b-backend qwen-ha-counter qwen-ha-gateway
systemctl --user restart qwen-ha-gateway
journalctl --user -u qwen-ha-gateway -n 50 --no-pager
docker ps
docker logs --tail 50 whisper-turbo-qnn
docker logs --tail 50 tts-pocket-pl
docker logs --tail 50 linux-voice-assistant
```

The installer writes the actual repository paths into the units. You do not need
to copy or edit unit files by hand. If you move the repository, regenerate the
units with `setup.sh`.

## Backup and restore

Back up only what is not reproducible:

- `.env` (contains paths and optionally an API key)
- `logs/` if you still need measurements or a private collection
- `lva` Docker volumes if you customized wake words, sounds or LVA configuration

Models and tokenizers are reproducible with `./run.sh prepare`. The repository
contains no secrets. Restoring is: copy the repository and `.env`, run
`./run.sh check`, then `./run.sh start`.
