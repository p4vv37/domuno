#!/usr/bin/env python3
"""Run command_router.decide against synthetic Home Assistant data.

No board, Docker, model or network is used. Choose a scenario with:

    python tools/debug_router.py --list
    python tools/debug_router.py toolcall

Set a breakpoint in gateway/command_router.py and start the scenario from the
VS Code Run and Debug panel.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))

from context_catalog import HEADING, parse_overview  # noqa: E402
from command_router import decide  # noqa: E402
from languages import get_language  # noqa: E402


def make_catalog(rows):
    if rows is None:
        return parse_overview([])
    text = HEADING + '\n' + '\n'.join(
        '- names: %s\n  domain: %s%s' % (
            name, domain, '\n  areas: ' + area if area else '')
        for name, domain, area in rows)
    return parse_overview([{'role': 'system', 'content': text}])


def schema(name, *, position=False):
    properties = {
        'name': {'type': 'string'},
        'area': {'type': 'string'},
        'domain': {'type': 'array', 'items': {'type': 'string'}},
    }
    required = ['name', 'domain']
    if position:
        properties['position'] = {'type': 'integer', 'minimum': 0, 'maximum': 100}
        required.append('position')
    return {'type': 'function', 'function': {
        'name': name,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False}}}


def tools(*, position=True):
    result = [schema('intent__HassTurnOn'), schema('intent__HassTurnOff')]
    if position:
        result.append(schema('intent__HassSetPosition', position=True))
    return result


ACK_MESSAGES = [
    {'role': 'user', 'content': 'Włącz Lampa w Salon'},
    {'role': 'assistant', 'tool_calls': [{
        'id': 'call_debug', 'type': 'function',
        'function': {'name': 'intent__HassTurnOn', 'arguments': '{}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_debug',
     'content': '{"success": true}'},
]

SCENARIOS = {
    'toolcall': {
        'description': 'existing light in an exact area -> TOOLCALL',
        'messages': [{'role': 'user', 'content': 'Włącz Lampa w Salon'}],
        'rows': [('Lampa', 'light', 'Salon')],
    },
    'outside-grammar': {
        'description': 'two actions in one sentence -> PASS to the LLM',
        'messages': [{'role': 'user', 'content': 'Włącz Lampa i wyłącz Wiatrak'}],
        'rows': [('Lampa', 'light', 'Salon'), ('Wiatrak', 'fan', 'Salon')],
    },
    'negation': {
        'description': 'explicit negation -> local CLARIFY',
        'messages': [{'role': 'user', 'content': 'Nie włącz Lampa'}],
        'rows': [('Lampa', 'light', 'Salon')],
    },
    'ambiguous': {
        'description': 'same name in two areas -> CLARIFY',
        'messages': [{'role': 'user', 'content': 'Włącz Lampa'}],
        'rows': [('Lampa', 'light', 'Salon'), ('Lampa', 'light', 'Gabinet')],
    },
    'no-target': {
        'description': 'unknown device -> PASS to the LLM',
        'messages': [{'role': 'user', 'content': 'Włącz XYZ'}],
        'rows': [('Lampa', 'light', 'Salon')],
    },
    'closest-match': {
        'description': 'larger STT name/area typo -> unique TOOLCALL',
        'messages': [{'role': 'user', 'content': 'Otwórz rożoleta w sypialnym.'}],
        'rows': [('roleta  Rolety', 'cover', 'Sypialnia')],
    },
    'prefix': {
        'description': 'unknown leading phrase before the verb -> TOOLCALL',
        'messages': [{'role': 'user', 'content': 'W tej chwili otwórz Markiza'}],
        'rows': [('Markiza', 'cover', 'Atrium')],
    },
    'blocked-prefix': {
        'description': 'condition/question prefix -> PASS to the LLM',
        'messages': [{'role': 'user', 'content': 'Może otwórz Markiza'}],
        'rows': [('Markiza', 'cover', 'Atrium')],
    },
    'position': {
        'description': 'absolute cover position -> TOOLCALL',
        'messages': [{'role': 'user', 'content': 'Otwórz Markiza w Atrium na 50%'}],
        'rows': [('Markiza', 'cover', 'Atrium')],
    },
    'position-unavailable': {
        'description': 'cover position without a position tool -> CLARIFY',
        'messages': [{'role': 'user', 'content': 'Otwórz Markiza w Atrium na 50%'}],
        'rows': [('Markiza', 'cover', 'Atrium')],
        'tools': tools(position=False),
    },
    'invalid-position': {
        'description': 'absolute position outside 0-100 -> CLARIFY',
        'messages': [{'role': 'user', 'content': 'Otwórz Markiza w Atrium na 150%'}],
        'rows': [('Markiza', 'cover', 'Atrium')],
    },
    'relative-position': {
        'description': 'relative cover change -> PASS to the LLM',
        'messages': [{'role': 'user', 'content': 'Otwórz Markiza o 20%'}],
        'rows': [('Markiza', 'cover', 'Atrium')],
    },
    'ack': {
        'description': 'single explicit tool success -> ACK',
        'messages': ACK_MESSAGES,
        'rows': [('Lampa', 'light', 'Salon')],
    },
    'english': {
        'description': 'English command with the English strategy -> TOOLCALL',
        'messages': [{'role': 'user', 'content': 'Turn on the Desk lamp in the Study'}],
        'rows': [('Desk lamp', 'light', 'Study')],
        'language': 'en',
    },
    'catalog-unavailable': {
        'description': 'no HA overview in messages -> PASS',
        'messages': [{'role': 'user', 'content': 'Włącz Lampa'}],
        'rows': None,
    },
}


def run(name):
    spec = SCENARIOS[name]
    language = get_language(spec.get('language', 'pl'))
    decision = decide(
        spec['messages'],
        make_catalog(spec.get('rows')),
        spec.get('tools', tools()),
        language=language,
    )
    print('scenario: %s' % name)
    print('what:     %s' % spec['description'])
    print('kind:     %s' % decision.kind.value)
    print('reason:   %s' % decision.reason)
    if decision.tool_call is not None:
        print('tool:     %s' % decision.tool_call.name)
        print('args:     %s' % json.dumps(decision.tool_call.arguments, ensure_ascii=False))
    if decision.response is not None:
        print('response: %s' % decision.response)
    return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', nargs='?', default='toolcall',
                        help='scenario name; use --list')
    parser.add_argument('--list', action='store_true', help='list scenarios and exit')
    args = parser.parse_args()
    if args.list:
        for name, spec in SCENARIOS.items():
            print('%-22s %s' % (name, spec['description']))
        return
    if args.scenario not in SCENARIOS:
        parser.error('unknown scenario: %s (use --list)' % args.scenario)
    run(args.scenario)


if __name__ == '__main__':
    main()
