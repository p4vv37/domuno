"""Language strategies through the shared router. Synthetic catalogs only."""
import subprocess
import sys
import unittest

from test_gateway import GATEWAY, get_language, overview, router, tools


class LanguageTests(unittest.TestCase):
    def route(self, text, rows, code='en'):
        return router.decide([{'role': 'user', 'content': text}], overview(rows), tools(),
                             language=get_language(code))

    def test_english_phrasal_verbs_articles_and_context_selectors(self):
        rows = [('Desk lamp', 'light', 'Study 7'), ('Curtain', 'cover', 'North wing')]
        for text, tool, name, area in (
                ('Turn on the Desk lamp in the Study 7.', 'On', 'Desk lamp', 'Study 7'),
                ('Please switch off Desk lamp at Study 7', 'Off', 'Desk lamp', 'Study 7'),
                ('Open the Curtain in the North wing', 'On', 'Curtain', 'North wing'),
                ('Close Curtain in North wing', 'Off', 'Curtain', 'North wing')):
            with self.subTest(text=text):
                decision = self.route(text, rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.name, 'intent__HassTurn'+tool)
                self.assertEqual(decision.tool_call.arguments['name'], name)
                self.assertEqual(decision.tool_call.arguments['area'], area)

    def test_english_negation_parameters_and_multiple_actions_never_execute(self):
        rows = [('Lamp', 'light', 'Room'), ('Curtain', 'cover', 'Room')]
        for text in ("Don't turn on the Lamp", 'Do not open the Curtain',
                     'Please never close Curtain'):
            with self.subTest(text=text):
                self.assertEqual(self.route(text, rows).reason, 'negation')
        for text in ('Turn on Lamp and close Curtain', 'Turn on Lamp after ten minutes',
                     'If it is dark turn on Lamp', 'Could you open the Curtain?'):
            with self.subTest(text=text):
                decision = self.route(text, rows)
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertIsNone(decision.tool_call)

    def test_ambiguity_and_unsupported_domains_use_shared_checks(self):
        rows = [('Lamp', 'light', 'Room A'), ('Lamp', 'light', 'Room B')]
        self.assertEqual(self.route('Turn on Lamp', rows).reason, 'ambiguous-target')
        decision = self.route('Turn on Lamp in Room B', rows)
        self.assertEqual(decision.tool_call.arguments['area'], 'Room B')
        self.assertEqual(self.route('Open Lock', [('Lock', 'lock', None)]).reason, 'unsupported-domain')

    def test_language_specific_ack_and_clarification(self):
        for code, success in (('en', 'Done'), ('pl', 'Wykonano')):
            with self.subTest(code=code):
                language = get_language(code)
                self.assertEqual(router._ack_response('{"success":true}', language), success)
                self.assertIsNone(router._ack_response('{"error":true}', language))
                self.assertIsNone(router._ack_response('{"result":"unknown"}', language))
                self.assertEqual(router.clarify_text('missing-area', language),
                                 language.vocabulary['responses']['clarifications']['missing-area'])

    def test_english_does_not_import_polish_strategy(self):
        result = subprocess.run([sys.executable, '-c',
            'import sys; from qwen_language import get_language; '
            'language=get_language("en"); language.warmup(); '
            'assert "qwen_language_pl" not in sys.modules; '
            'assert "polish_inflection" not in sys.modules; print(language.code)'],
            cwd=GATEWAY, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'en')

    def test_index_caches_are_separate_for_each_strategy(self):
        catalog = overview([('Lamp', 'light', 'Room')])
        pl, en = get_language('pl'), get_language('en')
        self.assertIs(router._indexes(catalog, pl), router._indexes(catalog, pl))
        self.assertIsNot(router._indexes(catalog, pl), router._indexes(catalog, en))

    def test_unknown_language_is_an_explicit_configuration_error(self):
        with self.assertRaisesRegex(ValueError, 'AGENT_LANGUAGE'):
            get_language('xx')


if __name__ == '__main__':
    unittest.main()
