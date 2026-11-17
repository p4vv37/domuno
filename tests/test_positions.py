"""Absolute cover-position routing. Synthetic context/schemas, no HA actions."""
import copy
import importlib.util
import json
import sys
import unittest
from unittest.mock import patch

from test_gateway import catalog, get_language, overview, router, tools
import test_metrics as metrics_tests


def position_tools():
    return tools() + [{'type': 'function', 'function': {
        'name': 'intent__HassSetPosition',
        'parameters': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'area': {'type': 'string'},
            'domain': {'type': 'array', 'items': {'type': 'string'}},
            'position': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
            'required': ['position'], 'additionalProperties': False}}}]


class PositionTests(unittest.TestCase):
    def route(self, text, code='pl', rows=None, available=None, **kwargs):
        rows = rows or [('Markiza 2', 'cover', 'Atrium północne')]
        return router.decide([{'role': 'user', 'content': text}], overview(rows),
                             position_tools() if available is None else available,
                             language=get_language(code), **kwargs)

    def test_polish_absolute_positions_and_context_selectors(self):
        for verb in ('Otwórz', 'Zamknij', 'Ustaw', 'Ustaw pozycję'):
            for value in (0, 50, 100):
                with self.subTest(verb=verb, value=value):
                    decision = self.route(f'{verb} Markiza 2 w Atrium północne na {value}%.')
                    self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                    self.assertEqual(decision.tool_call.name, 'intent__HassSetPosition')
                    self.assertEqual(decision.tool_call.arguments, {
                        'name': 'Markiza 2', 'area': 'Atrium północne',
                        'domain': ['cover'], 'position': value})

    def test_polish_do_introduces_absolute_position(self):
        for value in (0, 50, 100):
            with self.subTest(value=value):
                decision = self.route(f'Zamknij Markiza 2 w Atrium północne do {value}%.')
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments['position'], value)
                self.assertEqual(decision.tool_call.arguments['area'], 'Atrium północne')
        decision = self.route('Zamknij Markiza 2 o 20%')
        self.assertIsNone(decision.tool_call)

    def test_english_absolute_positions_and_articles(self):
        rows = [('Curtain 2', 'cover', 'North wing')]
        for text in ('Open the Curtain 2 in the North wing to 50%',
                     'Close Curtain 2 in North wing at 50 percent',
                     'Set the Curtain 2 in North wing to 50 per cent'):
            with self.subTest(text=text):
                decision = self.route(text, 'en', rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments['position'], 50)
                self.assertEqual(decision.tool_call.arguments['area'], 'North wing')

    def test_percentages_do_not_become_area_text_and_names_keep_spaces(self):
        rows = [('Awning  Motor, Awning', 'cover', 'Zone 8')]
        for code, text in (('pl', 'Otwórz Awning w Zone 8 w 50%'),
                           ('en', 'Open Awning in Zone 8 to 50%')):
            decision = self.route(text, code, rows)
            self.assertEqual(decision.tool_call.arguments['name'], 'Awning  Motor')
            self.assertEqual(decision.tool_call.arguments['area'], 'Zone 8')
            self.assertEqual(decision.tool_call.arguments['position'], 50)

    def test_missing_position_tool_defers_without_proposing_full_opening(self):
        decision = self.route('Otwórz Markiza 2 na 50%', available=tools())
        self.assertEqual(decision.kind, router.DecisionKind.PASS)
        self.assertEqual(decision.reason, 'tool-unavailable')
        self.assertIsNone(decision.tool_call)

    def test_position_schema_and_forced_tool_are_respected(self):
        for mutation in ('string', 'minimum', 'maximum', 'enum', 'required', 'missing', 'ref'):
            available = position_tools()
            schema = available[-1]['function']['parameters']
            field = schema['properties']['position']
            if mutation == 'string': field['type'] = 'string'
            elif mutation == 'minimum': field['minimum'] = 60
            elif mutation == 'maximum': field['maximum'] = 40
            elif mutation == 'enum': field['enum'] = [0, 100]
            elif mutation == 'required': schema['required'].append('unknown')
            elif mutation == 'missing': del schema['properties']['position']
            elif mutation == 'ref': field['$ref'] = '#/unknown'
            with self.subTest(mutation=mutation):
                decision = self.route('Otwórz Markiza 2 na 50%', available=available)
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertEqual(decision.reason, 'unsupported-schema')
                self.assertIsNone(decision.tool_call)
        decision = self.route('Otwórz Markiza 2 na 50%', tool_choice={
            'type': 'function', 'function': {'name': 'intent__HassTurnOn'}})
        self.assertEqual(decision.reason, 'tool-unavailable')

    def test_advertised_tool_alias_and_numeric_type_are_preserved(self):
        available = position_tools()
        function = available[-1]['function']
        function['name'] = 'HassSetPosition'
        function['parameters']['properties']['position']['type'] = 'number'
        decision = self.route('Ustaw Markiza 2 na 50%', available=available, tool_choice={
            'type': 'function', 'function': {'name': 'HassSetPosition'}})
        self.assertEqual(decision.tool_call.name, 'HassSetPosition')
        self.assertEqual(decision.tool_call.arguments['position'], 50)

    def test_ha_allof_position_schema_routes_without_changing_selectors(self):
        available = position_tools()
        properties = available[-1]['function']['parameters']['properties']
        properties['floor'] = {'type': 'string'}
        properties['device_class'] = {'type': 'array', 'items': {
            'type': 'string', 'enum': ['awning', 'blind', 'curtain', 'shade']}}
        properties['position'] = {'allOf': [
            {'type': 'integer'}, {'type': 'number', 'minimum': 0, 'maximum': 100}]}
        original = copy.deepcopy(available)
        for value in (0, 50, 100):
            decision = self.route(f'Zamknij Markiza 2 w Atrium północne na {value}%',
                                  available=available)
            self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
            self.assertEqual(decision.tool_call.arguments, {
                'name': 'Markiza 2', 'area': 'Atrium północne',
                'domain': ['cover'], 'position': value})
        self.assertEqual(available, original)

    def test_allof_checks_every_branch_and_sibling_constraints(self):
        schema = {'allOf': [{'type': 'integer'},
                           {'type': 'number', 'minimum': 0, 'maximum': 100}]}
        self.assertTrue(router._schema_accepts(50, schema))
        for value in (-1, 101, 50.5, True, '50'):
            with self.subTest(value=value):
                self.assertFalse(router._schema_accepts(value, schema))
        for invalid in ([], {}, [None], [True], [{'type': 'integer', '$ref': '#/unknown'}]):
            with self.subTest(allof=invalid):
                self.assertFalse(router._schema_accepts(50, {'allOf': invalid}))
        self.assertFalse(router._schema_accepts(50, {**schema, '$ref': '#/unknown'}))
        self.assertFalse(router._schema_accepts(50, {**schema, 'type': 'integer', 'maximum': 40}))
        self.assertTrue(router._schema_accepts(50, {**schema, 'type': 'integer', 'maximum': 60}))
        self.assertTrue(router._schema_accepts(50, {'allOf': [schema], 'description': 'Position'}))

    def test_out_of_range_and_fractional_values_do_not_execute(self):
        for value in ('-1', '101', '50.5', '50,5'):
            with self.subTest(value=value):
                decision = self.route(f'Otwórz Markiza 2 na {value}%')
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertEqual(decision.reason, 'invalid-position')
                self.assertIsNone(decision.tool_call)

    def test_percentage_like_device_name_does_not_select_a_shorter_neighbor(self):
        rows = [('Awning', 'cover', None), ('Awning 50%', 'cover', None)]
        decision = self.route('Open Awning 50%', 'en', rows)
        self.assertEqual(decision.kind, router.DecisionKind.PASS)
        self.assertIsNone(decision.tool_call)

    def test_relative_multiple_actions_negation_and_other_domains_do_not_route(self):
        rows = [('Curtain', 'cover', 'Room'), ('Lamp', 'light', 'Room')]
        for text in ('Close Curtain by 20%', 'Open Curtain to 20% and close Lamp',
                     'Open Curtain to 20% then close Curtain to 50%',
                     "Don't open Curtain to 50%", 'Set Lamp to 50%',
                     'Set brightness of Lamp to 50%'):
            with self.subTest(text=text):
                self.assertNotEqual(self.route(text, 'en', rows).kind, router.DecisionKind.TOOLCALL)

    def test_ambiguous_targets_are_not_group_control(self):
        rows = [('Markiza 2', 'cover', 'Zone A'), ('Markiza 2', 'cover', 'Zone B')]
        decision = self.route('Ustaw Markiza 2 na 50%', rows=rows)
        self.assertEqual(decision.reason, 'ambiguous-target')

    def test_position_success_gets_ack_and_failure_defers_in_both_languages(self):
        for code in ('pl', 'en'):
            language = get_language(code)
            messages = [{'role': 'user', 'content': 'synthetic command'},
                        {'role': 'assistant', 'tool_calls': [{'id': 'fixture', 'type': 'function',
                            'function': {'name': 'intent__HassSetPosition', 'arguments': '{"position":50}'}}]}]
            for result, key in (({'success': True}, 'success'), ({'error': True}, 'failure')):
                decision = router.decide(messages + [{'role': 'tool', 'tool_call_id': 'fixture',
                    'content': json.dumps(result)}], overview([]), position_tools(), language=language)
                if key == 'success':
                    self.assertEqual(decision.kind, router.DecisionKind.ACK)
                    self.assertEqual(decision.response, language.vocabulary['responses'][key])
                else:
                    self.assertEqual(decision.kind, router.DecisionKind.PASS)
                    self.assertIsNone(decision.response)

    @unittest.skipUnless(importlib.util.find_spec('num2words'), 'num2words not installed')
    def test_spelled_percentages_use_num2words(self):
        for code, text in (('pl', 'Otwórz Awning na pięćdziesiąt procent'),
                           ('en', 'Set Awning to fifty percent'),
                           ('en', 'Open Awning to twenty-five percent')):
            decision = self.route(text, code, [('Awning', 'cover', None)])
            self.assertEqual(decision.tool_call.arguments['position'], 25 if 'twenty-five' in text else 50)

    def test_missing_number_library_does_not_invent_a_position(self):
        language = get_language('en')
        language._number_words.cache_clear()
        try:
            with patch.dict(sys.modules, {'num2words': None}):
                self.assertEqual(language.parse('Open Awning to fifty percent').reason, 'invalid-position')
                self.assertEqual(language.parse('Open Awning to 50%').position, 50)
        finally:
            language._number_words.cache_clear()

    def test_gateway_routes_position_without_contacting_backend(self):
        body = {'messages': [{'role': 'system', 'content': catalog.HEADING +
                            '\n- names: Awning\n  domain: cover\n  areas: Zone 9'},
                            {'role': 'user', 'content': 'Set Awning in Zone 9 to 50%'}],
                'tools': position_tools()}
        original = copy.deepcopy(body)
        handler = metrics_tests.GatewayMetricsTests()
        language = get_language('en')
        def decide(request):
            return router.decide(request['messages'], catalog.parse_overview(request['messages']),
                                 request['tools'], language=language)
        output, rows = handler.request(body=body, decision_fn=decide, decide_apply=True,
                                       clarify_text=language.clarify, agent_language='en')
        call = json.loads(output)['choices'][0]['message']['tool_calls'][0]['function']
        self.assertEqual(call['name'], 'intent__HassSetPosition')
        self.assertEqual(json.loads(call['arguments'])['position'], 50)
        self.assertEqual(rows[0]['route'], 'toolcall')
        self.assertEqual(handler.forwarded, [])
        self.assertEqual(body, original)


if __name__ == '__main__':
    unittest.main()
