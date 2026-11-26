#!/usr/bin/env python3
"""Language-independent HA command router; proposes tools, never executes actions.

Names and areas come from the current HA overview; arguments must satisfy the
advertised JSON Schema. Simple on/off, open/close, absolute cover positions and absolute light
brightness are routed.
Other utterances pass to the LLM. Ambiguous targets ask for clarification.

Matching builds a per-catalog index: explicit spellings first, then inflected
forms provided by the language strategy, then lemmas, then a one-edit recovery
for STT noise. Singular and plural forms share target resolution. The router
proposes an action for one matching device after area filtering, or for an
explicit, unambiguous area in a generic light-brightness request.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache, partial
import enum
import json
import math
import re

from jsonschema import Draft7Validator, SchemaError
from rapidfuzz.distance import Levenshtein

from context_catalog import Catalog, parse_overview
from languages import default_language, normalize


_TOOL_NAMES_BY_INTENT = {
    'close': ('intent__HassTurnOff', 'HassTurnOff'),
    'open': ('intent__HassTurnOn', 'HassTurnOn'),
    'off': ('intent__HassTurnOff', 'HassTurnOff'),
    'on': ('intent__HassTurnOn', 'HassTurnOn'),
    'position': ('intent__HassSetPosition', 'HassSetPosition'),
    'brightness': ('light__HassLightSet', 'HassLightSet'),
}
_DOMAINS = {
    'open': frozenset(('cover',)),
    'close': frozenset(('cover',)),
    'brightness': frozenset(('light',)),
    'on': frozenset(('light', 'switch', 'fan', 'input_boolean')),
    'off': frozenset(('light', 'switch', 'fan', 'input_boolean')),
    'position': frozenset(('cover',)),
}
# Locks, alarms and parameterized actions other than cover positions stay with LLM.
_ACTION_TOOL_NAMES = frozenset(name for names in _TOOL_NAMES_BY_INTENT.values() for name in names)
# HA joins literal names/aliases with commas. Repeated spaces can belong to a
# friendly name and must survive in selectors sent back to HA.
_ALIAS_SEP = re.compile(r'\s*,\s*')


def _text_content(content):
    """Extract text from a string or an OpenAI list of text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text' \
                    and isinstance(part.get('text'), str):
                parts.append(part['text'])
        return ' '.join(parts) if parts else None
    return None


class DecisionKind(str, enum.Enum):
    PASS = 'pass'
    CLARIFY = 'clarify'
    TOOLCALL = 'toolcall'
    ACK = 'ack'


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str | None = None
    response: str | None = None
    tool_call: ToolCall | None = None


def _split_raw(name_scalar):
    """Split comma-joined literal aliases, preserving internal whitespace."""
    return [p for p in _ALIAS_SEP.split(name_scalar.strip()) if p]


def _name_spellings(scalar):
    """Full names plus existing short search hints, never HA selectors.

    A repeated-space fragment may help recognize a spoken device name. It is
    not evidence of an actual HA alias. Always send the full literal selector.
    """
    literals = _split_raw(scalar)
    parts = [part for name in literals for part in re.split(r'\s{2,}', name)]
    return tuple(dict.fromkeys(literals + parts))


def _canonical(name_scalar):
    """First (primary) alias of a joined scalar, or None when empty."""
    parts = _split_raw(name_scalar)
    return parts[0] if parts else None


# Catalog matching. Explicit spellings win over generated forms, generated
# forms over lemmas, and every exact hit wins over a one-edit recovery.


class _FormIndex:
    """Alias spellings of one catalog field, keyed three ways."""
    __slots__ = ('names', 'forms', 'lemmas')

    def __init__(self):
        self.names = {}   # normalized literal/search spelling -> {entity index}
        self.forms = {}   # generated normalized form -> {entity index}
        self.lemmas = {}  # simplemma lemma -> {entity index}


def _explicit_aliases(scalar):
    """Normalized explicit aliases of a joined scalar."""
    return {normalize(alias) for alias in _split_raw(scalar)} if scalar else set()


def _fill(index, aliases, entity_index, language):
    for alias in aliases:
        raw = normalize(alias)
        if raw:
            index.names.setdefault(raw, set()).add(entity_index)
        for form, _number in language.forms(alias):
            index.forms.setdefault(form, set()).add(entity_index)
        lemma = language.lemma(alias)
        if lemma:
            index.lemmas.setdefault(lemma, set()).add(entity_index)


