"""Experimental Polish QNN Wyoming endpoint. Never enables CPU fallback."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import logging
import signal
from time import perf_counter
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioStart, AudioChunk, AudioStop
from wyoming.event import Event
from wyoming.info import Info, Describe, AsrProgram, AsrModel, Attribution
from wyoming.server import AsyncEventHandler, AsyncServer

LOG = logging.getLogger(__name__)
MAX_BYTES = 16000 * 2 * 30
ATTRIBUTION = Attribution(name='Qualcomm / OpenAI', url='https://huggingface.co/qualcomm/Whisper-Small')
INFO = Info(asr=[AsrProgram(name='whisper-small-qnn-arduino', attribution=ATTRIBUTION,
    installed=True, description='EXPERIMENTAL: Whisper Small FP16 on Arduino NPU', version='0.1',
    models=[AsrModel(name='small-fp16-qnn', attribution=ATTRIBUTION, installed=True,
        description='Multilingual Small, Polish, QNN HTP; not INT8', version='v0.66.0', languages=['pl'])],
    requires_external_vad=True)])


def info_for_profile(profile='small-fp16'):
    """Keep HA's legacy program/entity identity while advertising the loaded model.

    Transcribe.model remains advisory/ignored: existing HA pipelines can send the
    legacy Small model ID; the explicitly constructed engine never switches models.
    """
    if profile == 'small-fp16':
        return INFO
    profiles = {
        'medium-w8a16': ('Medium', 'Whisper-Medium-Quantized'),
        'large-v3-turbo-w8a16': ('Large-v3-Turbo', 'Whisper-Large-V3-Turbo-Quantized'),
    }
    if profile not in profiles:
        raise ValueError('Unsupported model profile')
    label, repository = profiles[profile]
    attribution = Attribution(name='Qualcomm / OpenAI',
                              url='https://huggingface.co/qualcomm/' + repository)
    return Info(asr=[AsrProgram(name='whisper-small-qnn-arduino', attribution=attribution,
        installed=True, description=f'EXPERIMENTAL: Whisper {label} W8A16 on Arduino NPU', version='0.1',
        models=[AsrModel(name=profile+'-qnn', attribution=attribution, installed=True,
            description=f'Multilingual {label} W8A16, Polish, QNN HTP', version='v0.66.0', languages=['pl'])],
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
                return await self.fail('busy', 'NPU STT is busy; retry later')
            try:
                def transcribe():
                    return self.state.engine.transcribe(self.state.decode(raw))
                future = asyncio.get_running_loop().run_in_executor(self.state.pool, transcribe)
                result = await _finish_worker(future)
                await self.write_event(Transcript(text=result['text'].strip()).event())
                LOG.info('STT completed in %.3f s; samples=%d', perf_counter()-began, len(raw)//2)
            except Exception:
                LOG.error('STT_PHASE transcription-failed')
                return await self.fail('transcription-failed', 'QNN transcription failed; inspect server log')
            finally:
                self.state.lock.release()
        return True


async def serve(args):
    from qnn_whisper_engine import QnnWhisperEngine
    engine = QnnWhisperEngine(model_dir=args.model_dir, tokenizer=args.tokenizer, backend=args.backend,
                              model_profile=args.model_profile, metadata=args.metadata)
    state = State(engine, info=info_for_profile(args.model_profile))
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
    p.add_argument('--model-profile', choices=['small-fp16','medium-w8a16','large-v3-turbo-w8a16'], default='small-fp16')
    p.add_argument('--metadata', help='Public artifact metadata required for explicit W8A16 profile')
    for name in ['model-dir','tokenizer','backend']:
        p.add_argument('--'+name,required=True)
    args = p.parse_args(argv)
    return args


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(parse_args()))
