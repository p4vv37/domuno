"""Bounded, payload-free observations of OpenAI JSON and SSE responses."""
import json
import math
import time
from datetime import datetime, timezone
import uuid


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


class RequestMetrics:
    def __init__(self, backend_version='unknown', profile='default', clock=time.perf_counter):
        self.clock = clock
        self.started = clock()
        self.record = {
            'schema_version': 1, 'service': 'gateway',
            'request_id': uuid.uuid4().hex,
            'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'backend_version': backend_version, 'profile': profile,
            'route': 'rejected', 'outcome': 'rejected', 'status': None,
            'stream': None, 'total_ms': None, 'router_ms': None,
            'budget_ms': None, 'queue_ms': None, 'reset_ms': None,
            'audit_ms': None, 'capture_ms': None,
            'wait_sound_ms': None, 'wait_sound_started': None,
            'backend_ms': None, 'ttft_ms': None, 'prefill_ms': None,
            'decode_ms': None, 'estimated_prompt_tokens': None,
            'prompt_tokens': None, 'cached_tokens': None,
            'evaluated_prompt_tokens': None, 'completion_tokens': None,
            'response_kind': None, 'finish_reason': None,
        }

    def elapsed(self, started):
        return round((self.clock() - started) * 1000, 3)

    def finish(self, sink):
        self.record['total_ms'] = self.elapsed(self.started)
        try:
            sink.write(self.record)
        except Exception:
            pass  # Telemetry must never change the response or trigger a retry.


class ResponseObserver:
    """Observe a copy of bytes. Never rewrite or retain an entire SSE stream.

    TTFT is measured only for streaming content/tool deltas, from backend start.
    Oversized or malformed observations are marked unknown, never as empty.
    """
    LIMIT = 128 * 1024

    def __init__(self, stream, clock=time.perf_counter):
        self.stream, self.clock = stream, clock
        self.started = clock()
        self.buffer = bytearray()
        self.event = bytearray()
        self.unknown = False
        self.parsed = False
        self.done = False
        self.text = self.tool = False
        self.values = {}

    def feed(self, chunk):
        if self.unknown:
            return
        if not self.stream:
            if len(self.buffer) + len(chunk) > self.LIMIT:
                self._invalidate()
            else:
                self.buffer.extend(chunk)
            return
        # Scan lines without accumulating an unbounded chunk or event.
        for part in chunk.splitlines(keepends=True):
            self.buffer.extend(part)
            if len(self.buffer) > self.LIMIT:
                self._invalidate()
                return
            if not self.buffer.endswith(b'\n'):
                continue
            line = bytes(self.buffer).rstrip(b'\r\n')
            self.buffer.clear()
            if not line:
                if self.event:
                    self._parse(bytes(self.event).rstrip(b'\n'))
                    self.event.clear()
            elif line.startswith(b'data:'):
                self.event.extend(line[5:].lstrip(b' ') + b'\n')
                if len(self.event) > self.LIMIT:
                    self._invalidate()
                    return

    def _invalidate(self):
        self.unknown = True
        self.buffer.clear()
        self.event.clear()

    def _parse(self, data):
        if data == b'[DONE]':
            self.done = True
            return
        try:
            obj = json.loads(data)
            if not isinstance(obj, dict):
                raise ValueError('Invalid observation')
            self.parsed = True
            usage = obj.get('usage') or {}
            timings = obj.get('timings') or {}
            if not isinstance(usage, dict) or not isinstance(timings, dict):
                raise ValueError('Invalid observation')
            for key in ('prompt_tokens', 'completion_tokens'):
                if type(usage.get(key)) is int and usage[key] >= 0:
                    self.values[key] = usage[key]
            details = usage.get('prompt_tokens_details') or {}
            if isinstance(details, dict) and type(details.get('cached_tokens')) is int and details['cached_tokens'] >= 0:
                self.values['cached_tokens'] = details['cached_tokens']
            for source, dest in (('prompt_ms', 'prefill_ms'), ('predicted_ms', 'decode_ms'),
                                 ('prompt_n', 'evaluated_prompt_tokens')):
                if number(timings.get(source)) is not None:
                    self.values[dest] = timings[source]
            for choice in obj.get('choices') or []:
                finish = choice.get('finish_reason')
                if finish in ('stop', 'length', 'tool_calls', 'function_call', 'content_filter'):
                    self.values['finish_reason'] = finish
                message = choice.get('delta' if self.stream else 'message') or {}
                text = message.get('content')
                has_text = isinstance(text, str) and bool(text.strip())
                has_tool = any(isinstance(call, dict) and
                               (call.get('function') or {}).get('arguments')
                               for call in message.get('tool_calls') or [])
                if self.stream and (has_text or has_tool) and 'ttft_ms' not in self.values:
                    self.values['ttft_ms'] = round((self.clock() - self.started) * 1000, 3)
                self.text = self.text or has_text
                self.tool = self.tool or bool(has_tool)
        except (ValueError, TypeError, AttributeError):
            self._invalidate()

    def finish(self):
        if not self.stream and not self.unknown:
            self._parse(bytes(self.buffer))
        if self.stream and (self.buffer or self.event):
            self.unknown = True
        self.buffer.clear()
        self.event.clear()
        kind = ('unknown' if self.unknown or not self.parsed else
                'mixed' if self.text and self.tool else 'toolcall' if self.tool else
                'text' if self.text else 'empty')
        terminal = self.done or self.values.get('finish_reason') is not None
        return {**self.values, 'response_kind': kind,
                'observation_complete': not self.unknown and self.parsed and terminal}
