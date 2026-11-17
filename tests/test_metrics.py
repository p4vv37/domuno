"""Metrics regressions with synthetic responses. No services or model loads."""
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

GATEWAY = Path(__file__).resolve().parents[1] / 'gateway'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, GATEWAY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


metrics = load('qwen_metrics', 'qwen_metrics.py')
gateway = load('metrics_gateway', 'qwen_npu_gateway.py')
report = load('metrics_report', 'qwen_latency_stats.py')
catalog = load('metrics_catalog', 'qwen_context_catalog.py')


def event(obj):
    return b'data: ' + json.dumps(obj, ensure_ascii=False).encode() + b'\r\n\r\n'


class ContextAuditTests(unittest.TestCase):
    def body(self):
        return {'messages': [{'role': 'system', 'content': catalog.HEADING +
            '\n- names: PRIVATE lamp\n  domain: light\n  areas: PRIVATE room'},
            {'role': 'user', 'content': 'PRIVATE question'}],
            'tools': [{'type': 'function', 'function': {'name': name,
                       'parameters': {'type': 'object', 'properties': {}}}}
                      for name in ('PRIVATE_on', 'PRIVATE_off')]}

    def test_tool_order_changes_are_distinct_from_definition_changes(self):
        body = self.body()
        first = catalog.audit_request(body)
        body['tools'].reverse()
        reordered = catalog.audit_request(body)
        self.assertNotEqual(first['tools_order_hash'], reordered['tools_order_hash'])
        self.assertEqual(first['tools_set_hash'], reordered['tools_set_hash'])
        body['tools'][0]['function']['description'] = 'changed definition'
        changed = catalog.audit_request(body)
        self.assertNotEqual(changed['tools_set_hash'], reordered['tools_set_hash'])
        self.assertEqual(first['system_hash'], changed['system_hash'])
        self.assertNotIn('PRIVATE', json.dumps(changed))

    def test_mapping_order_and_question_changes_remain_observable(self):
        body = self.body()
        first = catalog.audit_request(body)
        body['tools'][0]['function'] = dict(reversed(list(body['tools'][0]['function'].items())))
        mapping_order = catalog.audit_request(body)
        self.assertEqual(first['tools_order_hash'], mapping_order['tools_order_hash'])
        self.assertNotEqual(first['tools_wire_hash'], mapping_order['tools_wire_hash'])
        body['messages'][-1]['content'] = 'different question'
        question = catalog.audit_request(body)
        self.assertEqual(first['system_hash'], question['system_hash'])
        self.assertEqual(first['catalog_hash'], question['catalog_hash'])
        self.assertNotEqual(first['message_hashes'][-1], question['message_hashes'][-1])
        body['messages'][0]['content'] = body['messages'][0]['content'].replace('PRIVATE room', 'different room')
        context = catalog.audit_request(body)
        self.assertNotEqual(first['catalog_hash'], context['catalog_hash'])
        self.assertNotEqual(first['system_hash'], context['system_hash'])

    def test_unknown_roles_and_unrecognized_catalog_do_not_leak_input(self):
        row = catalog.audit_request({'messages': [{'role': 'PRIVATE role', 'content': 'PRIVATE'}]})
        self.assertIsNone(row['catalog_hash'])
        self.assertEqual(row['message_roles'], ['unknown'])
        self.assertNotIn('PRIVATE', json.dumps(row))
        bounded = catalog.audit_request({'messages': [{}]*300})
        self.assertEqual(bounded['message_count'], 300)
        self.assertEqual(len(bounded['message_hashes']), 256)
        self.assertFalse(bounded['message_fingerprints_complete'])

    def test_full_capture_is_private_and_stops_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = gateway.RequestCaptureLogger(tmp, limit=2)
            try:
                for i in range(3):
                    sink.write({'request_id': str(i), 'body': self.body()})
            finally:
                sink.close()
            path = Path(tmp)/'request-capture.jsonl'
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row['request_id'] for row in rows], ['0', '1'])
            self.assertEqual(rows[0]['body'], self.body())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_log_configuration_disables_capture_and_audit(self):
        for switch in ('CONTEXT_AUDIT', 'CONTEXT_CAPTURE'):
            with patch.dict(gateway.os.environ, {switch: '0'}):
                self.assertIsNone(gateway.configured_log_directory('/unused', switch))
            with patch.dict(gateway.os.environ, {switch: '1'}):
                self.assertEqual(gateway.configured_log_directory('/unused', switch), '/unused')
                with self.assertRaises(ValueError): gateway.configured_log_directory(None, switch)
            with patch.dict(gateway.os.environ, {switch: 'invalid'}):
                with self.assertRaises(ValueError): gateway.configured_log_directory('/unused', switch)


