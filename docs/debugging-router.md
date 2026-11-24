# Debugging the router

This walks through `command_router.decide()` on a normal PC. It needs no board,
Docker, model, NPU or network access. The harness uses synthetic HA entities and
tool schemas.

The debug entry point is [tools/debug_router.py](../tools/debug_router.py). It
inserts `gateway/` into `sys.path`, builds a catalog with
`context_catalog.parse_overview()`, and calls `decide()`.

## 1. Prepare the PC

Install Python 3.10+ (3.12 is a good default) and Git. Then, from a terminal:

### Linux / macOS

```bash
git clone https://github.com/p4vv37/domuno.git
cd domuno
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install simplemma==2.0.0 num2words==0.5.14 jsonschema==4.25.1 RapidFuzz==3.14.1
```

For inflected Polish names, optionally install:

```bash
python -m pip install polish-inflection==0.7.3 polish-inflection-data==2026.6.28
```

### Windows PowerShell

```powershell
git clone https://github.com/p4vv37/domuno.git
cd domuno
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install simplemma==2.0.0 num2words==0.5.14 jsonschema==4.25.1 RapidFuzz==3.14.1
```

If PowerShell blocks activation, either run
`Set-ExecutionPolicy -Scope Process Bypass` in that terminal or use
`.\.venv\Scripts\python.exe` directly.

The optional packages are not required for the bundled scenarios: they use exact
names such as `Lampa`, `Markiza` and `Atrium` on purpose. Install them if you want
to experiment with inflected names such as `lampę` / `rolety`.

## 2. Run the harness without a debugger

```bash
python tools/debug_router.py --list
python tools/debug_router.py toolcall
```

Example output:

```text
scenario: toolcall
what:     existing light in an exact area -> TOOLCALL
kind:     toolcall
reason:   None
tool:     intent__HassTurnOn
args:     {"name": "Lampa", "domain": ["light"], "area": "Salon"}
```

### Scenarios

| Scenario | What it exercises | Expected result |
| --- | --- | --- |
| `toolcall` | existing light in an exact area | `TOOLCALL` |
| `outside-grammar` | two actions in one sentence | `PASS outside-grammar` |
| `negation` | explicit negation | `CLARIFY negation` |
| `ambiguous` | same name in two areas | `CLARIFY ambiguous-target` |
| `no-target` | unknown device | `PASS no-target` |
| `closest-match` | larger STT name/area typo | `TOOLCALL` |
| `prefix` | unknown leading phrase before the verb | `TOOLCALL` |
| `blocked-prefix` | condition/question prefix | `PASS outside-grammar` |
| `position` | absolute cover position | `TOOLCALL HassSetPosition` |
| `position-unavailable` | cover without position tool | `CLARIFY position-unavailable` |
| `invalid-position` | absolute value outside 0-100 | `CLARIFY invalid-position` |
| `relative-position` | relative cover change | `PASS` to the LLM |
| `ack` | single explicit tool success | `ACK` |
| `english` | English strategy | `TOOLCALL` |
| `catalog-unavailable` | no HA overview | `PASS catalog-unavailable` |

## 3. VS Code

1. Install the **Python** (`ms-python.python`) and **Python Debugger**
   (`ms-python.debugpy`) extensions. The repository recommends both in
   `.vscode/extensions.json`.
2. Open the **repository root** as the workspace:
   `File → Open Folder → domuno`. Do not open the parent
   `qwen-npu` directory; `${workspaceFolder}` must point at the repo.
3. Run `Python: Select Interpreter` and choose `.venv/bin/python`
   (Linux/macOS) or `.venv\Scripts\python.exe` (Windows).
4. Open `gateway/command_router.py` and set a breakpoint:
   - at the start of `decide()`, or
   - on `command = language.parse(utterance)`,
   - on `targets = resolve(...)`,
   - on `function = _action_tool(...)`.
5. Open **Run and Debug**, choose `Router: wybierz scenariusz`, and pick a
   scenario from the picker. The picker covers all 15 scenarios listed above,
   including the position, English, closest-match and catalog-unavailable cases.
