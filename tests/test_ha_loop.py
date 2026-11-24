import json
import unittest

from tools.simulate_ha_loop import (
    FakeUpstream, Gateway, parse_sse, post, request_body, system_message, user_message,
)


class HALoopTests(unittest.TestCase):
    def test_tool_choice_controls_local_routing(self):
        upstream = FakeUpstream('fallback-text')
        self.addCleanup(upstream.close)
        gateway = Gateway(upstream.url)
        self.addCleanup(gateway.close)
        body = request_body([
            system_message([('Światło', 'light', 'Gabinet')]),
            user_message('Włącz światło w gabinecie.'),
        ])

        local = parse_sse(post(gateway.port, body))
        self.assertEqual(local.finish_reason, 'tool_calls')
        self.assertEqual(len(local.tool_calls), 1)
        call = local.tool_calls[0]
        self.assertEqual(call['name'], 'intent__HassTurnOn')
        self.assertEqual(json.loads(call['arguments']),
                         {'name': 'Światło', 'domain': ['light'], 'area': 'Gabinet'})
        self.assertEqual(upstream.calls, 0)

        fallback = parse_sse(post(gateway.port, {**body, 'tool_choice': 'none'}))
        self.assertEqual(fallback.finish_reason, 'stop')
        self.assertEqual(fallback.tool_calls, [])
        self.assertEqual(fallback.content, 'To jest odpowiedź symulowanego modelu.')
        self.assertEqual(upstream.calls, 1)
