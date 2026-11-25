import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from chat_gateway import make_handler
from local_tools import LocalTools, SEARCH, WEATHER


class LocalToolTests(unittest.TestCase):
    def exchange(self, tools, name, args, stream=True, extra=(), final=None):
        from http.server import BaseHTTPRequestHandler
        requests, counted = [], []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if len(requests) == 1:
                    calls = [{'id': 'call_local', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}, *extra]
                    choice = {'message': {'role': 'assistant', 'tool_calls': calls}, 'finish_reason': 'tool_calls'}
                else:
                    choice = final or {'message': {'role': 'assistant', 'content': 'Odpowiedź.'}, 'finish_reason': 'stop'}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'choices': [choice]}).encode())

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        handler = make_handler(f'http://127.0.0.1:{upstream.server_port}', local_tools=tools,
                               prompt_counter=lambda body: counted.append(body) or 100)
        gateway = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        for server in (upstream, gateway):
            Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()
        try:
            with self.connection(gateway.server_port) as conn:
                conn.request('POST', '/v1/chat/completions', json.dumps({'stream': stream, 'messages': [
                    {'role': 'system', 'content': 'PRIVATE INVENTORY'},
                    {'role': 'user', 'content': 'Pytanie'},
                ]}))
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                return response.read().decode(), requests, counted
        finally:
            for server in (gateway, upstream):
                server.shutdown()
                server.server_close()

    @staticmethod
    def connection(port):
        from contextlib import closing
        return closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5))

    def test_search_stays_inside_gateway_and_counts_both_requests(self):
        get = Mock(return_value={'web': {'results': [
            {'url': 'https://www.example.org/test', 'title': 'Wynik meczu', 'description': '<b>Wynik</b> to 2:1.'},
            {'url': 'https://irrelevant.example/test', 'title': 'Obiad', 'description': 'Przepis kulinarny.'},
        ]}})
        cue = Mock()
        response, requests, counted = self.exchange(LocalTools(key='secret', get=get, cue=cue), SEARCH, {'query': 'wynik meczu'})
        cue.play.assert_called_once()
        self.assertEqual(get.call_args.args[1]['q'], 'wynik meczu')
        self.assertEqual(requests, counted)
        self.assertIn('example.org', str(requests[1]))
        self.assertNotIn('irrelevant', str(requests[1]))
        self.assertNotIn('PRIVATE INVENTORY', str(requests[1]))
        self.assertNotIn('tools', requests[1])
        self.assertNotIn('tool_calls', response)
        self.assertIn('Odpowiedź.', response)
        self.assertTrue(response.endswith('data: [DONE]\n\n'))

    def test_weather_uses_configured_location_and_returns_json(self):
        get = Mock(return_value={'current': {'time': '2026-11-25T10:00', 'temperature_2m': 4, 'weather_code': 3},
            'daily': {'time': ['2026-11-25'], 'temperature_2m_min': [1], 'temperature_2m_max': [7],
                      'weather_code': [61], 'precipitation_probability_max': [80]}})
        response, requests, _ = self.exchange(LocalTools(location=(50, 19, 'Kraków'), get=get), WEATHER, {}, stream=False)
        self.assertEqual(get.call_args.args[1]['latitude'], 50)
        self.assertIn('80%', str(requests[1]))
        self.assertEqual(json.loads(response)['choices'][0]['message']['content'], 'Odpowiedź.')

    def test_provider_failure_and_mixed_calls_do_not_request_second_round(self):
        for extra in ((), ({'function': {'name': 'HassTurnOn'}},)):
            get = Mock(side_effect=TimeoutError())
            response, requests, _ = self.exchange(LocalTools(key='secret', get=get), SEARCH, {'query': 'wynik'}, extra=extra)
            self.assertEqual(len(requests), 1)
            self.assertEqual(get.call_count, int(not extra))
            self.assertIn('Nie udało', response)
            self.assertNotIn('HassTurnOn', response)
