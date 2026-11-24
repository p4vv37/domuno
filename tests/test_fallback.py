"""Router abstention and failed-action handoff, with no HA actions or models."""
import copy
import json
import unittest
from unittest.mock import patch

from test_gateway import catalog, get_language, overview, router, tools
import test_metrics as metrics_tests


class FallbackTests(unittest.TestCase):
    def test_unsupported_percentages_reach_model_in_both_languages(self):
        for code, text in (
                ('pl', 'Zmniejsz jasność Lamp o 50%'),
                ('pl', 'Ustaw jasność Lamp na 50%'),
                ('pl', 'Zamknij Awning o 50%'),
                ('en', 'Set brightness of Lamp to 50%'),
                ('en', 'Close Awning by 50%')):
            with self.subTest(code=code, text=text):
                decision = router.decide([{'role': 'user', 'content': text}],
                    overview([('Lamp', 'light', None), ('Awning', 'cover', None)]),
                    tools(), language=get_language(code))
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertIsNone(decision.tool_call)

    def test_generated_plural_routes_one_target_but_real_collision_still_clarifies(self):
        # Exercise the strategy seam even when optional dictionary data is absent.
        for code, verb in (('en', 'Close'), ('pl', 'Zamknij')):
            language = get_language(code)
            with patch.object(language, 'forms', return_value=(('awning', 'sg'), ('awnings', 'pl'))):
                for rows, expected in (
                        ([('Awning', 'cover', None)], router.DecisionKind.TOOLCALL),
                        ([('Awning', 'cover', 'Zone A'), ('Awning', 'cover', 'Zone B')],
                         router.DecisionKind.CLARIFY)):
                    with self.subTest(code=code, rows=rows):
                        decision = router.decide([{'role': 'user', 'content': f'{verb} awnings'}],
                            overview(rows), tools(), language=language)
                        self.assertEqual(decision.kind, expected)
                        if expected == router.DecisionKind.TOOLCALL:
                            self.assertEqual(decision.tool_call.arguments['name'], 'Awning')
                        else:
                            self.assertIsNone(decision.tool_call)
                preposition = 'w' if code == 'pl' else 'in'
                scoped = router.decide([{'role': 'user',
                    'content': f'{verb} awnings {preposition} Zone B'}],
                    overview([('Awning', 'cover', 'Zone A'), ('Awning', 'cover', 'Zone B')]),
                    tools(), language=language)
                self.assertEqual(scoped.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(scoped.tool_call.arguments['area'], 'Zone B')

    def test_failed_action_goes_to_model_once_with_original_result_and_tools(self):
        failures = [{'error': 'MatchFailedError'}, {'success': False},
                    {'data': {'success': [], 'failed': [{'error': 'unavailable'}]}},
                    {'data': {'success': [{'name': 'Fixture'}], 'failed': [{}]}}]
        for code in ('pl', 'en'):
            for failure in failures:
                with self.subTest(code=code, failure=failure):
                    body = {'messages': [
                        {'role': 'user', 'content': 'synthetic original command'},
                        {'role': 'assistant', 'tool_calls': [{'id': 'fixture', 'type': 'function',
                            'function': {'name': 'intent__HassTurnOn', 'arguments': '{"name":"Lamp"}'}}]},
                        {'role': 'tool', 'tool_call_id': 'fixture', 'content': json.dumps(failure)}],
                        'tools': tools()}
                    original = copy.deepcopy(body)
                    def decide(request):
                        return router.decide(request['messages'], overview([]), request['tools'],
                                             language=get_language(code))
                    decision = decide(body)
                    self.assertEqual(decision.kind, router.DecisionKind.PASS)
                    handler = metrics_tests.GatewayMetricsTests()
                    _, rows = handler.request(body=body, decision_fn=decide,
                                              decide_apply=True, response_style='original')
                    self.assertEqual(len(handler.forwarded), 1)
                    self.assertEqual(handler.forwarded[0]['messages'], original['messages'])
                    self.assertEqual(handler.forwarded[0]['tools'], original['tools'])
                    self.assertEqual(rows[0]['route'], 'llm')
                    self.assertEqual(body, original)

    def test_success_with_failed_field_never_gets_a_success_ack(self):
        self.assertIsNone(router._ack_response('{"success":true,"data":{"failed":[{}]}}'))


if __name__ == '__main__':
    unittest.main()
