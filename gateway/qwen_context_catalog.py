#!/usr/bin/env python3
"""Typed HA entity-context catalog + derived audit summary.

Parses only Home Assistant's stock "Static Context" entity overview — a single,
line-anchored heading inside a `system` message followed by a flat YAML sequence
of string-keyed mappings (names/domain/areas) — into an immutable, sanitized
`Catalog`. The audit summary is a derived view (`summarize`); the catalog itself
carries provenance (trust/completeness/freshness) and never stores names beyond
the literal scalars it was handed. stdlib only; no new dependency.

For this source the conservative values are fixed: `entity_ids_present=False`,
`trusted=False`, `complete='unknown'`, `freshness='unknown'`. The stock overview
discards IDs, is not a trusted registry and may be cached. Row counts are only
meaningful when the format was correctly read.
"""
from __future__ import annotations

from dataclasses import dataclass
import enum
import hashlib
import json

HEADING = 'Static Context: An overview of the areas and the devices in this smart home:'

_ALLOWED_KEYS = frozenset(('names', 'domain', 'areas'))
_MAX_ROWS = 5000
_MAX_LINE = 64 * 1024

# YAML indicators the producer never emits unquoted at the start of a string.
_INDICATOR_LEAD = frozenset('&*!@`|>%{}[]#?,-:\'"')
_YAML_NATIVE = frozenset(('null', '~', 'true', 'false', 'yes', 'no', 'on', 'off',
                          '.nan', '.inf', '-.inf', '+.inf'))


class Status(str, enum.Enum):
    NONE = 'none'
    UNRECOGNIZED = 'unrecognized'
    RECOGNIZED = 'recognized'


@dataclass(frozen=True)
class Entity:
    names: str            # literal comma-joined scalar, lossless
    domain: str
    areas: str | None     # literal scalar, or None when the field is absent


@dataclass(frozen=True)
class Provenance:
    source: str             # 'static-overview'
    trusted: bool           # False — system prose is not authentication
    complete: str           # 'unknown'
    freshness: str          # 'unknown'
    entity_ids_present: bool  # False
    joined_scalars: bool    # True — names/areas are comma-joined, not arrays


@dataclass(frozen=True)
class Catalog:
    status: Status
    entities: tuple[Entity, ...]
    provenance: Provenance
    detail: str | None      # reason code for UNRECOGNIZED


_STATIC_PROVENANCE = Provenance(
    source='static-overview', trusted=False, complete='unknown',
    freshness='unknown', entity_ids_present=False, joined_scalars=True)


def _parse_scalar(raw):
    """Parse one producer string scalar; return str, or None when unsupported."""
    if raw == '':
        return None  # bare "key:" is YAML null/nested, not a producer string
    if raw.startswith('"'):
        if len(raw) < 2 or not raw.endswith('"'):
            return None
        body = raw[1:-1]
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == '\\':
                if i + 1 >= len(body) or body[i + 1] not in ('"', '\\'):
                    return None
                out.append(body[i + 1])
                i += 2
            elif ch == '"':
                return None  # unescaped quote inside a double-quoted scalar
            else:
                out.append(ch)
                i += 1
        return ''.join(out)
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            return None
        return raw[1:-1].replace("''", "'")
    # Plain scalar: reject anything PyYAML would have quoted as a non-string.
    if raw[0] in _INDICATOR_LEAD:
        return None
    if '\t' in raw or ': ' in raw or ' #' in raw:
        return None
    if raw.lower() in _YAML_NATIVE:
        return None
    try:
        float(raw)
        return None
    except ValueError:
        pass
    return raw


def _parse_entry(rest):
    """Parse the `key: value` part of a sequence item or mapping line."""
    idx = rest.find(':')
    if idx <= 0:
        return 'malformed-entry', None, None
    key = rest[:idx]
    if key.strip() != key:
        return 'malformed-entry', None, None
    if key not in _ALLOWED_KEYS:
        return 'unexpected-key', None, None
    val = rest[idx + 1:]
    if not val.startswith(' '):
        return 'malformed-scalar', None, None
    val = val[1:]
    if val.startswith(' ') or val.startswith('\t'):
        return 'malformed-scalar', None, None
    return None, key, val


def _parse_sequence(block):
    """Bounded parse of the flat YAML sequence; returns (reason, rows)."""
    rows = []
    for raw_line in block.split('\n'):
        if len(raw_line) > _MAX_LINE:
            return 'oversized', ()
        stripped = raw_line.strip()
        if stripped == '':
            continue
        if stripped == '[]' and not rows:
            return None, ()  # empty exposed set, dumped as a flow list
        if raw_line.startswith('- '):
            if len(rows) >= _MAX_ROWS:
                return 'oversized', ()
            reason, key, value = _parse_entry(raw_line[2:])
            if reason:
                return reason, ()
            scalar = _parse_scalar(value)
            if scalar is None:
                return 'malformed-scalar', ()
            rows.append({key: scalar})
        elif raw_line.startswith('  ') and not raw_line.startswith('   '):
            if not rows:
                return 'mapping-before-item', ()
            reason, key, value = _parse_entry(raw_line[2:])
            if reason:
                return reason, ()
            if key in rows[-1]:
                return 'duplicate-key', ()
            scalar = _parse_scalar(value)
            if scalar is None:
                return 'malformed-scalar', ()
            rows[-1][key] = scalar
        else:
            break  # first non-sequence line ends the overview block
    if not rows:
        return 'no-sequence', ()
    return None, rows


