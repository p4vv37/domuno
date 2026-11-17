"""Form-index router regressions and an A/B table against the legacy matcher.

Synthetic catalogs only: no HA, network or NPU. Run as a script to print the
old/new comparison on the same sentences:

    python3 tests/test_router_forms.py
"""
import importlib.util
from pathlib import Path
import sys
import unittest

GATEWAY = Path(__file__).resolve().parents[1] / 'gateway'
sys.path.insert(0, str(GATEWAY))
from qwen_language import get_language


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, GATEWAY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


catalog_mod = load('qwen_context_catalog', 'qwen_context_catalog.py')
router = load('forms_decision', 'qwen_decision.py')

HAS_INFLECTION = get_language('pl').supports_forms
HAS_SIMPLEMMA = get_language('pl').has_lemma


def overview(rows):
    text = catalog_mod.HEADING + '\n' + '\n'.join(
        '- names: %s\n  domain: %s%s' % (name, domain, '\n  areas: '+area if area else '')
        for name, domain, area in rows)
    return catalog_mod.parse_overview([{'role': 'system', 'content': text}])


def tools():
    parameters = {'type': 'object', 'properties': {
        'name': {'type': 'string'}, 'area': {'type': 'string'},
        'domain': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['name'], 'additionalProperties': False}
    return [{'type': 'function', 'function': {'name': name, 'parameters': parameters}}
            for name in ('intent__HassTurnOn', 'intent__HassTurnOff')]


def route(text, rows, matcher=None):
    return router.decide([{'role': 'user', 'content': text}], overview(rows),
                         tools(), matcher=matcher)


@unittest.skipUnless(HAS_INFLECTION, 'polish-inflection not installed')
class FormIndexTests(unittest.TestCase):
    def test_accusative_and_locative_forms_route(self):
        decision = route('Zamknij roletę w sypialni', [('Roleta', 'cover', 'Sypialnia')])
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments,
                         {'name': 'Roleta', 'area': 'Sypialnia', 'domain': ['cover']})

    def test_multiword_name_agrees_with_modifier(self):
        decision = route('Włącz lampę sufitową w kuchni',
                         [('Lampa sufitowa', 'light', 'Kuchnia')])
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments,
                         {'name': 'Lampa sufitowa', 'area': 'Kuchnia', 'domain': ['light']})

    def test_plural_generated_from_singular_alias_routes_one_device(self):
        decision = route('Zamknij rolety w sypialni', [('Roleta', 'cover', 'Sypialnia')])
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments['name'], 'Roleta')

    def test_explicit_plural_alias_still_routes(self):
        decision = route('Zamknij rolety w sypialni',
                         [('Roleta, Rolety', 'cover', 'Sypialnia')])
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments['name'], 'Roleta')

    def test_plural_of_two_identical_names_is_ambiguous_not_plural(self):
        rows = [('Roleta', 'cover', 'Salon'), ('Roleta', 'cover', 'Sypialnia')]
        decision = route('Zamknij rolety', rows)
        self.assertEqual(decision.reason, 'ambiguous-target')

    def test_brand_head_is_not_inflected(self):
        self.assertEqual(get_language('pl').forms('Shelly Plus 2PM'), ())
        self.assertTrue(get_language('pl').forms('Lampa sufitowa'))
        decision = route('Włącz Shelly Plus 2PM', [('Shelly Plus 2PM', 'switch', None)])
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments['name'], 'Shelly Plus 2PM')

    def test_explicit_spelling_beats_generated_form(self):
        rows = [('Lampa', 'light', 'Salon'), ('Lampy', 'light', 'Salon')]
        decision = route('Włącz Lampa', rows)
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments['name'], 'Lampa')

    def test_one_edit_still_recovers_stt_noise(self):
        rows = [('Roleta, Rolety', 'cover', 'Sypialnia')]
        for text in ('Zamknij urolety w sypialni', 'Zamknij relety w sypialni'):
            with self.subTest(text=text):
                self.assertEqual(route(text, rows).kind, router.DecisionKind.TOOLCALL)

    def test_unknown_word_never_routes(self):
        decision = route('Włącz XYZ', [('Lampa', 'light', 'Salon')])
        self.assertEqual(decision.reason, 'no-target')

    def test_equal_overviews_reuse_the_index(self):
        rows = [('Roleta', 'cover', 'Sypialnia')]
        self.assertIs(router._indexes(overview(rows), get_language('pl')), router._indexes(overview(rows), get_language('pl')))


# (label, text, rows, expected forms result, legacy result under simplemma).
# None means the legacy outcome depends on simplemma and is only reported.
CORPUS = [
    ('singular acc + locative', 'Zamknij roletę w sypialni',
     [('Roleta', 'cover', 'Sypialnia')], 'toolcall', None),
    ('plural of singular alias', 'Zamknij rolety w sypialni',
     [('Roleta', 'cover', 'Sypialnia')], 'toolcall', None),
    ('explicit plural alias', 'Zamknij rolety w sypialni',
     [('Roleta, Rolety', 'cover', 'Sypialnia')], 'toolcall', None),
    ('multiword agreement', 'Włącz lampę sufitową w kuchni',
     [('Lampa sufitowa', 'light', 'Kuchnia')], 'toolcall', None),
    ('exact spelling over similar name', 'Włącz Lampa',
     [('Lampa', 'light', 'Salon'), ('Lampy', 'light', 'Salon')], 'toolcall', None),
    ('same name, two domains', 'Włącz lampę',
     [('Lampa', 'light', 'Salon'), ('Lampa', 'switch', 'Salon')],
     'clarify:ambiguous-target', 'clarify:ambiguous-target'),
    ('unknown target', 'Włącz XYZ',
     [('Lampa', 'light', 'Salon')], 'pass:no-target', 'pass:no-target'),
    ('wrong domain', 'Zamknij lampę',
     [('Lampa', 'light', 'Salon')], 'pass:unsupported-domain', 'pass:unsupported-domain'),
    # A whole percentage after the preposition is a valid position command once
    # HassSetPosition is advertised. This catalog only exposes on/off tools.
    ('position tool not advertised', 'Otwórz roletę w sypialni na 30%',
     [('Roleta', 'cover', 'Sypialnia')], 'clarify:position-unavailable', 'clarify:position-unavailable'),
]


def outcome(decision):
    kind = decision.kind.value
    return kind + (':' + decision.reason if decision.kind in (
        router.DecisionKind.CLARIFY, router.DecisionKind.PASS) else '')


class ComparisonTests(unittest.TestCase):
    @unittest.skipUnless(HAS_INFLECTION, 'polish-inflection not installed')
    def test_forms_mode_matches_the_expected_table(self):
        for label, text, rows, expected, _legacy in CORPUS:
            with self.subTest(label=label):
                self.assertEqual(outcome(route(text, rows, 'forms')), expected)

    @unittest.skipUnless(HAS_INFLECTION and HAS_SIMPLEMMA,
                         'both matchers need simplemma and polish-inflection')
    def test_legacy_mode_matches_the_expected_table(self):
        for label, text, rows, _expected, legacy in CORPUS:
            if legacy is None:
                continue
            with self.subTest(label=label):
                self.assertEqual(outcome(route(text, rows, 'legacy')), legacy)


def print_table():
    print('%-32s %-24s %-24s' % ('sentence', 'forms', 'legacy'))
    for label, text, rows, _expected, _legacy in CORPUS:
        forms = outcome(route(text, rows, 'forms'))
        legacy = outcome(route(text, rows, 'legacy'))
        print('%-32s %-24s %-24s' % (label, forms, legacy))


if __name__ == '__main__':
    print_table()
