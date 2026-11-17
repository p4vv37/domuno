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
tools, unsupported schemas, missing targets and wrong areas. `CLARIFY` is used
when the request is understood but the target is ambiguous, the area is missing,
or negation was detected. `ACK` is returned only after a single explicit tool
success result.

## Supported grammar

The selected language supplies prepositions and reserved words. The shared
router supplies target resolution, uniqueness checks and schema validation.

| Language | Examples |
| --- | --- |
| Polish | `Włącz lampę w salonie`, `Zamknij rolety w sypialni`, `Otwórz Markiza w Atrium na 50%` |
| English | `Turn on the Desk lamp in the Study`, `Close the Curtain in the Bedroom`, `Set the Awning in the Atrium to fifty percent` |

Device and area names in the examples are placeholders. The actual selectors
always come from the current HA context, never from a built-in list.

Polish and English command words come from `gateway/router_pl.json` and
`gateway/router_en.json`. The JSON contains only command synonyms and spoken
replies. Tool mappings, supported domains, grammar and selector checks stay in
code.

## Language strategies

`AGENT_LANGUAGE=pl|en` selects exactly one strategy at startup. There is no
language detection, translation or plugin discovery.

| Module | Responsibility |
| --- | --- |
| `qwen_language.py` | Shared interface, vocabulary validation, `simplemma` lemmatization, two-language factory, number words |
| `qwen_language_pl.py` | Polish grammar, `w`/`we` area prepositions, optional SGJP inflection through `polish-inflection` |
| `qwen_language_en.py` | English grammar, `in`/`at`, `turn on`, `switch off`, article `the` |
| `qwen_decision.py` | Language-independent target resolution, ambiguity checks and tool validation |

Only the selected language's dictionaries are warmed. English does not import the
Polish strategy or require `polish-inflection`. The installer always installs
`simplemma` and `num2words`; the optional Polish packages are installed only for
`pl`. Without a dictionary, lexical matching remains available. Catalog indexes
are cached per language strategy, so Polish and English analyses cannot share the
wrong index.

A router-language change needs only a gateway restart. If the English dependency
was never installed, run the gateway-only installer. Match the HA Assist pipeline
language separately.

## Vocabulary

`router_pl.json` and `router_en.json` are plain JSON. An abbreviated example:

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
    "failure": "Nie udało się.",
    "default_clarification": "Nie mogę wykonać tego polecenia.",
    "clarifications": {
      "negation": "Nie wykonam zaprzeczonego polecenia.",
      "catalog-unavailable": "Nie widzę urządzeń w kontekście Home Assistant.",
      "no-target": "Nie znalazłem takiego urządzenia.",
      "ambiguous-area": "Nie wiem, którego obszaru dotyczy polecenie.",
      "missing-area": "Podaj nazwę obszaru.",
      "area-mismatch": "To urządzenie nie jest w tym obszarze.",
      "ambiguous-target": "Które urządzenie?",
      "plural-target": "Które urządzenie?",
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
- `responses` requires `success`, `failure`, `default_clarification` and
  `clarifications`.
- Every clarification reason used by the code must be present. Optional reasons
  `invalid-position` and `position-unavailable` may be added.
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
spellings win over generated forms, generated forms over lemmas, and every exact
hit wins over one-edit recovery.

- **Aliases:** comma-separated entries are literal aliases. Repeated spaces may
  be part of a friendly name and are preserved in the selector sent to HA.
- **Search hints:** a fragment split by two or more spaces may help recognize a
  spoken name. It is not evidence of an HA alias and is never sent as the
  selector.
- **Inflection:** Polish `polish-inflection` (SGJP) provides generated forms;
  `simplemma` provides lemmas. A generated plural may select one device after
  area filtering; multiple matches stay ambiguous.
- **STT recovery:** one edit (Levenshtein distance ≤ 1) recovers small errors such
  as `relety` / `rolety` when the result is unique.
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
`close` / `zamknij`. Relative changes such as “close by 20%” are outside the rule.
The router never converts a requested percentage into a full open/close, never
substitutes a different cover, and never falls back to a whole room.

Only `intent__HassSetPosition` or `HassSetPosition` is used, and only if its
current schema accepts the canonical name, domain, optional area and numeric
`position`, including type, enum, minimum/maximum and supported `allOf`
constraints. If the tool is missing or the schema is unsupported, the complete
request passes to the LLM.

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
