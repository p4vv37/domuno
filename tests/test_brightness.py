"""Absolute light-brightness routing and noisy voice input. No HA actions."""
import json
import unittest

from test_gateway import catalog, get_language, overview, router, tools
import test_metrics as metrics_tests


def light_tools():
    return tools() + [{'type': 'function', 'function': {
        'name': 'light__HassLightSet',
        'parameters': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'area': {'type': 'string'},
            'floor': {'type': 'string'},
            'domain': {'type': 'array', 'items': {'type': 'string'}},
            'brightness': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
            'additionalProperties': False}}}]


class BrightnessTests(unittest.TestCase):
    def route(self, text, rows=None, available=None, *, code='pl', **kwargs):
        rows = rows or [('Lampa sufitowa', 'light', 'Salon')]
        return router.decide([{'role': 'user', 'content': text}], overview(rows),
                             light_tools() if available is None else available,
                             language=get_language(code), **kwargs)

    def test_english_named_light_brightness(self):
        for text in ('Set the brightness of the Desk Lamp in the Study to 20%.',
                     'Dim the Desk Lamp in the Study to twenty percent.'):
            with self.subTest(text=text):
                decision = self.route(text, [('Desk Lamp', 'light', 'Study')], code='en')
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.name, 'light__HassLightSet')
                self.assertEqual(decision.tool_call.arguments,
                                 {'name': 'Desk Lamp', 'area': 'Study',
                                  'domain': ['light'], 'brightness': 20})

    def test_spanish_named_light_brightness(self):
        for text in ('Ajusta el brillo de la Lámpara de mesa en el Estudio al 20%.',
                     'Atenúa la Lámpara de mesa en el Estudio al veinte por ciento.'):
            with self.subTest(text=text):
                decision = self.route(text, [('Lámpara de mesa', 'light', 'Estudio')], code='es')
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.name, 'light__HassLightSet')
                self.assertEqual(decision.tool_call.arguments,
                                 {'name': 'Lámpara de mesa', 'area': 'Estudio',
                                  'domain': ['light'], 'brightness': 20})

    def test_named_light_uses_the_canonical_selector(self):
        decision = self.route('Ustaw jasność Lampa sufitowa w Salon na 20%.')
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.name, 'light__HassLightSet')
        self.assertEqual(decision.tool_call.arguments,
                         {'name': 'Lampa sufitowa', 'area': 'Salon',
                          'domain': ['light'], 'brightness': 20})

    def test_dim_and_brighten_are_absolute_requests(self):
        for text, value in (('Ściemnij Lampa sufitowa w salonie do 10%.', 10),
                            ('Rozjaśnij Lampa sufitowa w salonie do 80%.', 80)):
            with self.subTest(text=text):
                decision = self.route(text)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments,
                                 {'name': 'Lampa sufitowa', 'area': 'Salon',
                                  'domain': ['light'], 'brightness': value})

    def test_generic_light_noun_targets_every_light_in_the_area(self):
        rows = [('Lampa sufitowa', 'light', 'Salon'),
                ('Lampa stojąca', 'light', 'Salon')]
        for text in ('Ustaw jasność światła w salonie na 20%.',
                     'Ściemnij lampy w salonie do 20%.'):
            with self.subTest(text=text):
                decision = self.route(text, rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments,
                                 {'area': 'Salon', 'domain': ['light'], 'brightness': 20})

    def test_english_and_spanish_generic_lights_target_the_explicit_area(self):
        for code, text, area in (
                ('en', 'Dim the lights in the Study to 20%.', 'Study'),
                ('es', 'Atenúa las luces en el Estudio al 20%.', 'Estudio')):
            with self.subTest(code=code):
                rows = [('Fixture A', 'light', area), ('Fixture B', 'light', area)]
                decision = self.route(text, rows, code=code)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments,
                                 {'area': area, 'domain': ['light'], 'brightness': 20})

    def test_generic_noun_does_not_hijack_a_named_device_elsewhere(self):
        rows = [('Lampki', 'light', 'Balkon'),
                ('Lampa sufitowa', 'light', 'Salon'),
                ('Lampa stojąca', 'light', 'Salon')]
        decision = self.route('Ustaw jasność światła w salonie na 20%.', rows)
        self.assertEqual(decision.tool_call.arguments['area'], 'Salon')
        self.assertNotIn('name', decision.tool_call.arguments)

    def test_device_named_lampki_wins_over_the_generic_noun(self):
        rows = [('Lampki', 'light', 'Balkon'), ('Lampa sufitowa', 'light', 'Salon')]
        for text in ('Ustaw jasność lampek na 50%.', 'Ściemnij Lampki na 50%.'):
            with self.subTest(text=text):
                decision = self.route(text, rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments,
                                 {'name': 'Lampki', 'area': 'Balkon',
                                  'domain': ['light'], 'brightness': 50})

    def test_named_device_in_another_area_does_not_expand_to_a_group(self):
        rows = [('Lampki', 'light', 'Balkon'),
                ('Lampa sufitowa', 'light', 'Salon'),
                ('Lampa stojąca', 'light', 'Salon')]
        decision = self.route('Ustaw jasność lampek w salonie na 50%.', rows)
        self.assertEqual((decision.kind, decision.reason),
                         (router.DecisionKind.PASS, 'area-mismatch'))

    def test_group_brightness_requires_an_explicit_known_area_and_one_noun(self):
        for text in ('Ściemnij światło do 20%.',
                     'Ściemnij światło w Salom do 20%.',
                     'Ściemnij lampa żarówka w salonie do 20%.'):
            with self.subTest(text=text):
                self.assertEqual(self.route(text, default_area='Salon').kind,
                                 router.DecisionKind.PASS)

    def test_group_brightness_does_not_guess_an_ambiguous_area(self):
        for rows in ([('Lampa sufitowa', 'light', 'Salon, Pokój')],
                     [('Lampa sufitowa', 'light', 'Pokój'),
                      ('Gniazdko', 'switch', 'Gabinet, Pokój')]):
            with self.subTest(rows=rows):
                decision = self.route('Ściemnij światło w Pokoju do 20%.', rows)
                self.assertEqual((decision.kind, decision.reason),
                                 (router.DecisionKind.CLARIFY, 'ambiguous-area'))

    def test_relative_brightness_passes_in_every_language(self):
        for code, text, area in (
                ('pl', 'Ściemnij światło w salonie o 10%.', 'Salon'),
                ('en', 'Dim the lights in the Study by 10%.', 'Study'),
                ('es', 'Atenúa las luces en el Estudio en un 10%.', 'Estudio')):
            with self.subTest(code=code):
                decision = self.route(text, [('Fixture', 'light', area)], code=code)
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertIsNone(decision.tool_call)

    def test_out_of_range_and_relative_brightness_are_not_executed(self):
        decision = self.route('Ustaw jasność światła w salonie na 150%.')
        self.assertEqual((decision.kind, decision.reason),
                         (router.DecisionKind.CLARIFY, 'invalid-position'))
        for text in ('Zmniejsz jasność światła w salonie o 10%',
                     'Ściemnij światło w salonie trochę',
                     'Ustaw jasność światła w salonie na 20% i zamknij rolety',
                     'Ustaw jasność nie tej lampy w salonie na 150%'):
            with self.subTest(text=text):
                self.assertEqual(self.route(text).kind, router.DecisionKind.PASS)

    def test_missing_brightness_schema_and_tool_fail_open(self):
        for mutation in ('tool', 'parameter', 'name-required', 'malformed-required'):
            available = light_tools()
            if mutation == 'tool':
                available = tools()
            elif mutation == 'parameter':
                del available[-1]['function']['parameters']['properties']['brightness']
            else:
                available[-1]['function']['parameters']['required'] = (
                    ['name'] if mutation == 'name-required' else 1)
            with self.subTest(mutation=mutation):
                decision = self.route('Ustaw jasność światła w salonie na 20%.',
                                      available=available)
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertIsNone(decision.tool_call)

    def test_explicit_polish_retraction_cancels(self):
        for suffix in ('Nie, tak jest ok.', 'Anuluj.'):
            with self.subTest(suffix=suffix):
                decision = self.route(f'Ściemnij światło w salonie do 10%. {suffix}')
                self.assertEqual((decision.kind, decision.reason),
                                 (router.DecisionKind.CLARIFY, 'negation'))
                self.assertIsNone(decision.tool_call)
        self.assertEqual(self.route('Prognoza pogody. Anuluj.').kind, router.DecisionKind.PASS)

    def test_trailing_text_never_turns_into_a_partial_action(self):
        rows = [('Lampa', 'light', 'Salon'), ('Markiza', 'cover', 'Salon')]
        for text in ('Włącz Lampę. Zamknij Markizę.', 'Włącz Lampę. Zamknij, proszę.',
                     'Włącz Lampę. Wyłącz. Pogoda.', 'Włącz Lampę. Tylko jeśli jest ciemno.',
                     'Ściemnij światło w salonie do 10%. Nie schodź poniżej 30%.',
                     'Ustaw jasność światła w salonie na 20%. Co za pogoda.',
                     'Ściemnij światło w salonie do 20%?'):
            with self.subTest(text=text):
                self.assertEqual(self.route(text, rows).kind, router.DecisionKind.PASS)

    def test_period_in_a_friendly_name_does_not_shorten_the_selector(self):
        rows = [('Lampa św', 'light', 'Salon'), ('Lampa św. Jana', 'light', 'Salon')]
        decision = self.route('Włącz Lampa św. Jana.', rows)
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments['name'], 'Lampa św. Jana')

    def test_english_and_spanish_trailing_retractions_do_not_route(self):
        for code, name, text in (
                ('en', 'Desk lamp', 'Turn on Desk lamp. No, leave it as it is.'),
                ('es', 'Luz', 'Enciende la luz. No, déjalo así.')):
            with self.subTest(code=code):
                decision = router.decide([{'role': 'user', 'content': text}],
                                         overview([(name, 'light', None)]), light_tools(),
                                         language=get_language(code))
                self.assertEqual(decision.kind, router.DecisionKind.PASS)

    def test_gateway_routes_a_complete_command_and_preserves_a_conditional_one(self):
        language = get_language('pl')
        command = 'Ściemnij światło w salonie do 20%'
        for text, expected in ((command, 'toolcall'),
                               (command + '. Tylko jeśli jest ciemno.', 'llm')):
            with self.subTest(text=text):
                body = {'messages': [
                    {'role': 'system', 'content': catalog.HEADING +
                     '\n- names: Lampa sufitowa\n  domain: light\n  areas: Salon'},
                    {'role': 'user', 'content': text}], 'tools': light_tools()}
                handler = metrics_tests.GatewayMetricsTests()
                output, rows = handler.request(
                    body=body, decision_fn=lambda request: router.decide_request(request, language=language),
                    decide_apply=True, response_style='original')
                self.assertEqual(rows[0]['route'], expected)
                if expected == 'toolcall':
                    call = json.loads(output)['choices'][0]['message']['tool_calls'][0]['function']
                    self.assertEqual(call['name'], 'light__HassLightSet')
                    self.assertEqual(json.loads(call['arguments']),
                                     {'area': 'Salon', 'domain': ['light'], 'brightness': 20})
                    self.assertEqual(handler.forwarded, [])
                else:
                    self.assertEqual(len(handler.forwarded), 1)
                    self.assertEqual(handler.forwarded[0]['messages'], body['messages'])

    def test_group_brightness_ack_requires_success_without_failures(self):
        text = 'Ściemnij światło w salonie do 0%'
        call = self.route(text).tool_call
        self.assertEqual(call.arguments['brightness'], 0)
        messages = [{'role': 'user', 'content': text},
                    {'role': 'assistant', 'tool_calls': [{'id': 'fixture', 'type': 'function',
                        'function': {'name': call.name, 'arguments': json.dumps(call.arguments)}}]}]
        for result, expected in (({'success': True}, router.DecisionKind.ACK),
                                 ({'data': {'success': ['a'], 'failed': ['b']}},
                                  router.DecisionKind.PASS)):
            with self.subTest(result=result):
                decision = router.decide(messages + [{'role': 'tool', 'tool_call_id': 'fixture',
                    'content': json.dumps(result)}], None, light_tools(), language=get_language('pl'))
                self.assertEqual(decision.kind, expected)


if __name__ == '__main__':
    unittest.main()