@lru_cache(maxsize=4)
def _indexes(catalog, language):
    """Name and area indexes for one catalog; equal overviews reuse the cache."""
    names, areas = _FormIndex(), _FormIndex()
    for entity_index, entity in enumerate(catalog.entities):
        _fill(names, _name_spellings(entity.names), entity_index, language)
        if entity.areas:
            _fill(areas, _split_raw(entity.areas), entity_index, language)
    return names, areas


def _fuzzy(index, text, lemma):
    """One-edit recovery, explicit spellings before generated forms."""
    for table in (index.names, index.forms):
        found = set()
        for key, value in table.items():
            if Levenshtein.distance(key, text, score_cutoff=1) > 1:
                continue
            found.update(value)
        if found:
            return found
    if lemma and lemma != text:
        found = {entity for key, value in index.lemmas.items()
                 if Levenshtein.distance(key, lemma, score_cutoff=1) <= 1
                 for entity in value}
        if found:
            return found
    return _closest(index, text)


def _closest(index, text):
    """Unique high-similarity recovery for larger ASR errors.

    Only used after exact/form/lemma/one-edit matching fails. A single best
    candidate must clear 0.75 similarity and beat the runner-up by 0.05; ties or
    weak matches stay unresolved so the caller can ask or pass to the LLM.
    """
    if len(text) < 5:
        return None
    best = second = 0.0
    found = set()
    for table in (index.names, index.lemmas):
        for key, value in table.items():
            score = SequenceMatcher(None, text, key).ratio()
            if score > best + 1e-9:
                second, best, found = best, score, set(value)
            elif abs(score - best) <= 1e-9:
                found.update(value)
            elif score > second:
                second = score
    return found if best >= .75 and best - second >= .05 else None


def _index_match(index, text, lemma, *, allow_fuzzy=True):
    """Matching entity indexes for one phrase, or None when nothing matches."""
    hit = index.names.get(text)
    if hit:
        return set(hit)
    hit = index.forms.get(text)
    if hit:
        return set(hit)
    if lemma and lemma != text:
        hit = index.lemmas.get(lemma)
        if hit:
            return set(hit)
    return _fuzzy(index, text, lemma) if allow_fuzzy else None


def _resolve(index, catalog, text, *, language, allow_fuzzy=True):
    """Resolve a spoken phrase to matching (index, entity) pairs."""
    norm = normalize(text)
    if not norm:
        return []
    lemma = language.lemma(text)
    hit = _index_match(index, norm, lemma, allow_fuzzy=allow_fuzzy)
    if hit is None:
        return []
    return [(i, catalog.entities[i]) for i in sorted(hit)]


def _last_tool_content(messages):
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            return _text_content(message.get('content'))
    return None


def _tool_result_name(messages):
    """Name of the tool that produced the last `role: tool` result."""
    if not isinstance(messages, list):
        return None
    tool_call_id = None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            tool_call_id = message.get('tool_call_id')
            break
    if not tool_call_id:
        return None
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        for call in (message.get('tool_calls') or []):
            if not isinstance(call, dict) or call.get('id') != tool_call_id:
                continue
            fn = call.get('function')
            if isinstance(fn, dict):
                return fn.get('name')
    return None


def _ack_response(content, language=None):
    """Confirm explicit success only; failures and partial results need the LLM."""
    language = language or default_language()
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get('data', {})
    if (data.get('error') or data.get('success') is False
            or isinstance(inner, dict) and (inner.get('error') or inner.get('failed'))):
        return None
    if (data.get('success') is True
            or isinstance(inner, dict) and inner.get('success')):
        return language.vocabulary['responses']['success']
    return None


def _action_tool(intent, tools, tool_choice):
    names = _TOOL_NAMES_BY_INTENT[intent]
    if tool_choice not in (None, 'auto', 'required') and not isinstance(tool_choice, dict):
        return None
    if isinstance(tool_choice, dict):
        function = tool_choice.get('function')
        name = function.get('name') if isinstance(function, dict) else None
        if name not in names:
            return None
        names = (name,)
    for tool in tools if isinstance(tools, list) else []:
        if (isinstance(tool, dict) and tool.get('type') == 'function'
                and isinstance(tool.get('function'), dict)
                and tool['function'].get('name') in names):
            return tool['function']
    return None


_SCHEMA_TYPES = {'object', 'array', 'string', 'integer', 'number'}
_SCHEMA_KEYS = {'type', 'enum', 'const', 'properties', 'required', 'items',
                'additionalProperties', 'description', 'title', 'default',
                'minimum', 'maximum', 'allOf'}


