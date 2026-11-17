# Troubleshooting

Start with these; they do not change anything:

```bash
./run.sh check
./run.sh status
docker ps
```

`check` prints `OK` or `MISSING` and never installs or starts anything. The item
marked `MISSING` is the one to fix.

## `check` reports `MISSING`

| Item | Fix |
| --- | --- |
| `GenieX version` | Install GenieX 0.11.0, or use `CACHE_POLICY=reset` only for 0.9.3. |
| `Qwen GGUF` / `Qwen tokenizer` | Set `GGUF` to the file from `geniex pull`; run `./run.sh prepare` for the tokenizer. |
| Prompt counter | Set `GGUF` + `TOKENIZER`, or `COUNTER_URL`. The gateway will not start without one. |
| `QAIRT` / `libQnnHtp.so` | Set `QNN_LIB_DIR` to the directory containing the HTP libraries. |
| STT model/tokenizer | Run `./run.sh prepare` with `STT_PROVIDER=whisper-qnn`. |
| TTS model/voice/Rust binary | Run `./run.sh prepare` with `TTS_PROVIDER=pocket-pl`. |
| `PulseAudio/PipeWire` | Start the user audio server; check `/run/user/<uid>/pulse/native`. |
| `paplay` / LVA volume library | Run `./run.sh prepare-audio` with the cue enabled. |

Do not rename the downloaded Whisper directory; `check` expects the pinned path.

## Gateway will not start: prompt counter

The exact error:

```text
A prompt counter is required: set GGUF + TOKENIZER or pass --prompt-counter-url
```

Pick one:

- set `GGUF` and `TOKENIZER` to the local Qwen files (the installer creates
  `qwen-ha-counter.service`), or
- set `COUNTER_URL` to an existing **loopback** counter and leave
  `GGUF`/`TOKENIZER` empty.

They are mutually exclusive. `./run.sh check` reports this as
`MISSING: prompt counter`.

## Qwen does not load or FastRPC reports `ENOMEM`

```bash
journalctl --user -u qwen25-3b-backend -n 100 --no-pager
docker ps
pgrep -a geniex
```

- Stop Whisper and any other NPU client before the cold load.
- `ENOMEM` means reboot. The guard allows **one** load attempt per boot; retrying
  or deleting `~/.local/state/ha-local-agent/attempt-<boot_id>` is not a fix.
- If the version check fails, install GenieX 0.11.0 (or 0.9.3 with
  `CACHE_POLICY=reset`).

## Whisper never becomes healthy

```bash
docker logs --tail 100 whisper-turbo-qnn
docker inspect whisper-turbo-qnn --format '{{json .State.Health}}'
```

Usually one of:

- `QNN_LIB_DIR` has a different QAIRT series than the tested 2.50 build,
- the board paths for `libcdsprpc.so.1`, `libdmabufheap.so.0` or `libatomic.so.1`
  differ from the compose mounts,
- another process owns the NPU,
- the downloaded artifact is incomplete.

There is no CPU fallback. A failed HTP session fails instead of silently
producing wrong transcripts.

## Home Assistant cannot connect

| Service | Address |
| --- | --- |
| Wyoming STT | `tcp://<Arduino-IP>:10301` |
| Wyoming TTS | `tcp://<Arduino-IP>:10201` |
| Local OpenAI LLM | `http://<Arduino-IP>:18191/v1` |
| ESPHome/LVA | `<Arduino-IP>:6053`, no `http://` |

Test the gateway from the HA host:

```bash
curl --fail --max-time 5 http://<Arduino-IP>:18191/healthz
curl --fail --max-time 5 http://<Arduino-IP>:18191/v1/models
```

`healthz` proves only that the HTTP process is alive. Check inference with:

```bash
curl --fail --max-time 60 http://<Arduino-IP>:18191/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Odpowiedz jednym słowem: test"}],"max_tokens":16}'
```

If `QWEN_GATEWAY_API_KEY` is set, send `Authorization: Bearer <key>` from both HA
and `curl`. If HA is on another machine, do not bind `GATEWAY_HOST=127.0.0.1`.

## Gateway HTTP errors

| Status | Cause | Fix |
| --- | --- | --- |
| `401` | Wrong/missing bearer token | Use the same `QWEN_GATEWAY_API_KEY` in `.env` and HA. |
| `413` | Prompt + output exceeds `NCTX` | Expose fewer entities or shorten tools; raise `NCTX` only with the counter. |
| `429` | Another inference is running | Retry later; the gateway serializes inference. |
| `502` | Backend unavailable/reset failed | Check Qwen/GenieX and the backend port. |
| `503` | Prompt counter unavailable | Check `qwen-ha-counter` or `COUNTER_URL`; the gateway fails closed. |

A client disconnect is recorded as `client_disconnected`, not a model failure.
The gateway does not append a JSON error to an SSE stream that already started.

## No sound, no cue, or late TTS

- **No waiting cue:** check `LLM_WAIT_SOUND=1`, `command -v paplay`,
  `LLM_WAIT_SOUND_FILE`, and for `lva` mode `LVA_VOLUME_URL` plus an unmuted LVA
  output. Test with `./run.sh sound-test`.
- **TTS silent:** check `docker logs --tail 100 tts-pocket-pl`; verify assets with
  `./run.sh check`; verify the LVA output device/sink. The TTS container produces
  PCM, LVA plays it.
- **TTS starts late:** a short one-sentence answer may wait for the end of the
  text stream before synthesis. First PCM is not speaker playback; see
  [performance.md](performance.md#measuring-a-pipeline-run).

## A simple command goes to the LLM

That is fail-open behavior, not a crash. The target/area may be ambiguous, the
tool may be missing, or the sentence may be outside the supported grammar. Check
[router.md](router.md). For a bounded sample, set `ROUTER_MISS_LOGGING=1`,
restart only the gateway, review `logs/router-misses/`, then disable it.

To prove the router is not causing a wrong action, set `DECIDE_APPLY=0` and
restart the gateway.

## Logs and reporting a bug

```bash
journalctl --user -u qwen-ha-gateway -n 50 --no-pager
docker logs --tail 50 whisper-turbo-qnn
docker logs --tail 50 tts-pocket-pl
```

Include `./run.sh check`, the failing unit/container log, board model, Ubuntu,
GenieX and QAIRT versions. Remove hostnames, entity names and tokens.

Never attach `.env`, API keys, model files, raw audio, `logs/router-misses/` or
`logs/request-capture/`. The metrics and context-audit files are payload-free but
still local diagnostics.
