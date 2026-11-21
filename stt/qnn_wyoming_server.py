"""Experimental Polish QNN Wyoming endpoint. Never enables CPU fallback."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone
import signal
from time import perf_counter
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioStart, AudioChunk, AudioStop
from wyoming.event import Event
from wyoming.info import Info, Describe, AsrProgram, AsrModel, Attribution
from wyoming.server import AsyncEventHandler, AsyncServer

LOG = logging.getLogger(__name__)
MAX_BYTES = 16000 * 2 * 30
# A static Polish vocabulary prompt. It biases the decoder toward command words
# and common rooms without seeing the live HA catalog. Override with STT_PROMPT.
DEFAULT_PROMPT = (
    'Włącz światło, wyłącz światło, otwórz roletę, zamknij roletę, '
    'ustaw pozycję rolety, sypialnia, gabinet, salon, kuchnia.'
)
ATTRIBUTION = Attribution(
    name='Qualcomm / OpenAI',
    url='https://huggingface.co/qualcomm/Whisper-Large-V3-Turbo-Quantized')
# Keep the legacy program name so existing HA pipelines keep the same identity.
INFO = Info(asr=[AsrProgram(
    name='whisper-small-qnn-arduino', attribution=ATTRIBUTION, installed=True,
    description='EXPERIMENTAL: Whisper Large-v3-Turbo W8A16 on Arduino NPU', version='0.1',
    models=[AsrModel(
        name='large-v3-turbo-w8a16-qnn', attribution=ATTRIBUTION, installed=True,
        description='Multilingual Large-v3-Turbo W8A16, Polish, QNN HTP',
        version='v0.66.0', languages=['pl'])],
    requires_external_vad=True)])


def pcm_decode(raw):
    import numpy as np
    return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0


async def _finish_worker(future):
    """Delay any number of client cancellations until native/IO work finishes."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        result = future.result()  # Consume worker exceptions even after cancellation.
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class State:
    def __init__(self, engine, pcm_decoder=pcm_decode, info=None):
        self.info = INFO if info is None else info
        self.engine = engine
        self.decode = pcm_decoder
        self.lock = asyncio.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qnn-asr')

    def close(self):
        # Queue cleanup after any native inference, even if its client cancelled.
        try:
            self.pool.submit(self.engine.close).result(timeout=40)
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)


def emit_stt_metric(started, outcome, audio_s, result=None, queue_ms=None):
    """Log only timing fields, never transcript, tokens or exception messages."""
    try:
        record = {'schema_version': 1, 'service': 'stt',
                  'request_id': uuid.uuid4().hex,
                  'timestamp': datetime.now(timezone.utc).isoformat(),
                  'outcome': outcome, 'audio_s': audio_s,
                  'total_ms': (perf_counter() - started) * 1000,
                  'queue_ms': queue_ms}
        timings = (result or {}).get('timings_seconds') or {}
        for source, target in (('preprocess', 'preprocess_ms'), ('encoder', 'encoder_ms'),
                               ('decoder_total', 'decode_ms'), ('total', 'engine_ms')):
            value = timings.get(source)
            record[target] = (value * 1000 if type(value) in (int, float)
                              and math.isfinite(value) and value >= 0 else None)
        LOG.info('stt_metric %s', json.dumps(record, separators=(',', ':')))
    except Exception:
        pass


class Handler(AsyncEventHandler):
    def __init__(self, reader, writer, state):
        super().__init__(reader, writer)
        self.state = state
        self.audio = bytearray()
        self.started = False

    async def fail(self, code, text):
        self.audio.clear()
        self.started = False
        await self.write_event(Event(type='error', data={'code':code, 'text':text}))
        return False

    async def handle_event(self, event):
        if Describe.is_type(event.type):
            await self.write_event(getattr(self.state, 'info', INFO).event())
        elif Transcribe.is_type(event.type):
            language = Transcribe.from_event(event).language
            if language and language.lower().replace('_','-') not in ('pl','pl-pl'):
                return await self.fail('unsupported-language', 'This experimental endpoint supports Polish only')
            self.audio.clear()
            self.started = False
        elif AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            if (start.rate, start.width, start.channels) != (16000, 2, 1):
                return await self.fail('unsupported-audio', 'Expected PCM 16000 Hz, 16-bit, mono')
            self.audio.clear()
            self.started = True
        elif AudioChunk.is_type(event.type):
            if not self.started:
                return await self.fail('audio-not-started', 'Send audio-start first')
            chunk = AudioChunk.from_event(event)
            if (chunk.rate, chunk.width, chunk.channels) != (16000,2,1):
                return await self.fail('unsupported-audio', 'Chunk format changed')
            if len(self.audio) + len(chunk.audio) > MAX_BYTES:
                return await self.fail('audio-too-long', 'Limit is 30 seconds; audio was not truncated')
            self.audio.extend(chunk.audio)
        elif AudioStop.is_type(event.type):
            if not self.started or not self.audio or len(self.audio) % 2:
                return await self.fail('invalid-audio', 'Missing or malformed PCM')
            began = perf_counter()
            self.started = False
            raw = bytes(self.audio)
            self.audio.clear()
            try:
                await asyncio.wait_for(self.state.lock.acquire(), timeout=2)
            except asyncio.TimeoutError:
                emit_stt_metric(began, 'busy', len(raw)/32000)
                return await self.fail('busy', 'NPU STT is busy; retry later')
            except asyncio.CancelledError:
                emit_stt_metric(began, 'cancelled', len(raw)/32000)
                raise
            queue_ms = (perf_counter() - began) * 1000
            outcome, result = 'error', None
            try:
                def transcribe():
                    return self.state.engine.transcribe(self.state.decode(raw))
                future = asyncio.get_running_loop().run_in_executor(self.state.pool, transcribe)
                result = await _finish_worker(future)
                await self.write_event(Transcript(text=result['text'].strip()).event())
                outcome = 'completed'
            except asyncio.CancelledError:
                outcome = 'cancelled'
                raise
            except Exception:
                LOG.error('STT_PHASE transcription-failed')
                return await self.fail('transcription-failed', 'QNN transcription failed; inspect server log')
            finally:
                self.state.lock.release()
                emit_stt_metric(began, outcome, len(raw)/32000, result, queue_ms)
        return True


async def serve(args):
    from qnn_whisper_engine import QnnWhisperEngine
    engine = QnnWhisperEngine(model_dir=args.model_dir, tokenizer=args.tokenizer,
                              backend=args.backend, metadata=args.metadata,
                              prompt=args.prompt)
    state = State(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    server = AsyncServer.from_uri(args.uri)
    task = asyncio.create_task(server.run(functools.partial(Handler, state=state)))
    waiter = asyncio.create_task(stop.wait())
    LOG.info('NPU_MODEL_READY; endpoint=%s; no CPU fallback', args.uri)
    try:
        done, _ = await asyncio.wait([task,waiter], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
    finally:
        task.cancel()
        waiter.cancel()
        await asyncio.gather(task,waiter,return_exceptions=True)
        state.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--uri',default='tcp://0.0.0.0:10301')
    p.add_argument('--metadata', required=True, help='Public Turbo artifact metadata')
    p.add_argument('--prompt', default=os.environ.get('STT_PROMPT') or DEFAULT_PROMPT,
                   help='Static Polish decoder prompt; empty disables it')
    for name in ['model-dir','tokenizer','backend']:
        p.add_argument('--'+name,required=True)
    args = p.parse_args(argv)
    return args


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(parse_args()))
