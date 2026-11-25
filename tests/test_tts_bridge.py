"""The independent Wyoming bridge; no model, Rust binary or Home Assistant."""
import asyncio
import importlib.util
from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from wyoming.audio import AudioChunk, AudioStart
from wyoming.event import Event, async_read_event, async_write_event
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeStart

TTS_DIR = Path(__file__).resolve().parents[1] / 'tts-pocket-wyoming'
sys.path.insert(0, str(TTS_DIR))
SPEC = importlib.util.spec_from_file_location('tts_bridge', TTS_DIR / 'wyoming_tts_server.py')
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


def wav_bytes(pcm=b'\x01\x00' * 100, *, rate=22050, channels=1, width=2,
              data_size=2_000_000_000):
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * channels * width,
                      channels * width, width * 8)
    return (b'RIFF' + struct.pack('<I', data_size + 36) + b'WAVE'
            + b'fmt ' + struct.pack('<I', len(fmt)) + fmt
            + b'data' + struct.pack('<I', data_size) + pcm)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_exit_after_readiness_stops_bridge(self):
        server_started = asyncio.Event()

        async def serve(_handler):
            server_started.set()
            await asyncio.Future()

        async def child_wait():
            await server_started.wait()
            child.returncode = 7
            return 7

        child = SimpleNamespace(returncode=None, wait=child_wait)
        server = SimpleNamespace(run=serve, stop=AsyncMock())
        backend = SimpleNamespace(close=AsyncMock())
        args = SimpleNamespace(backend_url=None, backend_binary='fake', config='fake',
                               backend_voice=None, voice='shefowl', backend_port=18081,
                               startup_timeout=1, version='test', host='127.0.0.1', port=0,
                               language='pl')
        with patch.object(bridge, 'PocketBackend', return_value=backend) as create, \
                patch.object(bridge, 'start_rust_backend', return_value=child), \
                patch.object(bridge, 'wait_ready'), \
                patch.object(bridge.AsyncServer, 'from_uri', return_value=server):
            with self.assertRaisesRegex(bridge.BackendError, 'status 7'):
                await asyncio.wait_for(bridge.run(args), timeout=2)
        create.assert_called_once_with('http://127.0.0.1:18081')
        server.stop.assert_awaited_once()
        backend.close.assert_awaited_once()

    async def test_backend_that_ignores_terminate_is_killed_and_reaped(self):
        exited = asyncio.Event()
        child = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(side_effect=exited.set),
                                wait=AsyncMock(side_effect=exited.wait))
        await asyncio.wait_for(bridge.stop_rust_backend(child, timeout=0.01), timeout=2)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.await_count, 2)


class WavHeaderTests(unittest.TestCase):
    def test_placeholder_data_size_is_ignored(self):
        body = wav_bytes()
        fmt, offset = bridge.parse_wav_header(body)
        self.assertEqual((fmt.rate, fmt.width, fmt.channels), (22050, 2, 1))
        self.assertEqual(body[offset:], b'\x01\x00' * 100)

    def test_incomplete_or_invalid_header_is_not_accepted(self):
        self.assertIsNone(bridge.parse_wav_header(wav_bytes()[:20]))
        with self.assertRaises(bridge.BackendError):
            bridge.parse_wav_header(b'not a wave file at all')


