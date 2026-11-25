# Router

The router is the deterministic fast path between Home Assistant and Qwen. It
proposes tool calls; it never executes them.

It handles a deliberately narrow set of commands:

- turn on / turn off: lights, switches, fans and input booleans
- open / close: covers
- set an absolute cover position from 0 to 100%

Everything else goes to the LLM with the original request intact. That is a
feature, not a failure: a wrong local action is worse than a slower correct one.

## Pipeline

1. **Normalize** case, Polish diacritics and repeated whitespace.
2. **Parse** the selected language's command grammar.
3. **Resolve** the spoken name and optional area against the current HA catalog.
4. **Validate** the advertised tool and its JSON Schema.
5. **Decide** `TOOLCALL`, `ACK`, `CLARIFY` or `PASS`.

`PASS` is the default for unknown grammar, unsupported domains, unavailable
non-position tools, unsupported schemas, missing targets and wrong areas.
`CLARIFY` is used when the request is understood but the target is ambiguous,
the area is missing, negation was detected, an absolute position is outside
0-100, or the cover has no position tool. `ACK` is returned only after a single
explicit tool success result.

## Supported grammar

The selected language supplies prepositions and reserved words. The shared
router supplies target resolution, uniqueness checks and schema validation.

| Language | Examples |
| --- | --- |
| Polish | `Włącz lampę w salonie`, `Zamknij rolety w sypialni`, `Otwórz Markiza w Atrium na 50%` |
| English | `Turn on the Desk lamp in the Study`, `Close the Curtain in the Bedroom`, `Set the Awning in the Atrium to fifty percent` |
| Spanish | `Enciende la luz en la cocina`, `Cierra la persiana`, `Abre la persiana en la cocina al 50 %` |

Device and area names in the examples are placeholders. The actual selectors
always come from the current HA context, never from a built-in list.

Up to four words may precede the command verb: unknown politeness and adverb
words are accepted, so `Natychmiast otwórz Markiza` and
`W tej chwili otwórz Markiza` stay local. The prefix is rejected when it contains
a scope-changing word — negation, a question, a condition, a conjunction or
`może` — so the complete utterance can reach the model. Polish and English
allow short prefixes unless blocked. Spanish only allows listed
politeness words: a room mentioned before the verb is not silently dropped.

Each language bundles a vocabulary with command synonyms and spoken replies,
and a separate grammar. Tool mappings, supported domains and selector checks
stay in code.

## Language strategies

`AGENT_LANGUAGE` selects exactly one strategy (`pl`, `en` or `es`) at startup. There is no
language detection, translation or plugin discovery.

| Module | Responsibility |
| --- | --- |
| `languages/__init__.py` | Shared registry, lazy loading of the selected strategy |
| `languages/base.py` | `Language` interface, reusable single-action parser, vocabulary validation, lemmatization and number words |
| `languages/pl/` | Polish rules and vocabulary, `w`/`we` area prepositions, optional SGJP inflection |
| `languages/en/` | English rules and vocabulary, `in`/`at`, phrasal verbs and the article `the` |
| `languages/es/` | Spanish rules and vocabulary, articles and `al` for cover positions |
| `command_router.py` | Language-independent target resolution, ambiguity checks and tool validation |

Only the selected language's dictionaries are warmed. Other strategies do not
import Polish inflection support. The installer always installs `simplemma` and
`num2words`; the optional Polish packages are installed only for `pl`. All
languages use the same indexed matcher. It searches explicit names
and aliases, generated forms (if available), lemmas (if available), then
approximate matches. Without dictionaries, the same matcher works from literal
spellings. Catalog indexes are cached per language strategy.

