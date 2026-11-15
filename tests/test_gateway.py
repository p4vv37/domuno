"""Publication wrapper regression tests; synthetic HA data, no network or NPU."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import stat
import sys
import unittest

GATEWAY = Path(__file__).resolve().parents[1] / 'gateway'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, GATEWAY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Isolate this publication copy from the older experimental scripts in tests/.
previous = sys.modules.get('qwen_context_catalog')
catalog = load('qwen_context_catalog', 'qwen_context_catalog.py')
router = load('publication_decision', 'qwen_decision.py')
if previous is not None:
    sys.modules['qwen_context_catalog'] = previous
else:
    del sys.modules['qwen_context_catalog']
gateway = load('publication_gateway', 'qwen_npu_gateway.py')
counter = load('publication_counter', 'qwen_dynamic_budget_server.py')


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

    @unittest.skipIf(router._simplemma is None, 'simplemma not installed; run this case on Arduino')
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
                self.assertEqual(result(text, rows).kind, router.DecisionKind.CLARIFY)

    def test_unsupported_grammar_passes_without_losing_parameters(self):
        for text in ('Otwórz roletę w sypialni na 30%', 'Włącz lampę i wyłącz wiatrak',
                     'Włącz lampę za minutę', 'Jeśli jest ciemno włącz lampę',
                     'Czy możesz włączyć lampę?', 'Jaka jest pogoda?'):
            with self.subTest(text=text):
                self.assertEqual(result(text).kind, router.DecisionKind.PASS)
        self.assertEqual(result('Nie włącz lampę').reason, 'negation')

    def test_schema_and_tool_choice_respected(self):
        messages = [{'role': 'user', 'content': 'Włącz lampę'}]
        for mutation in ('required', 'enum', 'ref', 'missing-domain', 'wrong-type', 'no-schema'):
            available = tools()
            fn = available[0]['function']
            schema = fn['parameters']
            if mutation == 'required':
                schema['required'].append('brightness')
            elif mutation == 'enum':
                schema['properties']['domain']['items']['enum'] = ['cover']
            elif mutation == 'ref':
                schema['$ref'] = 'https://invalid.example/schema'
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
                ('{"error":"failed"}', 'ack', 'Nie udało się.'),
                ('{"success":false}', 'ack', 'Nie udało się.'),
                ('{"data":{"failed":[{}]}}', 'ack', 'Nie udało się.'),
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

    def test_shadow_log_contains_no_names_or_arguments(self):
        sanitized = router.sanitize(result('Włącz lampę'))
        self.assertNotIn('arguments', sanitized)
        self.assertNotIn('Lampa', json.dumps(sanitized))

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
            for cls,filename in [(gateway.ResponseLogger,'responses.jsonl'),
                                 (gateway.DecisionShadowLogger,'decision-shadow.jsonl'),
                                 (gateway.ContextAuditLogger,'context-audit.jsonl')]:
                directory=Path(tmp)/filename.removesuffix('.jsonl')
                logger=cls(directory)
                try: logger.write({'kind':'pass','reason':'synthetic fixture'})
                finally: logger.close()
                path=directory/filename
                self.assertEqual(json.loads(path.read_text())['kind'],'pass')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)

    def test_large_context_without_counter_rejected_before_listening(self):
        proc = subprocess.run([sys.executable, str(GATEWAY/'qwen_npu_gateway.py'), '--nctx','10240'],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('budget guard', proc.stderr)


if __name__ == '__main__':
    unittest.main()