6. Press **F5**. Use **F10** (step over), **F11** (step into), **Shift+F11**
   (step out). Inspect `command`, `targets`, `entity`, `function` and
   `arguments` in the Variables panel.

The committed [.vscode/launch.json](../.vscode/launch.json) contains this
picker configuration. If your VS Code uses the older Python extension and does
not recognise `"type": "debugpy"`, replace it with `"type": "python"`.

## 4. PyCharm Community

PyCharm Community can debug plain Python; none of the Professional-only Django or
Flask features are needed.

1. Open the `domuno` folder.
2. **Settings → Project → Python Interpreter → Add Local Interpreter**, choose
   the `.venv` interpreter.
3. **Run → Edit Configurations → + → Python**.
4. Set:
   - Script path: `tools/debug_router.py`
   - Parameters: `toolcall` (or another scenario)
   - Working directory: the repository root
   - Interpreter: the `.venv` interpreter
5. Set a breakpoint in `gateway/command_router.py` and choose **Debug**.

## 5. Full HA loop

`tools/simulate_ha_loop.py` walks through the complete conversation without a
board. It starts the real gateway handler with a synthetic HA catalog and a fake
GenieX upstream, then plays HA's side: STT text, gateway request, optional tool
call, simulated HA execution, tool result, gateway ACK or LLM fallback, and TTS.

```bash
python tools/simulate_ha_loop.py --list
python tools/simulate_ha_loop.py router-toolcall --step
```

| Scenario | What it exercises |
| --- | --- |
| `router-toolcall` | router tool call → HA success → router ACK |
| `router-cover` | `HassSetPosition` → HA success → router ACK |
| `router-clarify` | ambiguous target → local clarification, no tool |
| `fallback-text` | router passes → fake LLM text → TTS |
| `fallback-toolcall` | router passes → fake LLM tool call → HA result → router ACK |
| `tool-error` | tool fails → router passes the result → LLM explains |

What is real: gateway HTTP handler, request normalization, prompt-budget gate,
router, SSE encoding and ACK logic. What is simulated: LVA audio, STT, the
GenieX model, HA tool execution and TTS. The script prints the component that
handled each request, so use breakpoints in:

- `tools/simulate_ha_loop.py` — each HA-side stage;
- `gateway/chat_gateway.py` — `_post()` and `send_router_decision()`;
- `gateway/command_router.py` — `decide()`.

The `HA loop: full simulated pipeline` VS Code configuration runs the picker with
`--step`.

## 6. What to watch while stepping

In `decide()`:

- `command` — the parsed `intent`, `target`, `reason`, `position`;
- `position_out_of_range` — only true for a complete absolute phrase outside
  0-100;
- `targets` — candidate `(index, entity)` pairs after name and area resolution;
- `entity` — the single selected entity, its domain and areas;
- `function` — the advertised HA tool selected for the intent;
- `arguments` — the final selectors sent to HA.

Useful breakpoints:

| Where | Why |
| --- | --- |
| `command = language.parse(...)` | see exactly what the language strategy understood |
| `targets = resolve(...)` | see name matching before area filtering |
| `if area_text:` | see area filtering |
| `if len(targets) != 1:` | see ambiguity detection |
| `function = _action_tool(...)` | see tool choice |
| `arguments = _arguments(...)` | see schema validation |

## 7. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: command_router` | Run from the repository root; the harness adds `gateway/` to `sys.path`. |
| VS Code uses the wrong Python | `Python: Select Interpreter` → `.venv`. |
| `debugpy` is not a valid type | Install Python Debugger, or change `type` to `python`. |
| `polish-inflection` install fails | It is optional; the bundled scenarios use exact names and still run. |
| No breakpoint hit | Make sure you opened the repo root and launched `Router: wybierz scenariusz` or the HA loop configuration, not just “Run Python File”. |
| Output differs from the table | Run `python tools/debug_router.py --list` and check optional packages; exact-name scenarios should not depend on them. |

These are offline harnesses. They do not validate HA connectivity, audio, NPU,
Docker, or the board-specific gateway process; the loop uses the real handler
but a fake upstream.
