#!/usr/bin/env python3
"""Language-independent HA command router; proposes tools, never executes actions.

Names and areas come from the current HA overview; arguments must satisfy the
advertised JSON Schema. Simple on/off, open/close and absolute cover positions
are routed.
Other utterances pass to the LLM. Ambiguous targets ask for clarification.

Matching builds a per-catalog index: explicit spellings first, then inflected
forms provided by the language strategy, then lemmas, then a one-edit recovery
for STT noise. Singular and plural forms share target resolution. The router
proposes an action only for one matching device after area filtering.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache, partial
import enum
import json
import math
import re

from qwen_context_catalog import Status
from qwen_language import default_language, normalize


_TOOL_NAMES_BY_INTENT = {
    'close': ('intent__HassTurnOff', 'HassTurnOff'),
    'open': ('intent__HassTurnOn', 'HassTurnOn'),
    'off': ('intent__HassTurnOff', 'HassTurnOff'),
    'on': ('intent__HassTurnOn', 'HassTurnOn'),
    'position': ('intent__HassSetPosition', 'HassSetPosition'),
}
_DOMAINS = {
    'open': frozenset(('cover',)),
    'close': frozenset(('cover',)),
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


def _within_one(a, b):
    """True when Levenshtein distance is <= 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif not skipped:
            skipped = True
            j += 1
        else:
            return False
    return True


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


def _aliases(names, language, *, name_parts=False):
    """Split a joined scalar into raw-normalized + lemmatized candidate aliases.

    Keeping both forms lets the <= 1 edit pass recover small STT errors like
    "urolety"/"relety" against the raw alias "rolety", while the lemmatized form
    still handles "rolety" -> "roleta".
    """
    out = []
    for x in (_name_spellings(names) if name_parts else _split_raw(names)):
        raw = normalize(x)
        if raw:
            out.append(raw)
        lem = language.lemma(x)
        if lem and lem != raw:
            out.append(lem)
    return tuple(out)


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
        if language.has_lemma:
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
            if abs(len(key) - len(text)) > 1 or not _within_one(key, text):
                continue
            found.update(value)
        if found:
            return found
    if lemma and lemma != text:
        found = {entity for key, value in index.lemmas.items()
                 if abs(len(key) - len(lemma)) <= 1 and _within_one(key, lemma)
                 for entity in value}
        if found:
            return found
    return None


def _index_match(index, text, lemma):
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
    return _fuzzy(index, text, lemma)


def _resolve(index, catalog, text, *, language):
    """Resolve a spoken phrase to matching (index, entity) pairs."""
    norm = normalize(text)
    if not norm:
        return []
    lemma = language.lemma(text) if language.has_lemma else norm
    hit = _index_match(index, norm, lemma)
    if hit is None:
        return []
    return [(i, catalog.entities[i]) for i in sorted(hit)]


def _resolve_legacy(catalog, text, *, language):
    """Pre-index path, kept only so tests can compare both matchers."""
    return [(i, catalog.entities[i])
            for i in _match_entities(language.lemma(text), catalog.entities, language)]


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


def _match_entities(norm_target, entities, language):
    """Legacy lexical matcher: indices of entities whose aliases match."""
    exact = [i for i, e in enumerate(entities) if norm_target in _aliases(e.names, language, name_parts=True)]
    if exact:
        return exact
    return [i for i, e in enumerate(entities)
            if any(_within_one(norm_target, a) for a in _aliases(e.names, language, name_parts=True))]


def _match_area(norm_area, entity, language):
    if entity.areas is None:
        return False
    aliases = _aliases(entity.areas, language)
    return norm_area in aliases or any(_within_one(norm_area, a) for a in aliases)


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