class FakeBackend:
    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error
        self.text = None

    async def synthesize(self, text):
        self.text = text
        for event in self.events:
            yield event
        if self.error:
            raise self.error


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, backend, language='pl'):
        instance = bridge.Handler(None, None, backend, bridge.make_info('test', 'test', language), language)
        events = []

        async def write(event):
            events.append(event)

        instance.write_event = write
        return instance, events

    async def test_describe_and_synthesize_events(self):
        backend = FakeBackend([
            AudioStart(rate=16000, width=2, channels=1),
            AudioChunk(rate=16000, width=2, channels=1, audio=b'\x00\x00' * 10),
        ])
        instance, events = self.handler(backend)
        self.assertTrue(await instance.handle_event(Describe().event()))
        self.assertIsInstance(Info.from_event(events[0]), Info)
        self.assertTrue(await instance.handle_event(Synthesize(text='Dzień dobry').event()))
        self.assertEqual(backend.text, 'Dzień dobry')
        self.assertEqual([event.type for event in events[1:]],
                         ['audio-start', 'audio-chunk', 'audio-stop'])

    async def test_english_and_spanish_preserve_numbers(self):
        for language, text in (('en', 'The temperature is 20.'), ('es', 'La temperatura es 20.')):
            with self.subTest(language=language):
                backend = FakeBackend()
                instance, _ = self.handler(backend, language)
                await instance.handle_event(Synthesize(text=text).event())
                self.assertEqual(backend.text, text)

    async def test_invalid_or_oversized_text_never_reaches_backend(self):
        for text, code in ((123, 'invalid-request'), ('a' * 4001, 'text-too-long')):
            with self.subTest(code=code):
                backend = FakeBackend()
                instance, events = self.handler(backend)
                await instance.handle_event(Event(type='synthesize', data={'text': text}))
                self.assertEqual(events[0].data['code'], code)
                self.assertIsNone(backend.text)

    async def test_busy_is_reported_without_audio_events(self):
        instance, events = self.handler(FakeBackend(error=bridge.BackendBusy()))
        await instance.handle_event(Synthesize(text='test').event())
        self.assertEqual([(event.type, event.data['code']) for event in events], [('error', 'busy')])

    async def test_backend_failure_does_not_claim_success(self):
        backend = FakeBackend(
            [AudioStart(rate=16000, width=2, channels=1)],
            bridge.BackendError('synthetic failure'))
        instance, events = self.handler(backend)
        self.assertTrue(await instance.handle_event(Synthesize(text='test').event()))
        self.assertEqual([event.type for event in events], ['audio-start', 'error'])
        self.assertNotIn('audio-stop', [event.type for event in events])

    async def test_streaming_text_is_rejected(self):
        instance, events = self.handler(FakeBackend())
        self.assertTrue(await instance.handle_event(SynthesizeStart().event()))
        self.assertEqual(events[0].type, 'error')


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.release_stall = asyncio.Event()
        self.body = wav_bytes()
        self.status = 200

        async def tts(request):
            form = await request.post()
            self.requests.append(form)
            if form['text'] == 'Porzucone':
                response = web.StreamResponse(headers={'Content-Type': 'audio/wav'})
                await response.prepare(request)
                await response.write(wav_bytes(b'\x01\x00' * 441))
                await self.release_stall.wait()
                return response
            return web.Response(body=self.body, status=self.status, content_type='audio/wav')

        app = web.Application()
        app.router.add_post('/tts', tts)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.backend = bridge.PocketBackend(f'http://127.0.0.1:{port}')

    async def asyncTearDown(self):
        await self.backend.close()
        await self.runner.cleanup()

    async def test_disconnect_releases_synthesis_for_next_client(self):
        info = bridge.make_info('shefowl', 'test')

        async def accept(reader, writer):
            try:
                await bridge.Handler(reader, writer, self.backend, info).run()
            finally:
                writer.close()
                await writer.wait_closed()

        async def request(text):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            self.addAsyncCleanup(writer.wait_closed)
            self.addCleanup(writer.close)
            await async_write_event(Synthesize(text=text).event(), writer)
            return reader, writer

        async def receive(reader):
            event = await asyncio.wait_for(async_read_event(reader), timeout=2)
            self.assertIsNotNone(event)
            return event.type

        server = await asyncio.start_server(accept, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            try:
                first, writer = await request('Porzucone')
                try:
                    self.assertEqual(await receive(first), 'audio-start')
                    self.assertEqual(await receive(first), 'audio-chunk')
                finally:
                    writer.close()
                    await writer.wait_closed()

                second, writer = await request('Następne')
                try:
                    self.assertEqual(await receive(second), 'audio-start')
                    self.assertEqual(await receive(second), 'audio-chunk')
                    self.assertEqual(await receive(second), 'audio-stop')
                    self.assertFalse(self.release_stall.is_set())
                finally:
                    writer.close()
                    await writer.wait_closed()
            finally:
                self.release_stall.set()

    async def test_form_text_and_pcm_are_forwarded(self):
        events = [event async for event in self.backend.synthesize('Dzień dobry')]
        self.assertEqual(self.requests[0]['text'], 'Dzień dobry')
        self.assertIsInstance(events[0], AudioStart)
        pcm = b''.join(event.audio for event in events if isinstance(event, AudioChunk))
        self.assertEqual(pcm, b'\x01\x00' * 100)
        self.assertTrue(all(isinstance(event, (AudioStart, AudioChunk)) for event in events))

    async def test_http_error_is_a_failure(self):
        self.status = 500
        with self.assertRaisesRegex(bridge.BackendError, 'HTTP 500'):
            [event async for event in self.backend.synthesize('test')]

    async def test_empty_audio_is_a_failure(self):
        self.body = wav_bytes(b'')
        with self.assertRaisesRegex(bridge.BackendError, 'no audio'):
            [event async for event in self.backend.synthesize('test')]

    async def test_odd_sample_rate_keeps_chunks_frame_aligned(self):
        self.body = wav_bytes(b'\x01\x00' * 900, rate=22075)
        events = [event async for event in self.backend.synthesize('test')]
        self.assertEqual([len(event.audio) for event in events[1:]], [882, 882, 36])

    async def test_outsize_audio_is_a_failure(self):
        with patch.object(bridge, '_MAX_PCM_BYTES', 100):
            with self.assertRaisesRegex(bridge.BackendError, 'size limit'):
                [event async for event in self.backend.synthesize('test')]

    async def test_concurrent_render_fails_fast_without_posting(self):
        self.addCleanup(self.release_stall.set)
        stream = self.backend.synthesize('Porzucone')
        try:
            await asyncio.wait_for(anext(stream), timeout=2)
            with self.assertRaises(bridge.BackendBusy):
                await asyncio.wait_for(anext(self.backend.synthesize('Następne')), timeout=2)
            self.assertEqual([form['text'] for form in self.requests], ['Porzucone'])
        finally:
            await stream.aclose()
            self.release_stall.set()


class PolishBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_polish_uses_config_and_backend_voice(self):
        captured = {}

        async def fake_exec(*command, **kwargs):
            captured['command'] = list(command)
            return SimpleNamespace(returncode=None, wait=AsyncMock())

        with patch.object(bridge.asyncio, 'create_subprocess_exec', fake_exec):
            await bridge.start_rust_backend(
                '/bin/tts', '/assets/config.yaml', '/assets/reference.wav', 18080)
        self.assertIn('--config', captured['command'])
        self.assertNotIn('--language', captured['command'])
        self.assertEqual(captured['command'][-3:], ['--default-voice', '/assets/reference.wav', '--quantize'])

    def test_language_selects_its_own_model_and_voice(self):
        for language, voice, locale, folder, extension in (
            ('pl', 'shefowl', 'pl-PL', '/assets', 'wav'),
            ('en', 'alba', 'en-US', '/assets/en', 'safetensors'),
            ('es', 'lola', 'es-ES', '/assets/es', 'safetensors'),
        ):
            with self.subTest(language=language), patch.dict('os.environ', {'AGENT_LANGUAGE': language}, clear=True):
                args = bridge.parse_args([])
                self.assertEqual(args.config, f'{folder}/config.yaml')
                self.assertEqual(args.backend_voice, f'{folder}/reference.{extension}')
                program = bridge.make_info(args.voice, 'test', args.language).tts[0]
                self.assertEqual(program.name, f'pocket-tts-{language}')
                self.assertEqual(program.voices[0].name, voice)
                self.assertEqual(program.voices[0].languages, [locale])
                self.assertFalse(program.supports_synthesize_streaming)


if __name__ == '__main__':
    unittest.main()
