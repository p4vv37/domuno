#!/usr/bin/env python3
"""Pure Polish HA command router; proposes tools, never executes actions.

Names and areas come from the current HA overview; arguments must satisfy the
advertised JSON Schema. Only simple on/off and open/close commands are routed.
Other utterances pass to the LLM. Ambiguous targets ask for clarification.
"""
from __future__ import annotations

from dataclasses import dataclass
import enum
import json
import re

from qwen_context_catalog import Status

_DIACRITICS = str.maketrans('ąćęłńóśźżĄĆĘŁŃÓŚŹŻ', 'acelnoszzACELNOSZZ')

_CLOSE_INTENTS = frozenset(('zamknij', 'zamknijcie', 'zamykaj', 'zamknac',
                            'zaslon', 'zasun'))
_OPEN_INTENTS = frozenset(('otworz', 'otworzcie', 'otwieraj', 'otworzyc',
                           'odslon', 'odsun'))
_TOOL_NAMES_BY_INTENT = {
    'close': ('intent__HassTurnOff', 'HassTurnOff'),
    'open': ('intent__HassTurnOn', 'HassTurnOn'),
    'off': ('intent__HassTurnOff', 'HassTurnOff'),
    'on': ('intent__HassTurnOn', 'HassTurnOn'),
}
_ON_INTENTS = frozenset(('wlacz', 'wlaczcie', 'zapal', 'zaswiec', 'uruchom'))
_OFF_INTENTS = frozenset(('wylacz', 'wylaczcie', 'zgas'))
_DOMAINS = {
    'open': frozenset(('cover',)),
    'close': frozenset(('cover',)),
    'on': frozenset(('light', 'switch', 'fan', 'input_boolean')),
    'off': frozenset(('light', 'switch', 'fan', 'input_boolean')),
}
# Locks, alarms and parameterized actions intentionally stay with the LLM.
_ACTION_TOOL_NAMES = frozenset(name for names in _TOOL_NAMES_BY_INTENT.values() for name in names)
# HA aliases are joined by comma-space, but some builds join with runs of spaces.
_ALIAS_SEP = re.compile(r'\s*,\s*|\s{2,}')


def normalize(text):
    """Lowercase, strip Polish diacritics and collapse whitespace."""
    return ' '.join(text.translate(_DIACRITICS).lower().split())


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


try:
    import simplemma as _simplemma
except ImportError:  # optional; fall back to plain normalization without it
    _simplemma = None


def _lemma(text):
    """Lemmatize (Polish) then normalize; falls back to normalize alone."""
    if _simplemma is not None:
        text = ' '.join(_simplemma.lemmatize(tok, lang='pl') for tok in text.split())
    return normalize(text)


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
    clarification: str | None = None
    response: str | None = None
    tool_call: ToolCall | None = None
    matched_targets: int | None = None
    matched_areas: int | None = None


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
    """Split a joined alias scalar on comma or runs of spaces (not single spaces)."""
    return [p for p in _ALIAS_SEP.split(name_scalar.strip()) if p]


def _aliases(names):
    """Split a joined scalar into raw-normalized + lemmatized candidate aliases.

    Keeping both forms lets the <= 1 edit pass recover small STT errors like
    "urolety"/"relety" against the raw alias "rolety", while the lemmatized form
    still handles "rolety" -> "roleta".
    """
    out = []
    for x in _split_raw(names):
        raw = normalize(x)
        if raw:
            out.append(raw)
        lem = _lemma(x)
        if lem and lem != raw:
            out.append(lem)
    return tuple(out)


def _canonical(name_scalar):
    """First (primary) alias of a joined scalar, or None when empty."""
    parts = _split_raw(name_scalar)
    return parts[0] if parts else None


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


def _ack_response(content):
    """Confirm only explicit success, never infer it from arbitrary tool prose."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get('data', {})
    if (data.get('error') or data.get('success') is False
            or isinstance(inner, dict) and (inner.get('error') or inner.get('failed'))):
        return 'Nie udało się.'
    if (data.get('success') is True
            or isinstance(inner, dict) and inner.get('success')):
        return 'Wykonano'
    return None


def _detect_intent(tokens):
    for index, token in enumerate(tokens):
        for intent, words in (('close', _CLOSE_INTENTS), ('open', _OPEN_INTENTS),
                              ('on', _ON_INTENTS), ('off', _OFF_INTENTS)):
            if token in words:
                return intent, index
    return None, -1


def _match_entities(norm_target, entities):
    exact = [e for e in entities if norm_target in _aliases(e.names)]
    if exact:
        return exact
    return [e for e in entities if any(_within_one(norm_target, a) for a in _aliases(e.names))]


def _match_area(norm_area, entity):
    if entity.areas is None:
        return False
    aliases = _aliases(entity.areas)
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

    Unsupported constraints fail closed: the request goes to the model. This
    deliberately avoids implementing general JSON Schema or fetching references.
    """
    allowed = {'type', 'enum', 'const', 'properties', 'required', 'items',
               'additionalProperties', 'description', 'title', 'default'}
    if not isinstance(schema, dict) or schema.keys() - allowed:
        return False
    types = {'object': dict, 'array': list, 'string': str}
    kind = schema.get('type')
    if not isinstance(kind, str) or kind not in types or not isinstance(value, types[kind]):
        return False
    if 'enum' in schema and value not in schema['enum']:
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