def _schema_accepts(value, schema):
    """Validate the small inline schema subset used by HA selectors.

    Unsupported constraints fail closed: no tool call is proposed. This
    deliberately avoids implementing general JSON Schema or fetching references.
    """
    allowed = {'type', 'enum', 'const', 'properties', 'required', 'items',
               'additionalProperties', 'description', 'title', 'default', 'minimum', 'maximum',
               'allOf'}
    if not isinstance(schema, dict) or schema.keys() - allowed:
        return False
    if 'allOf' in schema:
        branches = schema['allOf']
        if (not isinstance(branches, list) or not branches
                or not all(_schema_accepts(value, branch) for branch in branches)):
            return False
        # HA combines integer and range constraints this way. Also validate
        # sibling constraints, rather than accepting solely on the branches.
        remaining = {key: item for key, item in schema.items()
                     if key not in ('allOf', 'description', 'title', 'default')}
        return not remaining or _schema_accepts(value, remaining)
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'number': (int, float)}
    kind = schema.get('type')
    if not isinstance(kind, str) or kind not in types or not isinstance(value, types[kind]):
        return False
    if kind in ('integer', 'number'):
        if isinstance(value, bool) or not math.isfinite(value):
            return False
    for bound, compare in (('minimum', lambda a, b: a >= b), ('maximum', lambda a, b: a <= b)):
        if bound in schema:
            limit = schema[bound]
            if (kind not in ('integer', 'number') or type(limit) not in (int, float)
                    or not math.isfinite(limit) or not compare(value, limit)):
                return False
    if 'enum' in schema:
        enum = schema['enum']
        if not isinstance(enum, list) or value not in enum:
            return False
    if 'const' in schema and value != schema['const']:
        return False
    if kind == 'array':
        return all(_schema_accepts(item, schema.get('items')) for item in value)
    if kind == 'object':
        properties = schema.get('properties', {})
        required = schema.get('required', [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or not all(isinstance(key, str) and key in value for key in required)):
            return False
        return all(key in properties and _schema_accepts(item, properties[key])
                   for key, item in value.items())
    return True


def _arguments(function, entity, area, position=None):
    """Keep canonical selectors and validate against the actual advertised tool."""
    schema = function.get('parameters')
    if not isinstance(schema, dict):
        return None
    properties = schema.get('properties', {})
    # Domain is essential: omitting it could select a same-named device of a
    # different kind. Name is essential because area alone can target a group.
    if not isinstance(properties, dict) or not {'name', 'domain'} <= properties.keys():
        return None
    arguments = {'name': _canonical(entity.names), 'domain': [entity.domain]}
    if position is not None:
        if 'position' not in properties:
            return None
        arguments['position'] = position
    if area:
        if 'area' not in properties:
            return None
        arguments['area'] = area
    if not _schema_accepts(arguments, schema):
        return None
    return arguments


def decide(messages, catalog, tools, default_area=None, tool_choice=None, matcher=None, language=None):
    """Route the current turn only; never replay an action from older history.

    `matcher` selects the resolution strategy: 'forms' uses the catalog form
    index, 'legacy' keeps the previous lexical matcher for A/B comparison, and
    None picks 'forms' when the selected strategy provides inflected forms.
    """
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
    # Strip terminal sentence punctuation only, preserving device names inside.
    utterance = utterance.strip().rstrip('.!?')
    command = language.parse(utterance)
    # `invalid-position` means a complete absolute-position phrase with a value
    # outside 0-100. Unknown, fractional or relative syntax is `outside-grammar`
    # and must still reach the LLM.
    position_out_of_range = command.reason == 'invalid-position'
    if command.reason and not position_out_of_range:
        kind = DecisionKind.CLARIFY if command.reason == 'negation' else DecisionKind.PASS
        return Decision(kind=kind, reason=command.reason)
    intent = command.intent
    if command.position is not None:
        intent = 'position'
    tail = command.target.split()
    if tool_choice == 'none':
        return Decision(kind=DecisionKind.PASS, reason='tool-choice-none')
    if getattr(catalog, 'status', None) != Status.RECOGNIZED:
        return Decision(kind=DecisionKind.PASS, reason='catalog-unavailable')
    mode = matcher or ('forms' if language.supports_forms else 'legacy')
    if mode == 'forms':
        name_index, area_index = _indexes(catalog, language)
        resolve = partial(_resolve, name_index, catalog, language=language)
    elif mode == 'legacy':
        resolve = partial(_resolve_legacy, catalog, language=language)
    else:
        raise ValueError('unknown matcher: %r' % (mode,))

    # Try the complete name first (it may contain "w"). Otherwise separate an
    # explicit area. Never let domain filtering hide a name collision.
    targets = resolve(' '.join(tail)) if tail else []
    area_text = default_area
    if not targets:
        for marker, word in enumerate(tail):
            if normalize(word) in language.area_prepositions:
                targets = resolve(' '.join(tail[:marker]))
                area_text = ' '.join(tail[marker + 1:])
                if not area_text:
                    return Decision(kind=DecisionKind.CLARIFY, reason='missing-area')
                break
    if not targets:
        if position_out_of_range or command.intent == 'position':
            # "Set brightness ..." and other parameterized domains are outside
            # the cover-position grammar. Preserve the complete request for LLM.
            return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
        return Decision(kind=DecisionKind.PASS, reason='no-target')
    if area_text:
        if mode == 'forms':
            norm_area = normalize(area_text)
            area_lemma = language.lemma(area_text) if language.has_lemma else norm_area
            hit = _index_match(area_index, norm_area, area_lemma)
            allowed = hit or set()
            targets = [(i, entity) for i, entity in targets if i in allowed]
        else:
            norm_area = language.lemma(area_text)
            exact = [(i, entity) for i, entity in targets
                     if entity.areas and norm_area in _aliases(entity.areas, language)]
            targets = exact or [(i, entity) for i, entity in targets
                                if _match_area(norm_area, entity, language)]
        if not targets:
            return Decision(kind=DecisionKind.PASS, reason='area-mismatch')
    if len(targets) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-target')
    entity = targets[0][1]
    if position_out_of_range:
        if entity.domain != 'cover':
            return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
    elif entity.domain not in _DOMAINS[intent]:
        return Decision(kind=DecisionKind.PASS, reason='unsupported-domain')
    # A joined area scalar cannot establish one unambiguous HA selector.
    areas = _split_raw(entity.areas) if entity.areas else []
    if len(areas) > 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-area')
    area = areas[0] if areas else None
    # An alias can match uniquely while its primary name collides with a
    # different row. Validate the selectors we will actually send to HA.
    canonical = normalize(_canonical(entity.names) or '')
    # Compare literal aliases only, not shortened search hints or lemmas.
    collisions = [other for other in catalog.entities
                  if other.domain == entity.domain
                  and canonical in _explicit_aliases(other.names)
                  and (area is None or other.areas
                       and normalize(area) in _explicit_aliases(other.areas))]
    if len(collisions) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-target')
    if position_out_of_range:
        return Decision(kind=DecisionKind.CLARIFY, reason='invalid-position')
    function = _action_tool(intent, tools, tool_choice)
    if function is None:
        if intent == 'position' and _action_tool('position', tools, None) is None:
            return Decision(kind=DecisionKind.CLARIFY, reason='position-unavailable')
        return Decision(kind=DecisionKind.PASS, reason='tool-unavailable')
    arguments = _arguments(function, entity, area, command.position)
    if arguments is None:
        return Decision(kind=DecisionKind.PASS, reason='unsupported-schema')
    return Decision(kind=DecisionKind.TOOLCALL,
                    tool_call=ToolCall(name=function['name'], arguments=arguments))


def clarify_text(reason, language=None):
    """Speakable clarification in the explicitly configured language."""
    return (language or default_language()).clarify(reason)


def warmup(language=None):
    """Load only the selected language's dictionaries before accepting traffic."""
    (language or default_language()).warmup()