def _supported_schema(schema):
    """Allow only inline HA constraints, including in unused properties."""
    if not isinstance(schema, dict) or schema.keys() - _SCHEMA_KEYS:
        return False
    kind = schema.get('type')
    if 'type' in schema or 'allOf' not in schema:
        if not isinstance(kind, str) or kind not in _SCHEMA_TYPES:
            return False
    for key in ('minimum', 'maximum'):
        if key in schema:
            limit = schema[key]
            if type(limit) not in (int, float) or (isinstance(limit, float) and not math.isfinite(limit)):
                return False
    properties = schema.get('properties', {})
    branches = schema.get('allOf', [])
    return (isinstance(properties, dict) and all(map(_supported_schema, properties.values()))
            and isinstance(branches, list) and all(map(_supported_schema, branches))
            and ('items' not in schema or _supported_schema(schema['items']))
            and isinstance(schema.get('additionalProperties', False), bool))


def _schema_accepts(value, schema):
    if not _supported_schema(schema):
        return False
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError:
        return False
    return Draft7Validator(schema).is_valid(value)


def _device_action(intent, arguments, tools, tool_choice):
    function = _action_tool(intent, tools, tool_choice)
    if function is None:
        if intent == 'position' and _action_tool('position', tools, None) is None:
            return Decision(kind=DecisionKind.CLARIFY, reason='position-unavailable')
        return Decision(kind=DecisionKind.PASS, reason='tool-unavailable')
    schema = function.get('parameters')
    if not _schema_accepts(arguments, schema) or arguments.keys() - schema.get('properties', {}).keys():
        return Decision(kind=DecisionKind.PASS, reason='unsupported-schema')
    return Decision(kind=DecisionKind.TOOLCALL,
                    tool_call=ToolCall(name=function['name'], arguments=arguments))


def _area_brightness(catalog, allowed, brightness, tools, tool_choice):
    """Send an area-only call only for a single unambiguous area."""
    if not any(catalog.entities[i].domain == 'light' for i in allowed):
        return Decision(kind=DecisionKind.PASS, reason='no-target')
    areas = {tuple(_split_raw(catalog.entities[i].areas or '')) for i in allowed}
    if len(areas) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-area')
    (selectors,) = areas
    if len(selectors) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-area')
    area = selectors[0]
    if any(i not in allowed and normalize(area) in _explicit_aliases(entity.areas)
           for i, entity in enumerate(catalog.entities)):
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-area')
    if not 0 <= brightness <= 100:
        return Decision(kind=DecisionKind.CLARIFY, reason='invalid-position')
    return _device_action('brightness', {'area': area, 'domain': ['light'], 'brightness': brightness},
                          tools, tool_choice)