def _arguments(function, entity, area):
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
    if area:
        if 'area' not in properties:
            return None
        arguments['area'] = area
    if not _schema_accepts(arguments, schema):
        return None
    return arguments


def decide(messages, catalog, tools, default_area=None, tool_choice=None):
    """Route the current turn only; never replay an action from older history."""
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
            response = _ack_response(_last_tool_content(messages))
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
    tokens = normalize(utterance).split()
    intent, index = _detect_intent(tokens)
    if intent is None:
        return Decision(kind=DecisionKind.PASS, reason='no-command')
    if 'nie' in tokens[:index]:
        return Decision(kind=DecisionKind.CLARIFY, reason='negation')
    # Restrict grammar to an imperative, optionally preceded by "proszę".
    # Questions, conditions, multiple actions and parameters go to the model.
    if tokens[:index] not in ([], ['prosze']):
        return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
    tail = utterance.split()[index + 1:]
    if (any(t in ('i', 'oraz', 'potem', 'jesli', 'za', 'na', 'do', 'nie') for t in tokens[index + 1:])
            or _detect_intent(tokens[index + 1:])[0] is not None):
        return Decision(kind=DecisionKind.PASS, reason='outside-grammar')
    if tool_choice == 'none':
        return Decision(kind=DecisionKind.PASS, reason='tool-choice-none')
    if getattr(catalog, 'status', None) != Status.RECOGNIZED:
        return Decision(kind=DecisionKind.CLARIFY, reason='catalog-unavailable')

    # Try the complete name first (it may contain "w"). Otherwise separate an
    # explicit area. Never let domain filtering hide a name collision.
    targets = _match_entities(_lemma(' '.join(tail)), catalog.entities) if tail else []
    area_text = default_area
    if not targets:
        for marker, word in enumerate(tail):
            if normalize(word) in ('w', 'we'):
                targets = _match_entities(_lemma(' '.join(tail[:marker])), catalog.entities)
                area_text = ' '.join(tail[marker + 1:])
                if not area_text:
                    return Decision(kind=DecisionKind.CLARIFY, reason='missing-area')
                break
    if not targets:
        return Decision(kind=DecisionKind.CLARIFY, reason='no-target')
    if area_text:
        norm_area = _lemma(area_text)
        exact = [e for e in targets if e.areas and norm_area in _aliases(e.areas)]
        targets = exact or [e for e in targets if _match_area(norm_area, e)]
        if not targets:
            return Decision(kind=DecisionKind.CLARIFY, reason='area-mismatch')
    if len(targets) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-target', matched_targets=len(targets))
    entity = targets[0]
    if entity.domain not in _DOMAINS[intent]:
        return Decision(kind=DecisionKind.PASS, reason='unsupported-domain')
    # A joined area scalar cannot establish one unambiguous HA selector.
    areas = _split_raw(entity.areas) if entity.areas else []
    if len(areas) > 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-area')
    area = areas[0] if areas else None
    # An alias can match uniquely while its primary name collides with a
    # different row. Validate the selectors we will actually send to HA.
    canonical = normalize(_canonical(entity.names) or '')
    collisions = [e for e in catalog.entities
                  if e.domain == entity.domain and canonical in _aliases(e.names)
                  and (area is None or e.areas and normalize(area) in _aliases(e.areas))]
    if len(collisions) != 1:
        return Decision(kind=DecisionKind.CLARIFY, reason='ambiguous-target')
    function = _action_tool(intent, tools, tool_choice)
    if function is None:
        return Decision(kind=DecisionKind.PASS, reason='tool-unavailable')
    arguments = _arguments(function, entity, area)
    if arguments is None:
        return Decision(kind=DecisionKind.PASS, reason='unsupported-schema')
    return Decision(kind=DecisionKind.TOOLCALL,
                    tool_call=ToolCall(name=function['name'], arguments=arguments),
                    matched_targets=1, matched_areas=len(areas))


def sanitize(decision):
    """Metadata only: never log device names, arguments or conversation text."""
    return {
        'kind': decision.kind.value,
        'reason': decision.reason,
        'matched_targets': decision.matched_targets,
        'matched_areas': decision.matched_areas,
        'tool_name': decision.tool_call.name if decision.tool_call else None,
    }


_CLARIFY_TEXT = {
    'negation': 'OK, nie wykonuję tej akcji.',
    'catalog-unavailable': 'Nie mam teraz listy urządzeń.',
    'no-target': 'Nie znalazłem takiego urządzenia. Podaj dokładniejszą nazwę.',
    'ambiguous-area': 'Urządzenie ma kilka obszarów. Doprecyzuj jego nazwę w HA.',
    'missing-area': 'W którym pomieszczeniu mam to zrobić?',
    'area-mismatch': 'Nie ma takiego urządzenia w tym pomieszczeniu.',
    'ambiguous-target': 'Jest kilka pasujących urządzeń. Podaj dokładniejszą nazwę.',
}


def clarify_text(reason):
    """Speakable Polish clarification for a CLARIFY reason."""
    return _CLARIFY_TEXT.get(reason, 'Nie mogę jednoznacznie wykonać tego polecenia.')