class StartupTests(unittest.TestCase):
    def test_readiness_follows_language_warmup_and_socket_binding(self):
        events = []
        decision = SimpleNamespace(decide=lambda *a, **kw: None,
                                   clarify_text=lambda *a: '', warmup=lambda language: events.append('warmup'))
        catalog = SimpleNamespace(parse_overview=lambda _: None)
        class Server:
            def __init__(self, *args, **kwargs): events.append('bind')
            def serve_forever(self):
                events.append('serve')
                raise KeyboardInterrupt()
            def server_close(self): events.append('close')
        with patch.dict(sys.modules, {'qwen_decision': decision, 'qwen_context_catalog': catalog}), \
             patch.object(sys, 'argv', ['gateway', '--decide-apply', '--prompt-counter-url', 'http://127.0.0.1:1/count']), \
             patch.object(gateway, 'ThreadingHTTPServer', Server), \
             patch.object(gateway, 'notify_ready', side_effect=lambda: events.append('ready')), \
             patch('sys.stdout', new=io.StringIO()):
            gateway.main()
        self.assertEqual(events, ['warmup', 'bind', 'ready', 'serve', 'close'])

    def test_configured_english_strategy_reaches_warmup_and_router(self):
        selected = []
        decision = SimpleNamespace(decide=lambda *args, **kw: selected.append(kw['language'].code),
                                   clarify_text=lambda *a: '',
                                   warmup=lambda language: selected.append(language.code))
        overview = SimpleNamespace(parse_overview=lambda _: None)
        class Server:
            daemon_threads = False
            def __init__(self, address, handler): pass
            def serve_forever(self): raise KeyboardInterrupt()
            def server_close(self): pass
        make = gateway.make_handler
        def handler(*args, **kwargs):
            self.assertEqual(kwargs['agent_language'], 'en')
            kwargs['decision_fn']({'messages': []})
            return make(*args, **kwargs)
        with patch.dict(sys.modules, {'qwen_decision': decision, 'qwen_context_catalog': overview}), \
             patch.dict(gateway.os.environ, {'AGENT_LANGUAGE': 'en', 'ROUTER_VOCABULARY': ''}), \
             patch.object(sys, 'argv', ['gateway', '--decide-apply', '--prompt-counter-url', 'http://127.0.0.1:1/count']), \
             patch.object(gateway, 'ThreadingHTTPServer', Server), \
             patch.object(gateway, 'make_handler', side_effect=handler), \
             patch.object(gateway, 'notify_ready'), patch('sys.stdout', new=io.StringIO()):
            gateway.main()
        self.assertEqual(selected, ['en', 'en'])

    def test_systemd_notification_uses_abstract_socket_without_running_a_service(self):
        with patch.dict(gateway.os.environ, {'NOTIFY_SOCKET': '@test-ready'}), \
             patch.object(gateway.socket, 'socket') as create:
            gateway.notify_ready()
            create.return_value.__enter__.return_value.sendto.assert_called_once_with(b'READY=1', '\0test-ready')
        with patch.dict(gateway.os.environ, {'NOTIFY_SOCKET': ''}), \
             patch.object(gateway.socket, 'socket') as create:
            gateway.notify_ready()
            create.assert_not_called()