def decide(messages, catalog: Catalog | None, tools, default_area=None, tool_choice=None, language=None):
    """Route the current turn only; never replay an action from older history."""
    language = language or default_language()
    if not isinstance(messages, list) or not messages:
        return Decision(kind=DecisionKind.PASS, reason='no-utterance')
    last = messages[-1]
    if not isinstance(last, dict):
        return Decision(kind=DecisionKind.PASS, reason='no-utterance')
    if last.get('role') == 'tool':
        # Multiple calls/results need the model to summarize their combined outcome.
        turn = []
        for message in reversed(messages):
            if isinstance(message, dict) and message.get('role') == 'user':
                break
            turn.append(message)
        if (sum(m.get('role') == 'tool' for m in turn if isinstance(m, dict)) != 1
                or any(len(m.get('tool_calls') or []) > 1 for m in turn if isinstance(m, dict))):
            return Decision(kind=DecisionKind.PASS, reason='multiple-results')
        if _tool_result_name(messages) in _ACTION_TOOL_NAMES:
            response = _ack_response(_last_tool_content(messages), language)
            if response:
                return Decision(kind=DecisionKind.ACK, reason='tool-result', response=response)
        return Decision(kind=DecisionKind.PASS, reason='tool-result')
    if last.get('role') != 'user':
        return Decision(kind=DecisionKind.PASS, reason='awaiting-result')
    utterance = _text_content(last.get('content'))
    if not utterance:
        return Decision(kind=DecisionKind.PASS, reason='no-utterance')
    # Preserve internal punctuation and do not turn a question into an action.
    utterance = utterance.strip()
    if utterance.endswith('?'):
        return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
    utterance = utterance.rstrip('.!')
    command = language.parse(utterance)
    # Keep dotted names intact, but never fuzzy-match across a potential
    # sentence boundary: the extra text may change the scope of the command.
    allow_fuzzy = re.search(r'[.!?]\s', utterance) is None
    # `invalid-position` means a complete absolute-position phrase with a value
    # outside 0-100. Unknown, fractional or relative syntax is `outside-grammar`
    # and must still reach the LLM.
    position_out_of_range = command.reason == 'invalid-position'
    if command.reason and not position_out_of_range:
        kind = DecisionKind.CLARIFY if command.reason == 'negation' else DecisionKind.PASS
        return Decision(kind=kind, reason=command.reason)
    intent = command.intent
    if command.percentage is not None:
        if intent not in ('open', 'close', 'position', 'brightness'):
            return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
        if intent != 'brightness':
            intent = 'position'
    tail = command.target.split()
    if tool_choice == 'none':
        return Decision(kind=DecisionKind.PASS, reason='tool-choice-none')
    # Only a new device action needs a catalog; tool-result ACKs do not.
    if catalog is None or not catalog.is_recognized:
        return Decision(kind=DecisionKind.PASS, reason='catalog-unavailable')
    name_index, area_index = _indexes(catalog, language)
    resolve = partial(_resolve, name_index, catalog, language=language)

    # Try the complete name first (it may contain "w"). Otherwise separate an
    # explicit area. Never let domain filtering hide a name collision.
    name_text = ' '.join(tail)
    marker = next((i for i, word in enumerate(tail)
                   if normalize(word) in language.area_prepositions), None)
    # A fuzzy candidate for the entire phrase must not swallow a spoken area.
    targets = resolve(name_text, allow_fuzzy=allow_fuzzy and not (
        intent == 'brightness' and marker is not None))
    explicit_area = not targets and marker is not None
    area_text = default_area
    if explicit_area:
        name_text = ' '.join(tail[:marker])
        targets = resolve(name_text, allow_fuzzy=allow_fuzzy)
        area_text = ' '.join(tail[marker + 1:])
        if not area_text:
            return Decision(kind=DecisionKind.CLARIFY, reason='missing-area')
    if area_text:
        area_only = (intent == 'brightness' and explicit_area and not targets
                     and language.is_generic_light(name_text))
        allowed = _index_match(area_index, normalize(area_text), language.lemma(area_text),
                               allow_fuzzy=allow_fuzzy and not area_only) or set()
        if area_only:
            return _area_brightness(catalog, allowed, command.percentage, tools, tool_choice)
        if targets:
            targets = [(i, entity) for i, entity in targets if i in allowed]
            if not targets:
                return Decision(kind=DecisionKind.PASS, reason='area-mismatch')
    if not targets:
        if position_out_of_range or command.intent == 'position':
            # "Set brightness ..." and other parameterized domains are outside
            # the cover-position grammar. Preserve the complete request for LLM.
            return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
        return Decision(kind=DecisionKind.PASS, reason='no-target')
    if len(targets) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-target')
    entity = targets[0][1]
    if entity.domain not in _DOMAINS[intent]:
        reason = 'outside-grammar' if position_out_of_range else 'unsupported-domain'
        return Decision(kind=DecisionKind.PASS, reason=reason)
    # A joined area scalar cannot establish one unambiguous HA selector.
    areas = _split_raw(entity.areas) if entity.areas else []
    if len(areas) > 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-area')
    area = areas[0] if areas else None
    # An alias can match uniquely while its primary name collides with a
    # different row. Validate the selectors we will actually send to HA.
    canonical = normalize(_canonical(entity.names) or '')
    # Compare literal aliases only, not shortened search hints or lemmas.
    selector_matches = [
        other for other in catalog.entities
        if other.domain == entity.domain
        and canonical in _explicit_aliases(other.names)
        and (area is None or normalize(area) in _explicit_aliases(other.areas))
    ]
    if len(selector_matches) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-target')
    if position_out_of_range:
        return Decision(kind=DecisionKind.CLARIFY, reason='invalid-position')
    arguments = {'name': _canonical(entity.names), 'domain': [entity.domain]}
    if area:
        arguments['area'] = area
    if command.percentage is not None:
        arguments[intent] = command.percentage
    return _device_action(intent, arguments, tools, tool_choice)


def decide_request(body, *, language):
    """Route a normalized HA chat request using its advertised tools and catalog."""
    messages = body['messages']
    return decide(messages, parse_overview(messages), body.get('tools'),
                  tool_choice=body.get('tool_choice'), language=language)


def clarify_text(reason, language=None):
    """Speakable clarification in the explicitly configured language."""
    return (language or default_language()).clarify(reason)


def warmup(language=None):
    """Load only the selected language's dictionaries before accepting traffic."""
    (language or default_language()).warmup()