For a gateway-only installation, re-run the installer after changing the router
language. For a full stack, recreate local Whisper as well. Match the HA Assist
pipeline language and TTS provider to the selected language; see
[configuration](configuration.md#speech-providers).

## Grammar rules

Each strategy ships a `grammar.json` beside its vocabulary. The shared parser
uses `prefix_blockers` before the command and `tail_blockers` after it, once a
valid position suffix has been removed. `negations` identify an explicit refusal
before the command. `reserved_synonym_words` only validates vocabulary entries;
it is not a list of words to reject in the utterance. For example, Polish `w`
cannot be a command synonym but can introduce an area in the target.

The grammar also defines area and position prepositions and how to recognize
percentage expressions. Keep rules here when only the words differ. Use a
strategy's Python implementation when the parsing algorithm or morphology differs.
`ROUTER_VOCABULARY` replaces synonyms and replies only; it does not override grammar.

## Vocabulary

Each strategy's `vocabulary.json` is plain JSON. An abbreviated example:

```json
{
  "commands": {
    "close": ["zamknij", "zaciągnij"],
    "open": ["otwórz", "podnieś"],
    "on": ["włącz", "włączcie", "zapal"],
    "off": ["wyłącz", "zgaś", "zgas"],
    "position": ["ustaw"]
  },
  "responses": {
    "success": "Wykonano.",
    "default_clarification": "Nie mogę wykonać tego polecenia.",
    "clarifications": {
      "negation": "Nie wykonam zaprzeczonego polecenia.",
      "catalog-unavailable": "Nie widzę urządzeń w kontekście Home Assistant.",
      "no-target": "Nie znalazłem takiego urządzenia.",
      "ambiguous-area": "Nie wiem, którego obszaru dotyczy polecenie.",
      "missing-area": "Podaj nazwę obszaru.",
      "area-mismatch": "To urządzenie nie jest w tym obszarze.",
      "ambiguous-target": "Które urządzenie?",
      "invalid-position": "Podaj pozycję od 0 do 100 procent.",
      "position-unavailable": "To urządzenie nie obsługuje pozycji."
    }
  }
}
```

Rules enforced at startup:

- The top level must contain exactly `commands` and `responses`.
- `commands` requires `close`, `open`, `on`, `off`; `position` is optional.
- Each list must be non-empty and contain alphabetic words or phrases.
- Synonyms may not collide after case/diacritic normalization, and may not use
  grammar-control words (`nie`, `w`, `i`, `na`, `the`, `in`, ...).
- `responses` requires `success`, `default_clarification` and `clarifications`.
  Legacy `failure` is accepted but unused.
- Every clarification reason used by the code must be present. The legacy
  `plural-target` key is accepted; `invalid-position` and `position-unavailable`
  are used for the two local position clarifications described below.
- Spoken responses must be 1–300 characters.

Invalid or missing vocabulary stops startup before the HTTP socket opens. The
vocabulary is loaded once, not on every request.

To use a complete replacement without editing the repository, set an absolute
path:

```dotenv
ROUTER_VOCABULARY="/home/arduino/my-router-pl.json"
```

Restart only the gateway after a change. Device names, aliases and rooms belong
in Home Assistant, not in this file. Adding a verb does not add parameterized
commands, brightness, timers or multi-action support.

## Target and area resolution

The catalog comes from the stock HA “Static Context” entity overview. Explicit
spellings win over generated forms, generated forms over lemmas, then one-edit
recovery. If that still misses, a unique similarity fallback accepts a close
match only when it scores at least 0.75 and beats the runner-up by 0.05; a tie,
weak match or ambiguity stays unresolved and goes to clarification or the LLM.

- **Aliases:** comma-separated entries are literal aliases. Repeated spaces may
  be part of a friendly name and are preserved in the selector sent to HA.
- **Search hints:** a fragment split by two or more spaces may help recognize a
  spoken name. It is not evidence of an HA alias and is never sent as the
  selector.
- **Inflection:** Polish `polish-inflection` (SGJP) provides generated forms;
  `simplemma` provides lemmas. A generated plural may select one device after
  area filtering; multiple matches stay ambiguous.
- **STT recovery:** RapidFuzz computes Levenshtein distance ≤ 1 to recover small
  errors such as `relety` / `rolety` when the result is unique. The separate
  similarity fallback keeps its original scorer and thresholds.
- **Areas:** only whole aliases match. Area selectors always come from the HA
  context. For short room names, configure explicit aliases in HA.
- **Collisions:** after a unique alias match, the router also checks the primary
  canonical name against other rows. If the selector it would send is not
  unique, it asks for clarification instead of guessing.

The router does not remove an area selector or control a whole room by accident.
It does not invent an entity id. A joined area scalar with multiple aliases is
ambiguous and gets a clarification.

## Absolute cover positions

The router accepts one cover and one whole percentage from 0 to 100:

- `Otwórz Markiza w Atrium na 50%`
- `Zamknij Markiza w Atrium w 50 procentach`
- `Ustaw pozycję Markiza w Atrium na pięćdziesiąt procent`
- `Set the Awning in the Atrium to fifty percent`

The value must follow `na` / `w` / `do` in Polish or `to` / `at` in English. A bare
percentage is left to the LLM because it can be part of a literal device name.

This uses **absolute HA position**: 0 is closed, 100 is open, even when the verb is
`close` / `zamknij`. The router never converts a requested percentage into a full
open/close, never substitutes a different cover, and never falls back to a whole
room.

Position handling is deliberately split:

- a complete absolute phrase with a value outside 0-100 → local `CLARIFY` with
  `invalid-position`;
- a resolved cover whose advertised tools contain no `HassSetPosition` → local
  `CLARIFY` with `position-unavailable`;
- unknown wording, fractions, bare percentages and relative changes →
  `PASS` to the LLM.

The second case is not used for a missing/invalid schema: if the tool exists but
its schema is unsupported, the request still passes to the LLM.

Only `intent__HassSetPosition` or `HassSetPosition` is used, and only if its
current schema accepts the canonical name, domain, optional area and numeric
`position`, including type, enum, minimum/maximum and supported `allOf`
constraints. `jsonschema` validates the arguments against the advertised inline
schema; the router still rejects unsupported keywords and references. If the
schema is unsupported, the complete request passes to the LLM.

Digits work without a number library. Spelled numbers use a bounded lookup of
0–100 generated by `num2words`, not a handwritten list. Unknown wording,
fractions and out-of-range values pass to the model without inventing an action.

## ACK rules

A local ACK is returned only when:

- the last message is a tool result,
- exactly one tool call/result belongs to the current user turn,
- the tool is one of the router's action tools,
- the result explicitly reports success and no failure/error.

Failed, partially failed, unknown and multiple results pass to the LLM with the
tool call and its original result intact. The gateway does not retry an action or
claim success. ACK means “the HA tool reported success”, not “the motor reached
the position”.

## Fallback and safety

Unknown grammar, percentages the router cannot interpret, unavailable tools,
unsupported schemas, unresolved names/areas and non-actionable tool results pass
to the LLM with the original wording. The gateway does not rewrite a relative
brightness change into an absolute position or drop unsupported parameters.

Negation and real target/area ambiguity receive a local clarification. Questions,
conditions and multiple actions go to the model. This is a narrow command grammar,
not a general parser.

The router is covered by offline tests. Changes to domains, tool mappings or
schema handling need code and behavioral tests, not just vocabulary edits.