def _to_entities(rows):
    entities = []
    for row in rows:
        if 'names' not in row or 'domain' not in row:
            return 'missing-required-key', ()
        entities.append(Entity(names=row['names'], domain=row['domain'],
                               areas=row.get('areas')))
    return None, tuple(entities)


def parse_overview(messages):
    """Parse the HA static entity overview into an immutable, typed Catalog.

    Never raises and never returns data beyond the literal scalars HA sent.
    """
    if not isinstance(messages, list):
        return Catalog(status=Status.NONE, entities=(), provenance=_STATIC_PROVENANCE, detail=None)
    block = None
    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'system':
            continue
        content = message.get('content')
        if not isinstance(content, str):
            continue
        start = 0
        while True:
            idx = content.find(HEADING, start)
            if idx == -1:
                break
            end = idx + len(HEADING)
            line_start = idx == 0 or content[idx - 1] == '\n'
            line_end = end == len(content) or content[end] == '\n'
            if line_start and line_end:
                count += 1
                if block is None:
                    # Keep the exact line-anchored occurrence, not content.find(),
                    # which could match the same words earlier in prose.
                    block = content[end:]
            start = end
    if count == 0:
        return Catalog(status=Status.NONE, entities=(), provenance=_STATIC_PROVENANCE, detail=None)
    if count != 1:
        return Catalog(status=Status.UNRECOGNIZED, entities=(), provenance=_STATIC_PROVENANCE, detail='duplicate-heading')
    reason, rows = _parse_sequence(block)
    if reason:
        return Catalog(status=Status.UNRECOGNIZED, entities=(), provenance=_STATIC_PROVENANCE, detail=reason)
    reason, entities = _to_entities(rows)
    if reason:
        return Catalog(status=Status.UNRECOGNIZED, entities=(), provenance=_STATIC_PROVENANCE, detail=reason)
    return Catalog(status=Status.RECOGNIZED, entities=entities, provenance=_STATIC_PROVENANCE, detail=None)


def summarize(catalog):
    """Derive the safe audit summary dict from a typed Catalog."""
    if catalog.status == Status.NONE:
        context_format = 'none'
    elif catalog.status == Status.UNRECOGNIZED:
        context_format = 'unrecognized'
    else:
        context_format = 'recognized-' + catalog.provenance.source
    recognized = catalog.status == Status.RECOGNIZED
    return {
        'context_format': context_format,
        'heading_present': catalog.status != Status.NONE,
        'overview_present': recognized,
        'overview_rows': len(catalog.entities) if recognized else None,
        'cover_rows': sum(1 for e in catalog.entities if e.domain == 'cover') if recognized else None,
        'entity_ids_present': catalog.provenance.entity_ids_present,
        'trusted_catalog': catalog.provenance.trusted,
        'complete': catalog.provenance.complete,
        'freshness': catalog.provenance.freshness,
        'detail': catalog.detail,
    }


def audit_context(messages):
    """Backward-compatible audit summary: summarize(parse_overview(messages))."""
    return summarize(parse_overview(messages))


def audit_request(body):
    """Fingerprint normalized input without storing prompts, names or schemas.

    These are JSON fingerprints, not hashes of the native rendered token stream.
    Canonical hashes ignore mapping-key order but preserve nested array order.
    Only tools_set_hash ignores the outer tools array order.
    """
    def encoded(value, canonical=True):
        return json.dumps(value, ensure_ascii=False, sort_keys=canonical,
                          separators=(',', ':')).encode('utf-8')

    def digest(value, canonical=True):
        return hashlib.sha256(encoded(value, canonical)).hexdigest()

    messages = body['messages']
    tools = body.get('tools')
    catalog = parse_overview(messages)
    entities = ([{'names': e.names, 'domain': e.domain, 'areas': e.areas}
                 for e in catalog.entities] if catalog.status == Status.RECOGNIZED else None)
    systems = [m for m in messages if isinstance(m, dict) and m.get('role') == 'system']
    return {
        'schema_version': 1, 'service': 'context-audit',
        'request_hash': digest(body, canonical=False),
        'tools_wire_hash': digest(tools, canonical=False),
        'tools_order_hash': digest(tools),
        'tools_set_hash': (digest(sorted(encoded(tool).decode('utf-8') for tool in tools))
                           if isinstance(tools, list) else digest(tools)),
        'system_hash': digest(systems),
        'catalog_hash': digest(entities) if entities is not None else None,
        'message_count': len(messages),
        'message_roles': [m['role'] if isinstance(m, dict) and m.get('role') in
                          ('system', 'developer', 'user', 'assistant', 'tool', 'function')
                          else 'unknown' for m in messages[:256]],
        'message_hashes': [digest(m) for m in messages[:256]],
        'message_fingerprints_complete': len(messages) <= 256,
        'tool_count': len(tools) if isinstance(tools, list) else 0,
        **summarize(catalog),
    }


if __name__ == '__main__':
    import json
    print(json.dumps(audit_context([{'role': 'system', 'content':
        HEADING + '\n- names: Roleta\n  domain: cover\n  areas: Sypialnia'}])))
