"""Real audio handlers with fake protocol/model adapters. No network or hardware."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor, Future
import importlib.util
import logging
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
try:
    import numpy as np
except ImportError:
    np=None


class Wire:
    def __init__(self, type, **values):
        self.type=type
        self.__dict__.update(values)


class Protocol:
    @classmethod
    def is_type(cls, kind): return cls.kind==kind
    @classmethod
    def from_event(cls,event): return event
    def __init__(self,**values): self.__dict__.update(values)
    def event(self): return Wire(self.kind,**self.__dict__)


def namespace(path, names):
    # Execute the actual handler definitions. Only external Wyoming/model types
    # are fakes, so the suite also runs without the device's runtime packages.
    names = names | ({'emit_stt_metric'} if 'Handler' in names else set())
    tree=ast.parse(path.read_text())
    body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]
    body += [node for node in tree.body if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    ns=dict(json=json,math=math,uuid=uuid,datetime=datetime,timezone=timezone,asyncio=asyncio,perf_counter=time.perf_counter,time=time,
            ThreadPoolExecutor=ThreadPoolExecutor,AsyncEventHandler=object,
            LOG=logging.getLogger('test-stt'),_LOGGER=logging.getLogger('test-tts'),
            MAX_BYTES=960000,INFO=Wire('Info'),pcm_decode=lambda raw:raw,
            numpy=np,_VOICE_STATES={},PREDEFINED_VOICE_NAMES={'shefowl':'unused'},
            PREFIX_MIN_DURATION=.15,PREFIX_MAX_DURATION=.05,PREFIX_SILENCE_GAP=.08,
            Event=Wire)
    for name in ('Describe Transcribe Transcript AudioStart AudioChunk AudioStop Synthesize '
                 'SynthesizeStart SynthesizeChunk SynthesizeStop SynthesizeStopped Error').split():
        ns[name]=type(name,(Protocol,),{'kind':name})
    if 'PocketTTSEventHandler' in names:
        spec=importlib.util.spec_from_file_location('audio_worker',ROOT/'tts-pocket-wyoming/tts_render_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        ns.update({name:getattr(worker,name) for name in ['RenderWorker','RenderBusy','RequestMetrics','PCMStream','StreamCancelled','TextStream']})
        backend_spec=importlib.util.spec_from_file_location('audio_backend',ROOT/'tts-pocket-wyoming/rust_tts_backend.py')
        backend=importlib.util.module_from_spec(backend_spec);backend_spec.loader.exec_module(backend)
        ns['split_ready_text']=backend.split_ready_text
    exec(compile(ast.fix_missing_locations(ast.Module(body=body,type_ignores=[])),str(path),'exec'),ns)
    return ns


class STTTests(unittest.IsolatedAsyncioTestCase):
    def test_stt_metrics_filter_payload_and_missing_engine_timings(self):
        ns = namespace(ROOT/'stt/qnn_wyoming_server.py', {'emit_stt_metric'})
        result = {'text': 'PRIVATE transcript', 'tokens': [1, 2],
                  'timings_seconds': {'encoder': .02, 'total': .03,
                                      'preprocess': float('nan'), 'decoder_steps': ['PRIVATE']}}
        with self.assertLogs(ns['LOG'], level='INFO') as logs:
            ns['emit_stt_metric'](time.perf_counter(), 'completed', 2.5, result, 1)
        row = json.loads(logs.output[0].split('stt_metric ', 1)[1])
        self.assertEqual(row['encoder_ms'], 20)
        self.assertEqual(row['engine_ms'], 30)
        self.assertIsNone(row['preprocess_ms'])
        self.assertIsNone(row['decode_ms'])
        self.assertNotIn('PRIVATE', json.dumps(row))

    async def test_pcm_protocol_returns_transcript_and_rejects_bad_format(self):
        ns=namespace(ROOT/'stt/qnn_wyoming_server.py',{'_finish_worker','State','Handler'})
        engine=SimpleNamespace(transcribe=lambda raw:{'text':'synthetic fixture'},close=lambda:None)
        state=ns['State'](engine)
        handler=object.__new__(ns['Handler'])
        handler.state=state;handler.audio=bytearray();handler.started=False
        events=[]
        async def write(event): events.append(event)
        handler.write_event=write
        try:
            await handler.handle_event(Wire('AudioStart',rate=16000,width=2,channels=1))
            await handler.handle_event(Wire('AudioChunk',rate=16000,width=2,channels=1,audio=b'\x01\x00'*1600))
            await handler.handle_event(Wire('AudioStop'))
            self.assertEqual(events[-1].text,'synthetic fixture')
            success=await handler.handle_event(Wire('AudioStart',rate=8000,width=2,channels=1))
            self.assertFalse(success)
            self.assertEqual(events[-1].data['code'],'unsupported-audio')
        finally: state.close()

    async def test_cancel_waits_for_native_worker_before_releasing_capacity(self):
        ns=namespace(ROOT/'stt/qnn_wyoming_server.py',{'_finish_worker'})
        future=asyncio.get_running_loop().create_future()
        task=asyncio.create_task(ns['_finish_worker'](future))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        future.set_result('native worker completed')
        with self.assertRaises(asyncio.CancelledError): await task


@unittest.skipIf(np is None,'NumPy unavailable')
class TTSTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, generate, streamed=False, buffer_seconds=.02):
        ns=namespace(ROOT/'tts-pocket-wyoming/wyoming_tts_server.py',{'PocketTTSEventHandler'})
        class Tensor:
            def __init__(self,values): self.values=values
            def detach(self): return self
            def cpu(self): return self
            def numpy(self): return self.values
        class Model:
            sample_rate=1000
            reference='/local/reference.wav'
            def get_state_for_audio_prompt(self,prompt): return {}
            def generate_audio_stream(self,**kwargs):
                for values in generate(): yield Tensor(values)
        handler=object.__new__(ns['PocketTTSEventHandler'])
        handler.cli_args=SimpleNamespace(voice='shefowl',pcm_streaming=True,pcm_buffer_seconds=buffer_seconds,pcm_allow_slow=True)
        class InlineWorker:
            # The executor is a seam. These tests exercise protocol and PCM,
            # without relying on the host's cross-thread event-loop scheduling.
            def submit(self, function, *args):
                result=Future()
                try: result.set_result(function(*args))
                except BaseException as error: result.set_exception(error)
                return result
            def close(self): pass
        handler.tts_model=Model();handler.render_worker=InlineWorker()
        handler.is_streaming=None
        events=[]
        async def write(event): events.append(event)
        handler.write_event=write
        try:
            if streamed:
                await handler.handle_event(Wire('SynthesizeStart',voice=None))
                await handler.handle_event(Wire('SynthesizeChunk',text='synthetic fixture'))
                await handler.handle_event(Wire('Synthesize',text='synthetic fixture',voice=None))
                await handler.handle_event(Wire('SynthesizeStop'))
            else:
                await handler.handle_event(Wire('Synthesize',text='synthetic fixture',voice=None))
            return events
        finally: handler.render_worker.close()

    def streaming_handler(self):
        ns = namespace(ROOT/'tts-pocket-wyoming/wyoming_tts_server.py', {'PocketTTSEventHandler'})
        calls, events = [], []
        first_audio = asyncio.Event()
        class Tensor:
            def detach(self): return self
            def cpu(self): return self
            def numpy(self): return np.full(200, .5, dtype=np.float32)
        class Model:
            sample_rate = 1000
            reference = '/local/reference.wav'
            def get_state_for_audio_prompt(self, prompt): return {}
            def generate_audio_stream(self, **kwargs):
                calls.append(kwargs['text_to_generate'])
                yield Tensor()
        worker = ns['RenderWorker']()
        args = SimpleNamespace(voice='shefowl', pcm_streaming=True,
                               pcm_buffer_seconds=.02, pcm_allow_slow=True)
        info = SimpleNamespace(event=lambda: Wire('Info'))
        handler = ns['PocketTTSEventHandler'](info, args, Model(), worker)
        async def write(event):
            events.append(event)
            if event.type == 'AudioChunk': first_audio.set()
        handler.write_event = write
        return handler, worker, calls, events, first_audio

    async def test_complete_sentence_is_audible_before_text_stop_without_duplicate_audio(self):
        handler, worker, calls, events, first_audio = self.streaming_handler()
        try:
            await handler.handle_event(Wire('SynthesizeStart', voice=None))
            await handler.handle_event(Wire('SynthesizeChunk', text='Pierwsze zdanie. D'))
            await asyncio.wait_for(first_audio.wait(), timeout=2)
            self.assertEqual(calls, ['... Pierwsze zdanie. '])
            self.assertFalse(any(e.type in ('AudioStop', 'SynthesizeStopped') for e in events))
            await handler.handle_event(Wire('SynthesizeChunk', text='rugie zdanie.'))
            await handler.handle_event(Wire('Synthesize', text='compatibility copy', voice=None))
            self.assertTrue(await handler.handle_event(Wire('SynthesizeStop')))
            self.assertEqual(calls, ['... Pierwsze zdanie. ', '... Drugie zdanie.'])
            self.assertEqual(sum(e.type == 'AudioStart' for e in events), 1)
            self.assertEqual(sum(e.type == 'AudioStop' for e in events), 1)
            self.assertEqual(sum(e.type == 'SynthesizeStopped' for e in events), 1)
            self.assertEqual(len(b''.join(e.audio for e in events if e.type == 'AudioChunk')), 800)
        finally:
            await handler.disconnect()
            await asyncio.to_thread(worker.close)

    async def test_disconnect_stops_waiting_for_more_text_and_releases_worker(self):
        handler, worker, calls, events, first_audio = self.streaming_handler()
        try:
            await handler.handle_event(Wire('SynthesizeStart', voice=None))
            await handler.handle_event(Wire('SynthesizeChunk', text='Pierwsze zdanie. D'))
            await asyncio.wait_for(first_audio.wait(), timeout=2)
            await handler.disconnect()
            await asyncio.wait_for(asyncio.to_thread(worker._queue.join), timeout=2)
            self.assertFalse(any(e.type in ('AudioStop', 'SynthesizeStopped') for e in events))
            self.assertEqual(worker.submit(lambda: 42).result(timeout=1), 42)
        finally:
            await handler.disconnect()
            await asyncio.to_thread(worker.close)

    def test_sentence_split_keeps_abbreviations_and_decimal_fragments(self):
        ns = namespace(ROOT/'tts-pocket-wyoming/wyoming_tts_server.py', {'PocketTTSEventHandler'})
        split = ns['split_ready_text']
        for text in ('To np. przykład', 'Wynik 21.05', 'Czekam...', 'dr. Kowalski'):
            with self.subTest(text=text): self.assertEqual(split(text), ([], text))
        self.assertEqual(split('Gotowe. Następne'), (['Gotowe. '], 'Następne'))

    async def test_streamed_text_is_synthesized_once_and_quiet_tail_is_preserved(self):
        calls=[]
        def generate():
            calls.append(1)
            yield np.concatenate([np.full(200,.5),np.full(2400,.001)]).astype(np.float32)
        events=await self.run_request(generate,streamed=True)
        self.assertEqual(len(calls),1)
        self.assertEqual(events[-1].type,'SynthesizeStopped')
        pcm=b''.join(e.audio for e in events if e.type=='AudioChunk')
        self.assertEqual(len(pcm)//2,2600)

    async def test_full_buffer_path_preserves_quiet_tail(self):
        def generate():
            yield np.concatenate([np.full(200,.5),np.full(2400,.001)]).astype(np.float32)
        events = await self.run_request(generate, buffer_seconds=30)
        pcm = b''.join(e.audio for e in events if e.type == 'AudioChunk')
        self.assertEqual(len(pcm)//2, 2600)

    async def test_text_limit_is_cumulative_across_chunks(self):
        handler, worker, calls, events, first_audio = self.streaming_handler()
        try:
            await handler.handle_event(Wire('SynthesizeStart', voice=None))
            await handler.handle_event(Wire('SynthesizeChunk', text='x'*1300))
            with self.assertRaisesRegex(ValueError, 'text exceeds'):
                await handler.handle_event(Wire('SynthesizeChunk', text='y'*800))
            self.assertEqual(calls, [])
            self.assertFalse(any(e.type == 'SynthesizeStopped' for e in events))
        finally:
            await handler.disconnect()
            await asyncio.to_thread(worker.close)

    async def test_model_failure_does_not_send_success_completion(self):
        def generate():
            raise ValueError('synthetic failure')
            yield
        events=await self.run_request(generate,streamed=True)
        self.assertEqual([e.type for e in events],['AudioStart','Error'])

    async def test_second_sentence_failure_does_not_replay_first_or_finish_successfully(self):
        handler, worker, calls, events, first_audio = self.streaming_handler()
        generate = handler.tts_model.generate_audio_stream
        def fail_second(**kwargs):
            if 'Drugie' in kwargs['text_to_generate']:
                raise ValueError('synthetic second sentence failure')
            yield from generate(**kwargs)
        handler.tts_model.generate_audio_stream = fail_second
        try:
            await handler.handle_event(Wire('SynthesizeStart', voice=None))
            await handler.handle_event(Wire('SynthesizeChunk', text='Pierwsze zdanie. Drugie zdanie.'))
            await asyncio.wait_for(first_audio.wait(), timeout=2)
            self.assertFalse(await handler.handle_event(Wire('SynthesizeStop')))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(b''.join(e.audio for e in events if e.type == 'AudioChunk')), 400)
            self.assertTrue(any(e.type == 'Error' for e in events))
            self.assertFalse(any(e.type in ('AudioStop', 'SynthesizeStopped') for e in events))
        finally:
            await handler.disconnect()
            await asyncio.to_thread(worker.close)


if __name__=='__main__': unittest.main()