class ObserverTests(unittest.TestCase):
    def test_fragmented_sse_tracks_payload_not_role_and_preserves_usage(self):
        now = [10.0]
        observer = metrics.ResponseObserver(True, clock=lambda: now[0])
        role = event({'choices': [{'delta': {'role': 'assistant'}, 'finish_reason': None}]})
        for byte in role:
            observer.feed(bytes([byte]))
        self.assertNotIn('ttft_ms', observer.values)
        now[0] = 10.5
        payload = event({'choices': [{'delta': {'content': 'Zażółć'}, 'finish_reason': None}]})
        for byte in payload:
            observer.feed(bytes([byte]))
        observer.feed(event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}))
        observer.feed(event({'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 3,
                                'prompt_tokens_details': {'cached_tokens': 80}},
                             'timings': {'prompt_ms': 50, 'predicted_ms': 20, 'prompt_n': 20}}))
        observer.feed(b'data: [DONE]\r\n\r\n')
        result = observer.finish()
        self.assertEqual(result['ttft_ms'], 500)
        self.assertEqual(result['response_kind'], 'text')
        self.assertEqual(result['cached_tokens'], 80)
        self.assertEqual(result['evaluated_prompt_tokens'], 20)
        self.assertEqual(result['prefill_ms'], 50)
        self.assertTrue(result['observation_complete'])
        self.assertNotIn('Zażółć', json.dumps(result))

    def test_tool_name_metadata_is_not_first_token(self):
        observer = metrics.ResponseObserver(True)
        observer.feed(event({'choices': [{'delta': {'tool_calls': [
            {'function': {'name': 'private_tool'}}]}}]}))
        self.assertNotIn('ttft_ms', observer.values)
        observer.feed(event({'choices': [{'delta': {'tool_calls': [
            {'function': {'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]}))
        result = observer.finish()
        self.assertEqual(result['response_kind'], 'toolcall')
        self.assertIsNotNone(result['ttft_ms'])
        self.assertNotIn('private_tool', json.dumps(result))

    def test_json_unknown_values_empty_and_invalid_observations(self):
        observer = metrics.ResponseObserver(False)
        observer.feed(json.dumps({'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}],
                                   'usage': {'prompt_tokens': '100', 'completion_tokens': True},
                                   'timings': {'prompt_ms': float('nan')}}).encode())
        result = observer.finish()
        self.assertEqual(result['response_kind'], 'empty')
        self.assertTrue(result['observation_complete'])
        self.assertNotIn('ttft_ms', result)
        self.assertNotIn('prompt_tokens', result)
        self.assertNotIn('prefill_ms', result)
        for stream, raw in ((False, b'not JSON'), (True, b'data: {"choices":'),
                            (True, b'data: ' + b'x' * (metrics.ResponseObserver.LIMIT + 1))):
            observer = metrics.ResponseObserver(stream)
            observer.feed(raw)
            result = observer.finish()
            self.assertEqual(result['response_kind'], 'unknown')
            self.assertFalse(result['observation_complete'])
            self.assertLessEqual(len(observer.buffer) + len(observer.event), observer.LIMIT)


class FakeResponse:
    status = 200
    length = None
    def __init__(self, chunks): self.chunks = list(chunks)
    def read(self, limit): return b'{}'
    def read1(self, limit): return self.chunks.pop(0) if self.chunks else b''
    def getheader(self, *args): return 'application/json'


class GatewayMetricsTests(unittest.TestCase):
    def request(self, response=None, body=None, raw=None, sink=None, reset_status=200, disconnect=False, backend_error=False, **options):
        records = []
        sink = sink or SimpleNamespace(write=lambda record: records.append(dict(record)))
        handler_type = gateway.make_handler('http://127.0.0.1:1', metrics_logger=sink,
                                            backend_version='test', metrics_profile='fixture', **options)
        handler = object.__new__(handler_type)
        body = body if body is not None else {'messages': [{'role': 'user', 'content': 'PRIVATE command'}]}
        raw = raw if raw is not None else json.dumps(body).encode()
        handler.path = '/v1/chat/completions'
        handler.headers = Message()
        handler.headers['Content-Length'] = str(len(raw))
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        class Writer(io.BytesIO):
            def write(self, data):
                if disconnect and b'PRIVATE response' in data:
                    raise BrokenPipeError('client closed')
                return super().write(data)
        handler.rfile, handler.wfile = io.BytesIO(raw), Writer()
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST /v1/chat/completions HTTP/1.1'
        handler.command = 'POST'
        handler.client_address = ('127.0.0.1', 1)
        handler.log_request = lambda *args: None
        response = response or FakeResponse([json.dumps({'choices': [
            {'message': {'content': 'PRIVATE response'}, 'finish_reason': 'stop'}]}).encode()])
        reset = FakeResponse([])
        reset.status = reset_status
        self.forwarded = []
        def send(*args, **kwargs):
            self.forwarded.append(json.loads(kwargs['body']))
            if backend_error: raise OSError('PRIVATE backend details')
        conn = SimpleNamespace(request=send,
                               getresponse=iter([reset, response] if options.get('cache_policy') == 'reset' else [response]).__next__, close=lambda: None)
        with patch.object(gateway.http.client, 'HTTPConnection', return_value=conn):
            handler.do_POST()
        return handler.wfile.getvalue().split(b'\r\n\r\n', 1)[-1], records

    def test_json_and_sse_bytes_are_unchanged_and_metrics_are_payload_free(self):
        for stream in (False, True):
            obj = {'choices': [{'delta' if stream else 'message': {'content': 'PRIVATE response'},
                                 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 100, 'completion_tokens': 5,
                             'prompt_tokens_details': {'cached_tokens': 70}}}
            raw = event(obj) + b'data: [DONE]\n\n' if stream else json.dumps(obj).encode()
            output, rows = self.request(FakeResponse([raw[:11], raw[11:]]),
                                       body={'messages': [], 'stream': stream})
            self.assertEqual(output, raw)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row['outcome'], row['route']), ('completed', 'llm'))
            self.assertEqual(row['cached_tokens'], 70)
            self.assertIsNone(row['reset_ms'])
            self.assertIsNotNone(row['backend_ms'])
            self.assertEqual(row['backend_version'], 'test')
            self.assertEqual(row['cache_policy'], 'reuse')
            self.assertIsNone(row['estimated_prompt_tokens'])
            self.assertNotIn('PRIVATE', json.dumps(row))
            self.assertEqual(row['ttft_ms'] is not None, stream)

    def test_reuse_sends_only_real_request_and_reset_is_explicit(self):
        for policy, expected_count in (('reuse', 1), ('reset', 2)):
            with self.subTest(policy=policy):
                _, rows = self.request(cache_policy=policy)
                self.assertEqual(len(self.forwarded), expected_count)
                self.assertEqual(self.forwarded[-1]['messages'][-1]['content'], 'PRIVATE command')
                self.assertEqual(rows[0]['cache_policy'], policy)
                self.assertEqual(rows[0]['reset_ms'] is not None, policy == 'reset')
                if policy == 'reset':
                    self.assertEqual(self.forwarded[0]['messages'], gateway.RESET_MESSAGES)
        with self.assertRaises(ValueError):
            gateway.make_handler('http://127.0.0.1:1', cache_policy='invalid')
        for policy in ('reuse', 'reset'):
            self.request(cache_policy=policy, prompt_counter=lambda _: 99999)
            self.assertEqual(self.forwarded, [])

    def test_audit_and_capture_observe_normalized_request_without_mutation(self):
        audits, captures = [], []
        body = ContextAuditTests().body()
        original = json.dumps(body)
        _, metrics_rows = self.request(body=body,
            context_auditor=SimpleNamespace(write=audits.append), context_audit_fn=catalog.audit_request,
            request_capture=SimpleNamespace(write=captures.append))
        self.assertEqual(json.dumps(body), original)
        self.assertEqual(captures[0]['body'], self.forwarded[0])
        self.assertTrue(captures[0]['body']['messages'][0]['content'].startswith(gateway.VOICE_STYLE))
        self.assertEqual(audits[0]['request_hash'], catalog.audit_request(self.forwarded[0])['request_hash'])
        self.assertEqual(audits[0]['request_id'], metrics_rows[0]['request_id'])
        self.assertEqual(captures[0]['request_id'], metrics_rows[0]['request_id'])
        self.assertNotIn('PRIVATE', json.dumps(audits))
        self.assertIsNotNone(metrics_rows[0]['audit_ms'])
        self.assertIsNotNone(metrics_rows[0]['capture_ms'])
        self.assertEqual(len(self.forwarded), 1)

    def test_router_and_budget_rejections_are_captured_for_diagnosis(self):
        decision = SimpleNamespace(kind=SimpleNamespace(value='clarify'), reason='position-unavailable')
        for options in ({'prompt_counter': lambda _: 99999},
                        {'decide_apply': True, 'decision_fn': lambda _: decision}):
            audits, captures = [], []
            self.request(context_auditor=SimpleNamespace(write=audits.append),
                context_audit_fn=catalog.audit_request,
                request_capture=SimpleNamespace(write=captures.append), **options)
            self.assertEqual(len(audits), 1)
            self.assertEqual(len(captures), 1)
            self.assertIn('messages', captures[0]['body'])
            self.assertEqual(self.forwarded, [])

    def test_failed_audit_or_capture_does_not_retry_or_change_response(self):
        def failed(_): raise OSError('PRIVATE failure')
        with patch('sys.stderr', new=io.StringIO()) as errors:
            output, rows = self.request(context_auditor=SimpleNamespace(write=failed),
                context_audit_fn=catalog.audit_request, request_capture=SimpleNamespace(write=failed))
        self.assertEqual(rows[0]['outcome'], 'completed')
        self.assertEqual(json.loads(output)['choices'][0]['message']['content'], 'PRIVATE response')
        self.assertEqual(len(self.forwarded), 1)
        self.assertNotIn('PRIVATE', errors.getvalue())

    def test_response_style_preserves_input_tools_and_overview(self):
        original = {'messages': [{'role': 'system', 'content': 'Entity overview:\n- names: Lamp\n  domain: light'},
                                 {'role': 'user', 'content': 'PRIVATE command'}],
                    'tools': [{'type': 'function', 'function': {'name': 'test'}}]}
        frozen = json.dumps(original)
        self.request(body=original)
        sent = self.forwarded[-1]
        self.assertTrue(sent['messages'][0]['content'].startswith(gateway.VOICE_STYLE))
        self.assertTrue(sent['messages'][0]['content'].endswith(original['messages'][0]['content']))
        self.assertEqual(sent['tools'], original['tools'])
        self.assertEqual(json.dumps(original), frozen)
        self.request(body=original, response_style='original')
        self.assertEqual(self.forwarded[-1]['messages'], original['messages'])
        self.assertNotIn(gateway.VOICE_STYLE, json.dumps(self.forwarded[-1]))

    def test_private_miss_log_contains_only_latest_user_text_for_pass(self):
        decision = SimpleNamespace(kind=SimpleNamespace(value='pass'), reason='no-command')
        rows = []
        miss_sink = SimpleNamespace(write=lambda row: rows.append(row))
        body = {'messages': [{'role': 'system', 'content': 'PRIVATE catalog'},
                             {'role': 'user', 'content': 'old turn'},
                             {'role': 'user', 'content': 'x'*600}]}
        self.request(body=body, decision_fn=lambda _: decision, router_miss_logger=miss_sink)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['utterance'], 'x'*512)
        self.assertTrue(rows[0]['truncated'])
        self.assertNotIn('PRIVATE catalog', json.dumps(rows))
        self.assertNotIn('old turn', json.dumps(rows))
        self.request(body={'messages': [{'role': 'tool', 'content': 'PRIVATE result'}]},
                     decision_fn=lambda _: decision, router_miss_logger=miss_sink)
        self.assertEqual(len(rows), 1)

    def test_private_miss_log_stops_at_fixed_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [100]
            sink = gateway.RouterMissLogger(tmp, until=200, clock=lambda: now[0])
            try:
                sink.write({'utterance': 'first'})
                now[0] = 200
                sink.write({'utterance': 'expired'})
            finally:
                sink.close()
            path = Path(tmp)/'router-misses.jsonl'
            self.assertEqual([json.loads(line)['utterance'] for line in path.read_text().splitlines()], ['first'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_miss_logging_can_be_toggled_without_reinstalling(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/'private'
            with patch.dict(gateway.os.environ, {'ROUTER_MISS_LOGGING': '0'}):
                self.assertIsNone(gateway.router_miss_logger(directory))
                self.assertFalse(directory.exists())
            with patch.dict(gateway.os.environ, {'ROUTER_MISS_LOGGING': '1', 'ROUTER_MISS_UNTIL': ''}):
                sink = gateway.router_miss_logger(directory)
                try:
                    self.assertIsNone(sink.until)
                    sink.write({'utterance': 'synthetic fixture'})
                finally:
                    sink.close()
            with patch.dict(gateway.os.environ, {'ROUTER_MISS_LOGGING': '0'}):
                self.assertIsNone(gateway.router_miss_logger(directory))
            self.assertEqual(len((directory/'router-misses.jsonl').read_text().splitlines()), 1)

    def test_private_miss_logging_validates_configured_cutoff_and_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(gateway.os.environ, {'ROUTER_MISS_LOGGING': '1',
                                                'ROUTER_MISS_UNTIL': '2026-11-24T00:00:00Z'}):
                sink = gateway.router_miss_logger(tmp)
                try:
                    self.assertEqual(sink.until, 1795478400)
                finally:
                    sink.close()
            for values in ({'ROUTER_MISS_LOGGING': 'yes'},
                           {'ROUTER_MISS_LOGGING': '1', 'ROUTER_MISS_UNTIL': 'tomorrow'},
                           {'ROUTER_MISS_LOGGING': '1', 'ROUTER_MISS_UNTIL': '2026-11-24'}):
                with self.subTest(values=values), patch.dict(gateway.os.environ, values):
                    with self.assertRaises(ValueError):
                        gateway.router_miss_logger(tmp)

    def test_rejections_and_router_are_recorded_without_backend(self):
        cases = [({'raw': b'{'}, 400), ({'api_key': 'secret'}, 401),
                 ({'prompt_counter': lambda _: 99999}, 413)]
        for kwargs, status in cases:
            with self.subTest(status=status):
                _, rows = self.request(**kwargs)
                self.assertEqual(rows[0]['status'], status)
                self.assertEqual(rows[0]['outcome'], 'rejected')
                self.assertIsNone(rows[0]['backend_ms'])
        decision = SimpleNamespace(kind=SimpleNamespace(value='ack'), response='Wykonano')
        _, rows = self.request(decide_apply=True, decision_fn=lambda _: decision)
        self.assertEqual(rows[0]['route'], 'ack')
        self.assertEqual(rows[0]['outcome'], 'completed')
        self.assertIsNone(rows[0]['reset_ms'])

    def test_empty_truncated_or_failed_backend_does_not_count_as_completed(self):
        for raw, outcome in (({'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}]}, 'empty'),
                             ({'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}]}, 'incomplete'),
                             ({'error': 'PRIVATE'}, 'unverified')):
            _, rows = self.request(FakeResponse([json.dumps(raw).encode()]))
            self.assertEqual(rows[0]['outcome'], outcome)
        response = FakeResponse([b'{"error":"PRIVATE"}'])
        response.status = 503
        _, rows = self.request(response)
        self.assertEqual(rows[0]['outcome'], 'backend_error')

    def test_backend_failure_reset_failure_queue_timeout_and_disconnect(self):
        _, rows = self.request(backend_error=True)
        self.assertEqual(rows[0]['status'], 502)
        self.assertEqual(rows[0]['outcome'], 'backend_error')
        self.assertIsNotNone(rows[0]['backend_ms'])
        self.assertIsNone(rows[0]['reset_ms'])
        self.assertNotIn('PRIVATE', json.dumps(rows))
        _, rows = self.request(reset_status=500, cache_policy='reset')
        self.assertEqual(rows[0]['outcome'], 'backend_error')
        self.assertIsNotNone(rows[0]['reset_ms'])
        _, rows = self.request(disconnect=True)
        self.assertEqual(rows[0]['outcome'], 'client_disconnected')
        with patch.object(gateway.threading, 'BoundedSemaphore', return_value=SimpleNamespace(acquire=lambda **kw: False)):
            _, rows = self.request()
        self.assertEqual(rows[0]['status'], 429)
        self.assertEqual(rows[0]['outcome'], 'rejected')
        self.assertIsNotNone(rows[0]['queue_ms'])
        self.assertIsNone(rows[0]['reset_ms'])

    def test_oversized_observation_is_forwarded_but_unverified(self):
        raw = b'PRIVATE ' * (metrics.ResponseObserver.LIMIT // 8 + 1)
        output, rows = self.request(FakeResponse([raw]))
        self.assertEqual(output, raw)
        self.assertEqual(rows[0]['outcome'], 'unverified')
        self.assertEqual(rows[0]['response_kind'], 'unknown')
        self.assertNotIn('PRIVATE', json.dumps(rows))

    def test_failed_sink_does_not_change_response(self):
        def fail(_): raise OSError('disk unavailable')
        output, _ = self.request(sink=SimpleNamespace(write=fail))
        self.assertIn(b'PRIVATE response', output)

    def test_rotated_metrics_remain_readable_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = gateway.MetricsLogger(tmp)
            sink.handler.maxBytes = 400
            try:
                for i in range(12): sink.write({'schema_version': 1, 'service': 'gateway', 'index': i})
            finally:
                sink.close()
            paths = list(Path(tmp).glob('metrics.jsonl*'))
            self.assertGreater(len(paths), 1)
            self.assertLessEqual(len(paths), 3)
            for path in paths:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for line in path.read_text().splitlines(): self.assertEqual(json.loads(line)['schema_version'], 1)


class ReportTests(unittest.TestCase):
    def test_languages_are_not_combined_in_reports(self):
        rows = [{'service': 'gateway', 'route': 'llm', 'outcome': 'completed',
                 'agent_language': code, 'total_ms': elapsed}
                for code, elapsed in (('pl', 200), ('en', 300))]
        groups = report.summarize(rows)
        self.assertEqual({group['agent_language'] for group in groups}, {'pl', 'en'})
        self.assertTrue(all(group['requests'] == 1 for group in groups))
    def test_reports_exclude_empty_and_failed_latencies_and_keep_versions_separate(self):
        base = {'schema_version': 1, 'service': 'gateway', 'route': 'llm',
                'backend_version': '0.9.3', 'profile': 'baseline', 'outcome': 'completed'}
        rows = [{**base, 'total_ms': n} for n in (10, 20, 30)]
        rows += [{**base, 'total_ms': 1, 'outcome': 'empty'},
                 {**base, 'total_ms': 2, 'outcome': 'backend_error'},
                 {**base, 'backend_version': '0.11.0', 'total_ms': 3,
                  'prompt_tokens': 100, 'cached_tokens': 80}]
        result = report.summarize(rows)
        old = next(group for group in result if group['backend_version'] == '0.9.3')
        new = next(group for group in result if group['backend_version'] == '0.11.0')
        self.assertEqual(old['requests'], 5)
        self.assertEqual(old['completed_timings_ms']['total_ms']['median'], 20)
        self.assertEqual(old['completed_timings_ms']['total_ms']['p95'], 30)
        self.assertIsNone(old['reported_cache']['token_reuse_fraction'])
        self.assertEqual(new['reported_cache']['token_reuse_fraction'], .8)

    def test_public_stats_command_reads_rotated_logs_without_env_or_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(GATEWAY.parent/'run.sh', root/'run.sh')
            (root/'gateway').mkdir()
            for filename in ('qwen_latency_stats.py', 'qwen_metrics.py'):
                shutil.copy(GATEWAY/filename, root/'gateway'/filename)
            logs = root/'logs/metrics'
            logs.mkdir(parents=True)
            row = {'schema_version': 1, 'service': 'gateway', 'route': 'llm',
                   'outcome': 'completed', 'total_ms': 12}
            for suffix in ('', '.1'):
                (logs/('metrics.jsonl'+suffix)).write_text(json.dumps(row)+'\n')
            result = subprocess.run(['bash', str(root/'run.sh'), 'stats'],
                                    capture_output=True, text=True, check=True)
            self.assertIn('2 requests', result.stdout)
            self.assertIn('median=', result.stdout)
            self.assertFalse((root/'.env').exists())

    def test_cache_policies_are_not_combined_in_reports(self):
        rows = [{'service': 'gateway', 'route': 'llm', 'backend_version': '0.11.0',
                 'outcome': 'completed', 'cache_policy': policy, 'total_ms': elapsed}
                for policy, elapsed in (('reuse', 200), ('reset', 7000))]
        groups = report.summarize(rows)
        self.assertEqual(len(groups), 2)
        for group in groups:
            expected = 200 if group['cache_policy'] == 'reuse' else 7000
            self.assertEqual(group['completed_timings_ms']['total_ms']['median'], expected)

    def test_pipeline_trace_does_not_confuse_tts_url_with_playback(self):
        def e(kind, seconds, **data):
            return {'type': kind, 'timestamp': '2026-11-17T00:00:%02d+00:00' % seconds, 'data': data}
        trace = {'events': [e('stt-vad-end', 1), e('stt-end', 2), e('intent-start', 2),
                            e('intent-progress', 3, chat_log_delta={'content': 'PRIVATE answer'}),
                            e('intent-end', 5), e('tts-start', 5), e('tts-end', 5), e('run-end', 5)]}
        result = report.pipeline_summary(trace)
        self.assertEqual(result['vad_end_to_stt_ms'], 1000)
        self.assertEqual(result['intent_ms'], 3000)
        self.assertEqual(result['intent_first_text_ms'], 1000)
        self.assertEqual(result['tts_url_ready_ms'], 0)
        self.assertIsNone(result['vad_end_to_first_pcm_ms'])
        self.assertIsNone(result['vad_end_to_playback_ms'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        observed = report.pipeline_summary(trace, first_pcm_at='2026-11-17T00:00:06Z', playback_at='2026-11-17T00:00:07Z')
        self.assertEqual(observed['vad_end_to_first_pcm_ms'], 5000)
        self.assertEqual(observed['vad_end_to_playback_ms'], 6000)
        with self.assertRaises(ValueError): report.pipeline_summary(trace, playback_at='2026-11-17T00:00:00Z')

    def test_audio_log_exports_and_unfinished_requests(self):
        lines = ['not a metric\n', '{unfinished\n']
        for event_name, elapsed in [('render_start', 5), ('first_pcm_send_complete', 15), ('request_complete', 30)]:
            lines.append('INFO:worker:pocket_tts_metric ' + json.dumps({
                'schema_version': 1, 'service': 'tts', 'request_id': 'test',
                'event': event_name, 'elapsed_ms': elapsed}))
        lines.append('INFO:worker:pocket_tts_metric ' + json.dumps({
            'schema_version': 1, 'service': 'tts', 'request_id': 'other',
            'event': 'render_start', 'elapsed_ms': 2}))
        result = report.summarize(report.read_records(lines))[0]
        self.assertEqual(result['outcomes'], {'completed': 1, 'unfinished': 1})
        self.assertEqual(result['completed_timings_ms']['first_pcm_ms']['median'], 15)
        self.assertEqual(result['completed_timings_ms']['total_ms']['n'], 1)

    def test_tts_stream_boundaries_and_failure_survive_cleanup_events(self):
        events = [('first_text_chunk', 2), ('first_sentence_ready', 10),
                  ('first_pcm_send_complete', 25), ('text_stream_stop', 40),
                  ('request_complete', 55)]
        rows = [{'service': 'tts', 'request_id': 'complete', 'event': event,
                 'elapsed_ms': elapsed} for event, elapsed in events]
        rows.extend({'service': 'tts', 'request_id': 'failed', 'event': event,
                     'elapsed_ms': elapsed}
                    for event, elapsed in [('request_failed', 30), ('request_cancelled', 31)])
        group = report.summarize(rows)[0]
        self.assertEqual(group['outcomes'], {'completed': 1, 'failed': 1})
        timing = group['completed_timings_ms']
        self.assertEqual(timing['first_text_ms']['median'], 2)
        self.assertEqual(timing['text_stream_ms']['median'], 40)
        self.assertEqual(timing['sentence_to_first_pcm_ms']['median'], 15)


if __name__ == '__main__':
    unittest.main()
