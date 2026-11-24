"""Publication wrapper regression tests; synthetic HA data, no network or NPU."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import stat
import sys
import unittest
from unittest.mock import patch

GATEWAY = Path(__file__).resolve().parents[1] / 'gateway'
sys.path.insert(0, str(GATEWAY))
from languages import get_language


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, GATEWAY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Isolate this publication copy from the older experimental scripts in tests/.
previous = sys.modules.get('context_catalog')
catalog = load('context_catalog', 'context_catalog.py')
router = load('publication_decision', 'command_router.py')
if previous is not None:
    sys.modules['context_catalog'] = previous
else:
    del sys.modules['context_catalog']
load('gateway_metrics', 'gateway_metrics.py')
gateway = load('publication_gateway', 'chat_gateway.py')
counter = load('publication_counter', 'prompt_counter.py')


def tools():
    parameters = {'type': 'object', 'properties': {
        'name': {'type': 'string'}, 'area': {'type': 'string'},
        'domain': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['name'], 'additionalProperties': False}
    return [{'type': 'function', 'function': {'name': name, 'parameters': copy.deepcopy(parameters)}}
            for name in ('intent__HassTurnOn', 'intent__HassTurnOff')]


def overview(rows):
    text = catalog.HEADING + '\n' + '\n'.join(
        '- names: %s\n  domain: %s%s' % (name, domain, '\n  areas: '+area if area else '')
        for name, domain, area in rows)
    return catalog.parse_overview([{'role': 'system', 'content': text}])


def result(text, rows=None, **kwargs):
    rows = rows or [('Lampa', 'light', 'Salon')]
    return router.decide([{'role': 'user', 'content': text}], overview(rows), tools(), **kwargs)


class PublicationRouterTests(unittest.TestCase):
    def test_vocabulary_environment_selects_external_file_and_missing_file_blocks_start(self):
        vocabulary = json.loads((GATEWAY/'languages/pl/vocabulary.json').read_text())
        vocabulary['responses']['clarifications']['negation'] = 'Synthetic response'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'custom vocabulary.json'
            path.write_text(json.dumps(vocabulary))
            env = {**os.environ, 'ROUTER_VOCABULARY': str(path)}
            selected = subprocess.run([sys.executable, '-c',
                'from command_router import clarify_text; print(clarify_text("negation"))'],
                cwd=GATEWAY, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertEqual(selected.stdout.strip(), 'Synthetic response')
            path.unlink()
            blocked = subprocess.run([sys.executable, 'chat_gateway.py', '--decide-apply', '--port', '0',
                 '--prompt-counter-url', 'http://127.0.0.1:1/count'],
                cwd=GATEWAY, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertNotIn('Qwen gateway listening', blocked.stdout)
            self.assertIn('FileNotFoundError', blocked.stderr)

    def test_vocabulary_changes_verbs_and_replies_without_changing_target_checks(self):
        vocabulary = json.loads((GATEWAY/'languages/pl/vocabulary.json').read_text())
        vocabulary['commands']['on'].append('odpal')
        vocabulary['responses']['success'] = 'Gotowe'
        vocabulary['responses']['clarifications']['ambiguous-target'] = 'Które urządzenie?'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'words.json'
            path.write_text(json.dumps(vocabulary))
            with patch.object(router, 'default_language', return_value=get_language('pl', str(path))):
                self.assertEqual(result('Odpal Lampa').kind, router.DecisionKind.TOOLCALL)
                duplicate = [('Lampa', 'light', 'Salon'), ('Lampa', 'light', 'Gabinet')]
                self.assertEqual(result('Odpal Lampa', duplicate).kind, router.DecisionKind.CLARIFY)
                self.assertEqual(result('Odpal Lampa na 50 procent').kind, router.DecisionKind.PASS)
                self.assertEqual(router._ack_response('{"success":true}'), 'Gotowe')
                self.assertEqual(router.clarify_text('ambiguous-target'), 'Które urządzenie?')

    def test_vocabulary_rejects_conflicting_commands_and_incomplete_responses(self):
        original = json.loads((GATEWAY/'languages/pl/vocabulary.json').read_text())
        variants = []
        duplicate = copy.deepcopy(original)
        duplicate['commands']['off'].append('WŁĄCZ')
        variants.append(duplicate)
        phrase = copy.deepcopy(original)
        phrase['commands']['on'].append('proszę włącz')
        variants.append(phrase)
        negation = copy.deepcopy(original)
        negation['commands']['on'].append('nie')
        variants.append(negation)
        missing = copy.deepcopy(original)
        del missing['responses']['clarifications']['negation']
        variants.append(missing)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'words.json'
            for vocabulary in variants:
                path.write_text(json.dumps(vocabulary))
                with self.subTest(vocabulary=vocabulary), self.assertRaises(ValueError):
                    get_language('pl', str(path))

    def test_normalization_drops_unsupported_knobs_and_pins_runtime(self):
        body = {'messages': [{'role': 'user', 'content': 'Test'}],
                'spec_type': 'draft', 'spec_draft_model': 'draft.gguf',
                'spec_n_max': 4, 'spec_n_min': 1, 'spec_p_min': .5,
                'grammar_path': '/tmp/grammar.gbnf', 'grammar_string': 'root ::= "x"',
                'vit_compute': 'npu', 'power_mode': 'high', 'stop': ['\n\n'],
                'reasoning_format': 'json', 'temperature': 2, 'max_tokens': 9999}
        normalized = gateway.normalize_request(body, nctx=10240)
        for key in ('spec_type', 'spec_draft_model', 'spec_n_max', 'spec_n_min',
                    'spec_p_min', 'grammar_path', 'grammar_string', 'vit_compute',
                    'power_mode', 'stop', 'max_tokens'):
            self.assertNotIn(key, normalized)
        self.assertEqual(normalized['reasoning_format'], 'deepseek')
        self.assertEqual(normalized['temperature'], .1)
        self.assertEqual(normalized['max_completion_tokens'], 512)
        self.assertEqual(normalized['nctx'], 10240)

    def test_inline_heading_text_does_not_shift_the_overview_block(self):
        content = ('Earlier prose mentions ' + catalog.HEADING + ' without a newline.\n'
                   + catalog.HEADING + '\n- names: Lampa\n  domain: light\n  areas: Salon')
        parsed = catalog.parse_overview([{'role': 'system', 'content': content}])
        self.assertEqual(parsed.status, catalog.Status.RECOGNIZED)
        self.assertEqual(parsed.entities[0].names, 'Lampa')

    def test_dynamic_names_domains_and_optional_area(self):
        for verb, name, domain, action in [
                ('Włącz', 'Lampa', 'light', 'On'), ('Zgaś', 'Lampa', 'light', 'Off'),
                ('Uruchom', 'Wiatrak', 'fan', 'On'), ('Wyłącz', 'Gniazdko', 'switch', 'Off'),
                ('Włącz', 'Tryb gości', 'input_boolean', 'On'),
                ('Zamknij', 'Roleta', 'cover', 'Off'), ('Otwórz', 'Markiza', 'cover', 'On')]:
            for area in ('Salon', None):
                with self.subTest(verb=verb, domain=domain, area=area):
                    decision = result(verb+' '+name, [(name, domain, area)])
                    self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                    expected = {'name': name, 'domain': [domain]}
                    if area:
                        expected['area'] = area
                    self.assertEqual(decision.tool_call.arguments, expected)
                    self.assertEqual(decision.tool_call.name, 'intent__HassTurn'+action)

    def test_polish_case_punctuation_alias_and_one_edit(self):
        rows = [('Roleta, Rolety', 'cover', 'Sypialnia')]
        for text in ('Zamknij rolety w sypialni.', 'Proszę zamknij urolety w sypialni!',
                     'ZAMKNIJ ROLETĘ W SYPIALNI'):
            with self.subTest(text=text):
                self.assertEqual(result(text, rows).kind, router.DecisionKind.TOOLCALL)

    @unittest.skipIf(not get_language('pl').has_lemma, 'simplemma not installed; run this case on Arduino')
    def test_lemmatization_on_installed_runtime(self):
        self.assertEqual(result('Włącz lampę w salonie').kind, router.DecisionKind.TOOLCALL)

    def test_duplicate_name_needs_area(self):
        rows = [('Lampa', 'light', 'Salon'), ('Lampa', 'light', 'Gabinet')]
        self.assertEqual(result('Włącz lampę', rows).kind, router.DecisionKind.CLARIFY)
        decision = result('Włącz lampę w Gabinet', rows)
        self.assertEqual(decision.tool_call.arguments['area'], 'Gabinet')

    def test_wrong_domain_does_not_become_cover(self):
        self.assertEqual(result('Zamknij lampę').kind, router.DecisionKind.PASS)
        self.assertEqual(result('Otwórz Zamek', [('Zamek', 'lock', None)]).kind, router.DecisionKind.PASS)

    def test_exact_target_and_area_precede_fuzzy(self):
        rows = [('Lampa', 'light', 'Salon'), ('Lampy', 'light', 'Salon')]
        self.assertEqual(result('Włącz Lampa', rows).tool_call.arguments['name'], 'Lampa')
        rows = [('Lampa', 'light', 'Salon'), ('Lampa', 'light', 'Salony')]
        self.assertEqual(result('Włącz Lampa w Salon', rows).tool_call.arguments['area'], 'Salon')

    def test_unknown_or_ambiguous_target_never_routes(self):
        for text, rows in [
                ('Włącz XYZ', [('Lampa', 'light', 'Salon')]),
                ('Włącz lampę', [('Lampa', 'light', 'Salon'), ('Lampa', 'switch', 'Salon')]),
                ('Włącz lampę w Gabinet', [('Lampa', 'light', 'Salon')]),
                ('Włącz lampę', [('Lampa', 'light', 'Salon, Gabinet')]),
                ('Włącz lampę w', [('Lampa', 'light', 'Salon')])]:
            with self.subTest(text=text, rows=rows):
                expected = (router.DecisionKind.PASS if text in ('Włącz XYZ', 'Włącz lampę w Gabinet')
                            else router.DecisionKind.CLARIFY)
                self.assertEqual(result(text, rows).kind, expected)

    def test_unsupported_grammar_passes_without_losing_parameters(self):
        for text in ('Włącz lampę i wyłącz wiatrak',
                     'Włącz lampę za minutę', 'Jeśli jest ciemno włącz lampę',
                     'Czy możesz włączyć lampę?', 'Jaka jest pogoda?'):
            with self.subTest(text=text):
                self.assertEqual(result(text).kind, router.DecisionKind.PASS)
        self.assertEqual(result('Nie włącz lampę').reason, 'negation')

    def test_closest_match_recovers_larger_stt_noise_only_when_unique(self):
        rows = [('roleta  Rolety', 'cover', 'Sypialnia')]
        for text in ('Otwórz rożoleta w sypialni.',
                     'Otwórz roleta w sypialnym.',
                     'Otwórz rożoleta w sypialnym.'):
            with self.subTest(text=text):
                decision = result(text, rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments['name'], 'roleta  Rolety')
                self.assertEqual(decision.tool_call.arguments['area'], 'Sypialnia')
        decision = result('Otwórz lampka w sypialni.', rows)
        self.assertEqual(decision.kind, router.DecisionKind.PASS)
        self.assertIsNone(decision.tool_call)

    def test_politeness_and_urgency_prefixes_keep_local_routing(self):
        rows = [('Markiza', 'cover', None)]
        for prefix in ('', 'Proszę ', 'Proszę bardzo ', 'Teraz ', 'Natychmiast ',
                       'W tej chwili ', 'Już ', 'Szybko ', 'Od razu ', 'No ',
                       'Proszę natychmiast ', 'Szybciutko ', 'Chciałbym teraz '):
            with self.subTest(prefix=prefix):
                decision = result(prefix+'Otwórz Markiza', rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments['name'], 'Markiza')
        for text in ('Może otwórz Markiza', 'Czy otwórz Markiza', 'Markiza otwórz'):
            with self.subTest(text=text):
                decision = result(text, rows)
                self.assertEqual(decision.kind, router.DecisionKind.PASS)
                self.assertIsNone(decision.tool_call)

    def test_cover_percentages_never_fall_back_to_full_opening(self):
        for text in ('Otwórz zasłony w salonie w 50%.',
                     'Otwórz zasłony w salonie w pięćdziesięciu procentach.',
                     'Otwórz zasłony w 50 procentach.'):
            with self.subTest(text=text):
                decision = result(text, [('zasłony', 'cover', 'Salon')])
                self.assertEqual(decision.kind, router.DecisionKind.CLARIFY)
                self.assertEqual(decision.reason, 'position-unavailable')
                self.assertIsNone(decision.tool_call)
        decision = result('Otwórz zasłony 50%.', [('zasłony', 'cover', 'Salon')])
        self.assertEqual(decision.kind, router.DecisionKind.PASS)
        self.assertEqual(decision.reason, 'outside-grammar')
        self.assertIsNone(decision.tool_call)

    def test_numeric_names_and_context_only_area_names_remain_supported(self):
        for area in ('Atrium północne', 'Pokój 50', 'Strefa X-12'):
            with self.subTest(area=area):
                rows = [('Markiza 2', 'cover', area)]
                decision = result('Otwórz Markiza 2 w '+area, rows)
                self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                self.assertEqual(decision.tool_call.arguments['area'], area)
                self.assertEqual(decision.tool_call.arguments['name'], 'Markiza 2')

    def test_repeated_spaces_in_friendly_name_are_preserved_in_ha_selector(self):
        for name in ('zasłony  Zasłona', 'żaluzje  Sterownik'):
            rows = [(name, 'cover', 'Atrium północne')]
            for spoken in (name, name.split('  ')[-1]):
                with self.subTest(name=name, spoken=spoken):
                    decision = result('Otwórz '+spoken+' w Atrium północne', rows)
                    self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
                    self.assertEqual(decision.tool_call.arguments['name'], name)

    def test_repeated_spaces_are_not_area_alias_separators(self):
        decision = result('Otwórz Markiza w Atrium północne',
                          [('Markiza', 'cover', 'Atrium  północne')])
        self.assertEqual(decision.kind, router.DecisionKind.TOOLCALL)
        self.assertEqual(decision.tool_call.arguments['area'], 'Atrium  północne')

    def test_short_name_parts_do_not_hide_collisions(self):
        rows = [('Rolety  Sterownik', 'cover', 'Atrium'),
                ('Rolety  Napęd', 'cover', 'Atrium')]
        decision = result('Otwórz Rolety w Atrium', rows)
        self.assertEqual(decision.kind, router.DecisionKind.CLARIFY)
        self.assertIsNone(decision.tool_call)

    def test_missing_catalog_blocks_new_action_but_not_ack(self):
        unavailable = catalog.parse_overview([])
        messages = [{'role': 'user', 'content': 'Włącz Lampa'}]
        decision = router.decide(messages, unavailable, tools(), language=get_language('pl'))
        self.assertEqual((decision.kind, decision.reason),
                         (router.DecisionKind.PASS, 'catalog-unavailable'))
        self.assertIsNone(decision.tool_call)
        messages += [
            {'role': 'assistant', 'tool_calls': [{'id': 'call_test', 'type': 'function',
                'function': {'name': 'intent__HassTurnOn', 'arguments': '{"name":"Lampa"}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_test', 'content': '{"success":true}'},
        ]
        decision = router.decide(messages, unavailable, tools(), language=get_language('pl'))
        self.assertEqual(decision.kind, router.DecisionKind.ACK)

    def test_schema_and_tool_choice_respected(self):
        messages = [{'role': 'user', 'content': 'Włącz lampę'}]
        for mutation in ('required', 'enum', 'bad-enum', 'ref', 'unused-ref', 'missing-domain', 'wrong-type', 'no-schema'):
            available = tools()
            fn = available[0]['function']
            schema = fn['parameters']
            if mutation == 'required':
                schema['required'].append('brightness')
            elif mutation == 'enum':
                schema['properties']['domain']['items']['enum'] = ['cover']
            elif mutation == 'bad-enum':
                schema['properties']['name']['enum'] = 'Lampa'
            elif mutation == 'ref':
                schema['$ref'] = 'https://invalid.example/schema'
            elif mutation == 'unused-ref':
                schema['properties']['unused'] = {'$ref': 'https://invalid.example/schema'}
            elif mutation == 'missing-domain':
                del schema['properties']['domain']
            elif mutation == 'wrong-type':
                schema['properties']['area']['type'] = 'array'
            else:
                del fn['parameters']
            with self.subTest(mutation=mutation):
                self.assertEqual(router.decide(messages, overview([('Lampa','light','Salon')]), available).kind,
                                 router.DecisionKind.PASS)
        for choice in ('none', {'type':'function','function':{'name':'HassTurnOff'}}):
            self.assertEqual(result('Włącz lampę', tool_choice=choice).kind, router.DecisionKind.PASS)

    def test_forced_tool_alias_is_the_one_emitted(self):
        available = tools()
        available.append(copy.deepcopy(available[0]))
        available[-1]['function']['name'] = 'HassTurnOn'
        decision = router.decide([{'role':'user','content':'Włącz lampę'}],
            overview([('Lampa','light','Salon')]), available,
            tool_choice={'type':'function','function':{'name':'HassTurnOn'}})
        self.assertEqual(decision.tool_call.name, 'HassTurnOn')

    def history(self, content):
        return [{'role':'user','content':'Włącz lampę'},
                {'role':'assistant','content':None,'tool_calls':[
                    {'id':'call1','type':'function','function':{'name':'HassTurnOn','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'call1','content':content}]

    def test_success_failure_unknown_and_new_turn(self):
        current = overview([('Lampa','light','Salon')])
        for content, kind, response in [
                ('{"success":true}', 'ack', 'Wykonano'),
                ('{"data":{"success":[{}],"failed":[]}}', 'ack', 'Wykonano'),
                ('{"error":"failed"}', 'pass', None),
                ('{"success":false}', 'pass', None),
                ('{"data":{"failed":[{}]}}', 'pass', None),
                ('unknown result', 'pass', None), ('{}', 'pass', None)]:
            with self.subTest(content=content):
                decision = router.decide(self.history(content), current, tools())
                self.assertEqual(decision.kind.value, kind)
                self.assertEqual(decision.response, response)
        history = self.history('{"success":true}')
        history.append({'role':'user','content':'Wyłącz lampę'})
        decision = router.decide(history, current, tools())
        self.assertEqual(decision.tool_call.name, 'intent__HassTurnOff')

    def test_primary_name_collision_after_unique_alias_match(self):
        rows = [('Lampa, Biurkowa', 'light', 'Salon'), ('Lampa, Sufitowa', 'light', 'Salon')]
        self.assertEqual(result('Włącz Biurkowa', rows).kind, router.DecisionKind.CLARIFY)

    def test_pending_and_multiple_results_pass(self):
        current = overview([('Lampa','light','Salon')])
        history = self.history('{"success":true}')
        self.assertEqual(router.decide(history[:-1], current, tools()).kind, router.DecisionKind.PASS)
        history.append(history[-1].copy())
        self.assertEqual(router.decide(history, current, tools()).kind, router.DecisionKind.PASS)

    def test_gateway_routes_json_and_sse_without_upstream(self):
        decision = result('Włącz lampę')
        for stream in (False, True):
            handler_type = gateway.make_handler('http://127.0.0.1:1')
            handler = object.__new__(handler_type)
            handler.wfile = io.BytesIO()
            handler.request_version = 'HTTP/1.1'
            handler.requestline = 'POST /v1/chat/completions HTTP/1.1'
            handler.command = 'POST'
            handler.client_address = ('127.0.0.1', 1)
            handler.send_router_decision(decision, {'stream': stream})
            payload = handler.wfile.getvalue().decode()
            self.assertIn('intent__HassTurnOn', payload)
            if stream:
                self.assertIn('data: [DONE]', payload)
            else:
                body = json.loads(payload.split('\r\n\r\n')[1])
                args = body['choices'][0]['message']['tool_calls'][0]['function']['arguments']
                self.assertEqual(json.loads(args), decision.tool_call.arguments)

    def test_counter_is_standalone_and_experimental_bound_is_preserved(self):
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                self.messages, self.options = messages, kwargs
                return 'rendered'
            def encode(self, text, **kwargs):
                return [1, 2, 3]
        tokenizer = Tokenizer()
        body = {'messages':[{'role':'user','content':'Test'}], 'tools': tools()}
        count = counter.estimate(body, {'tokenizer.chat_template':'template'}, tokenizer,
                                 counter.normalize_go_http_messages)
        self.assertEqual(count['prompt_upper_bound'], 3+1024+16+2*16)
        self.assertFalse(count['native_parity_certified'])
        self.assertEqual(tokenizer.options['tools'], body['tools'])

    def test_log_sinks_keep_distinct_files_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cls,filename in [(gateway.MetricsLogger,'metrics.jsonl'),
                                 (gateway.ContextAuditLogger,'context-audit.jsonl')]:
                directory=Path(tmp)/filename.removesuffix('.jsonl')
                logger=cls(directory)
                try: logger.write({'kind':'pass','reason':'synthetic fixture'})
                finally: logger.close()
                path=directory/filename
                self.assertEqual(json.loads(path.read_text())['kind'],'pass')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)

    def test_gateway_requires_a_prompt_counter_before_listening(self):
        proc = subprocess.run([sys.executable, str(GATEWAY/'chat_gateway.py')],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('prompt counter is required', proc.stderr)


if __name__ == '__main__':
    unittest.main()
