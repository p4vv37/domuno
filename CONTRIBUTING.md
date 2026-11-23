# Contributing to Domuno

First off, thank you for considering contributing to Domuno. It is people
like you who make a local, open voice stack better.

Following these guidelines helps communicate that you respect the time of the
maintainer and other contributors. In return, you can expect your issue, change
or pull request to be read and answered respectfully.

## What we are looking for

This is a small, hardware-specific project. Useful contributions include:

- reproducible bug fixes, with the board image, GenieX/QAIRT version and Home
  Assistant integration version
- documentation fixes: commands, paths, ports, configuration and failure modes
- router grammar or Home Assistant catalog parsing covered by offline tests
- smaller, clearer code with the same behavior
- measured performance data with the exact model, runtime, context and workload

## What is out of scope

- support for arbitrary ARM64 boards, other QAIRT versions or other model
  runtimes, unless you have measured them on real hardware
- replacing the tested model/artifact without a compatibility and performance
  report
- features that depend on cloud inference or a permanently running GPU host
- adding configuration variables or abstractions without a concrete use case

This repository is a tested recipe, not a generic installer. A documented
limitation is often better than a speculative feature.

## Ground rules

- Be respectful and constructive. Harassment, personal attacks and dismissive
  behavior are not welcome.
- Test your change. A change that cannot be tested on the board may still be
  useful, but say so explicitly.
- Keep one feature or fix per pull request when practical.
- Do not add a dependency or configuration switch unless it is needed now and
  has a documented default.
- Never commit private data: `.env`, API keys, model files, audio, real entity
  names, router-miss transcripts, request captures or unredacted logs.
- Do not open a public issue for a security vulnerability. Use GitHub's private
  vulnerability reporting if it is enabled for the repository; otherwise contact
  the maintainer through GitHub without posting exploit details publicly.

## Your first contribution

Not sure where to begin? Documentation fixes, small test improvements and
reproducible bug reports are all valuable. Look for issues labelled `good first
issue` or `help wanted` if they exist.

Working on your first pull request? [makeapullrequest.com](https://makeapullrequest.com/)
and [firsttimersonly.com](https://www.firsttimersonly.com/) are friendly starting
points.

## Getting started

1. Fork the repository and create a branch for your change.
2. Make the change and keep the diff focused.
3. Run the offline tests:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
   ```

   Some tests are skipped without `numpy`, `simplemma`, `num2words` or
   `polish-inflection`. CI installs them; a local `.venv` is fine too. Use
   `.venv/bin/python` to exercise the installed `simplemma`; number normalization
   tests require `num2words==0.5.14`. To step through the router on a PC, use the
   synthetic harness and the VS Code / PyCharm setup in
   [docs/debugging-router.md](docs/debugging-router.md).

4. If you changed board, NPU, Docker, audio or systemd behavior, test on the
   board and describe the result. "Not tested on hardware" is acceptable when
   stated clearly; pretending otherwise is not.
5. Update the relevant document in `docs/` when behavior changes. The README is
   the overview; details belong in `docs/configuration.md`, `docs/operations.md`,
   `docs/home-assistant.md`, `docs/router.md`, `docs/performance.md` or
   `docs/troubleshooting.md`.
6. Open a pull request that explains what changed, why, and what you tested.

There is no Contributor License Agreement. Contributions are accepted under the
repository's MIT license.

## Repository layout

```text
run.sh / .env.example   public entry point and configuration
setup.sh                internal installer (systemd units + uv venv)
llm/backend.py          GenieX version check, boot guard, readiness
stt/                    QNN Whisper engine, profile validation, Wyoming server
gateway/                OpenAI proxy, prompt budget, router, metrics
tts-pocket-wyoming/     Rust TTS adapter, text streaming, PCM delivery
lva/                    Linux Voice Assistant compose configuration
tools/                  offline router and full HA-loop debug harnesses
tests/                  offline unit and integration-style tests
docs/                   configuration, operations, router, debugging, performance
```

Start reading at `gateway/qwen_npu_gateway.py`, then
`gateway/qwen_context_catalog.py` (HA overview parsing) and
`gateway/qwen_decision.py` (routing and tool validation). For audio, start with
the Wyoming handlers, then `stt/qnn_whisper_engine.py` or
`tts-pocket-wyoming/rust_tts_backend.py`.

## How to report a bug

Security issues first: do not open a public issue for a vulnerability. Use the
repository's private vulnerability reporting if available, or contact the
maintainer through GitHub without posting details publicly.

For a normal bug report, include:

1. What board image, Ubuntu, GenieX and QAIRT version are you using?
2. What model, context size and Home Assistant integration version?
3. What did you do?
4. What did you expect to see?
5. What did you see instead?

Also include:

- `./run.sh check` output
- the failing unit or container log, with private values removed
- whether the failure reproduces after a reboot

Do not attach `.env`, request captures, router transcripts or raw audio.

## How to suggest a feature

Open an issue and describe:

- the problem you want to solve
- the hardware and software constraints
- why the current router, prompt budget or LLM flow cannot solve it
- how you would test it on the board

If the feature adds a configuration variable, explain the default and what
happens when it is unset.

## How a contribution is reviewed

Reviews are best-effort; there is no guaranteed response time. A pull request is
more likely to be accepted when:

- the offline test suite passes
- the change is small and has a clear purpose
- hardware-specific claims include the exact setup and measurement
- private data and generated assets are not committed
- documentation is updated with the code

The maintainer may ask for a narrower scope, a test, or a measurement before
merging. A rejected change is not a judgement of your work; it may simply not fit
this hardware-specific recipe.

## Style

- Python: standard library unless a dependency is already pinned; keep the
  gateway importable without the NPU runtime.
- Shell: `set -euo pipefail`, quote paths, fail with a useful message.
- Systemd unit values: no quotes, `$`, `%`, `\` or newlines; use the installer.
- Logging: never log prompts, transcripts, entity names, tool arguments, audio or
  credentials. Metrics must stay payload-free. Diagnostics are private opt-ins.
- Tests: synthetic Home Assistant data, fake models, no network and no NPU. Tests
  must pass on a machine without the board.
- Commit messages: a short imperative subject; add a body when the reason is not
  obvious.

## Community

GitHub issues and pull requests are the project's communication channel. There is
no chat server or mailing list. Be patient and assume good faith.

---

This guide follows the structure of the
[contributing template](https://github.com/nayafia/contributing-template) by
Nadia Eghbal ([@nayafia](https://github.com/nayafia)), which is dedicated to the
public domain under CC0 1.0 and recommended by
[GitHub's Open Source Guides](https://opensource.guide/starting-a-project/#writing-your-contributing-guidelines).
The project-specific wording was adapted for this repository.
