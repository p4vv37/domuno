"""Audio protocol and Whisper prompt planning with fake adapters. No network or hardware."""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Describe

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'stt'))
import qnn_wyoming_server  # noqa: E402
import qnn_whisper_engine  # noqa: E402
import qnn_whisper_profile  # noqa: E402
from qnn_whisper_engine import forced_decoder_token  # noqa: E402


class RecordingHandler(qnn_wyoming_server.Handler):
    """Capture the outbound Wyoming events without opening sockets."""
    def __init__(self, state):
        super().__init__(None, None, state)
        self.events = []

    async def write_event(self, event):
        self.events.append(event)


class STTTests(unittest.IsolatedAsyncioTestCase):
    def make_handler(self, transcribe=lambda _raw: {'text': 'synthetic fixture'}, *, language='pl'):
        engine = SimpleNamespace(transcribe=transcribe, close=lambda: None)
        state = qnn_wyoming_server.State(engine, pcm_decoder=lambda raw: raw, language=language)
        self.addCleanup(state.close)
        return RecordingHandler(state)

    async def send_audio(self, handler):
        await handler.handle_event(AudioStart(16000, 2, 1).event())
        await handler.handle_event(AudioChunk(16000, 2, 1, b'\x01\x00' * 1600).event())
        return await handler.handle_event(AudioStop().event())

    def test_stt_metrics_filter_payload_and_missing_engine_timings(self):
        result = {'text': 'PRIVATE transcript', 'tokens': [1, 2],
                  'timings_seconds': {'encoder': .02, 'total': .03,
                                      'preprocess': float('nan'), 'decoder_steps': ['PRIVATE']}}
        with self.assertLogs(qnn_wyoming_server.LOG, level='INFO') as logs:
            qnn_wyoming_server.emit_stt_metric(time.perf_counter(), 'completed', 2.5, result, 1)
        row = json.loads(logs.output[0].split('stt_metric ', 1)[1])
        self.assertEqual(row['encoder_ms'], 20)
        self.assertEqual(row['engine_ms'], 30)
        self.assertIsNone(row['preprocess_ms'])
        self.assertIsNone(row['decode_ms'])
        self.assertNotIn('PRIVATE', json.dumps(row))

    async def test_pcm_protocol_returns_transcript_and_rejects_bad_format(self):
        handler = self.make_handler()
        self.assertTrue(await self.send_audio(handler))
        self.assertEqual(Transcript.from_event(handler.events[-1]).text, 'synthetic fixture')
        self.assertFalse(await handler.handle_event(AudioStart(8000, 2, 1).event()))
        self.assertEqual(handler.events[-1].data['code'], 'unsupported-audio')

    async def test_describe_advertises_configured_language_and_requires_external_vad(self):
        for language in ('pl', 'en', 'es'):
            with self.subTest(language=language):
                handler = self.make_handler(language=language)
                self.assertTrue(await handler.handle_event(Describe().event()))
                info = handler.events[-1]
                self.assertEqual(info.type, 'info')
                program, = info.data['asr']
                self.assertEqual(program['name'], 'whisper-small-qnn-arduino')
                self.assertIs(program.get('requires_external_vad'), True)
                self.assertEqual(program['models'][0]['languages'], [language])

    async def test_transcribe_accepts_omitted_and_matching_locale_languages(self):
        handler = self.make_handler(language='es')
        for language in (None, 'es', 'ES', 'es-419', 'es_MX', 'es-Latn-ES'):
            with self.subTest(language=language):
                handler.events.clear()
                self.assertTrue(await handler.handle_event(Transcribe(language=language).event()))
                self.assertEqual(handler.events, [])
                self.assertTrue(await self.send_audio(handler))
                self.assertEqual(Transcript.from_event(handler.events[-1]).text, 'synthetic fixture')

    async def test_transcribe_rejects_explicit_unsupported_or_malformed_language(self):
        handler = self.make_handler(language='es')
        for language in ('en-US', 'de', None, '', 0, [], {}, 'es-', 'es--ES', 'es-!', 'es-foo'):
            with self.subTest(language=language):
                event = Event(type='transcribe', data={'language': language})
                self.assertFalse(await handler.handle_event(event))
                error = handler.events[-1]
                self.assertEqual(error.type, 'error')
                self.assertEqual(error.data['code'], 'unsupported-language')

    async def test_transcription_failure_logs_without_exception_payload(self):
        def fail(_raw):
            raise RuntimeError('PRIVATE audio fixture')

        handler = self.make_handler(fail)
        with self.assertLogs(qnn_wyoming_server.LOG, level='ERROR') as logs:
            self.assertFalse(await self.send_audio(handler))
        error = handler.events[-1]
        self.assertEqual(error.data['code'], 'transcription-failed')
        self.assertNotIn('PRIVATE', error.data['text'])
        self.assertEqual(logs.output, ['ERROR:qnn_wyoming_server:STT_PHASE transcription-failed'])

    async def test_cancel_waits_for_native_worker_before_releasing_capacity(self):
        future=asyncio.get_running_loop().create_future()
        task=asyncio.create_task(qnn_wyoming_server._finish_worker(future))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        future.set_result('native worker completed')
        with self.assertRaises(asyncio.CancelledError): await task


class SttLanguageTests(unittest.TestCase):
    def test_engine_rejects_unknown_language_before_opening_npu(self):
        with self.assertRaisesRegex(ValueError, 'language must be one of'):
            qnn_whisper_engine.QnnWhisperEngine(
                '/nonexistent', '/nonexistent', '/nonexistent',
                metadata='/nonexistent', language='de')

    def test_profile_requires_the_token_id_for_the_configured_language(self):
        class Tokenizer:
            def get_vocab_size(self):
                return 51866
            def token_to_id(self, token):
                return {'<|yue|>': 50358}.get(token)
        profile = qnn_whisper_profile.Profile(None)
        for language, lang_id in (('pl', 50269), ('en', 50259), ('es', 50262)):
            with self.subTest(language=language):
                profile.validate_tokenizer(Tokenizer(), [50258, lang_id, 50360, 50364, 50257], language)
        for lang_id in (0, 50358, 50262):  # invalid, unsupported (yue), and wrong for pl (es)
            with self.subTest(lang_id=lang_id), self.assertRaisesRegex(ValueError, 'language token'):
                profile.validate_tokenizer(Tokenizer(), [50258, lang_id, 50360, 50364, 50257], 'pl')


class PromptPlanTests(unittest.TestCase):
    def test_control_prefix_then_prompt_then_free_generation(self):
        prefix = (10, 11, 12, 13)
        prompt = (20, 21)
        self.assertEqual(
            [forced_decoder_token(prefix, prompt, step) for step in range(7)],
            [11, 12, 13, 20, 21, None, None])

    def test_empty_prompt_starts_free_generation_after_control_tokens(self):
        prefix = (10, 11, 12, 13)
        self.assertEqual(
            [forced_decoder_token(prefix, (), step) for step in range(5)],
            [11, 12, 13, None, None])


if __name__=='__main__': unittest.main()
