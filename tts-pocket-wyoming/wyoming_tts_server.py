#!/usr/bin/env python3
"""
Wyoming Protocol TTS Server for Pocket-TTS

Implements Wyoming protocol TTS server that wraps pocket-tts,
exposing available voices to Home Assistant for selection.
"""

import argparse
import asyncio
import logging
import time
import math
from functools import partial
from typing import Optional

import numpy
from rust_tts_backend import split_ready_text

from tts_render_worker import RenderBusy, RenderWorker, RequestMetrics, PCMStream, StreamCancelled, TextStream

# Polish fine-tune (shefowl/pocket-tts-polish-6l) has a single reference voice.
# Map voice name -> audio prompt (reference.wav ships in the model repo).
PREDEFINED_VOICE_NAMES = {
    "shefowl": "hf://shefowl/pocket-tts-polish-6l/reference.wav",
}
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

_LOGGER = logging.getLogger(__name__)

# Prefix trimming (seconds). These are measured service constants, not deployment
# knobs: the tests inject shorter values into their synthetic namespace.
PREFIX_MIN_DURATION = 0.15   # minimum time before looking for the pause
PREFIX_MAX_DURATION = 1.0    # maximum time to search for the prefix end
PREFIX_SILENCE_GAP = 0.08    # minimum silence gap after the "... " prefix

_VOICE_STATES: dict[str, dict] = {}


class PocketTTSEventHandler(AsyncEventHandler):
    """Event handler for Pocket-TTS Wyoming server."""

    def __init__(
        self,
        wyoming_info: Info,
        cli_args: argparse.Namespace,
        tts_model: "TTSModel",
        render_worker: RenderWorker,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.wyoming_info_event = wyoming_info.event()
        self.tts_model = tts_model
        self.render_worker = render_worker
        self.is_streaming: Optional[bool] = None
        self._synthesize: Optional[Synthesize] = None
        self._stream_text: str = ""
        self._stream_chars = 0
        self._text_source = None
        self._stream_task = None
        self._stream_metrics = None

    async def handle_event(self, event: Event) -> bool:
        """Handle incoming Wyoming protocol events."""
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            _LOGGER.debug("Sent info")
            return True

        try:
            if Synthesize.is_type(event.type):
                if self.is_streaming:
                    # Ignore since this is only sent for compatibility reasons.
                    # For streaming, we expect:
                    # [synthesize-start] -> [synthesize-chunk]+ -> [synthesize]? -> [synthesize-stop]
                    return True

                synthesize = Synthesize.from_event(event)
                return await self._handle_synthesize(synthesize, send_start=True, send_stop=True)

            if SynthesizeStart.is_type(event.type):
                if self.is_streaming:
                    raise ValueError('TTS text stream already started')
                stream_start = SynthesizeStart.from_event(event)
                self.is_streaming = True
                self._stream_text = ''
                self._stream_chars = 0
                self._text_source = TextStream()
                self._stream_task = None
                self._stream_metrics = RequestMetrics()
                self._stream_metrics.mark('request_received')
                self._stream_metrics.mark('text_stream_start')
                self._synthesize = Synthesize(text='', voice=stream_start.voice)
                return True

            if SynthesizeChunk.is_type(event.type):
                if not self.is_streaming or self._synthesize is None:
                    raise ValueError('Send synthesize-start before text chunks')
                chunk = SynthesizeChunk.from_event(event).text
                self._stream_chars += len(chunk)
                if self._stream_chars > getattr(self.cli_args, 'pcm_max_text', 2048) - 4:
                    raise ValueError('TTS text exceeds streaming limit')
                self._stream_metrics.mark('first_text_chunk')
                self._stream_text += chunk
                ready, self._stream_text = split_ready_text(self._stream_text)
                for sentence in ready:
                    self._text_source.send(sentence)
                if ready:
                    self._stream_metrics.mark('first_sentence_ready')
                    self._start_text_render()
                if self._stream_task is not None and self._stream_task.done():
                    if not self._stream_task.result():
                        return False
                return True

            if SynthesizeStop.is_type(event.type):
                if not self.is_streaming or self._synthesize is None:
                    raise ValueError('TTS text stream not started')
                self._stream_metrics.mark('text_stream_stop')
                if self._stream_text.strip():
                    self._text_source.send(self._stream_text)
                    self._stream_metrics.mark('first_sentence_ready')
                    self._start_text_render()
                self._text_source.finish()
                if self._stream_task is not None:
                    if not await self._stream_task:
                        return False
                else:
                    self._stream_metrics.mark('request_empty')
                await self.write_event(SynthesizeStopped().event())
                self.is_streaming = False
                self._stream_text = ''
                self._synthesize = None
                self._stream_task = None
                self._text_source = None
                return True

            return True
        except Exception as err:
            metrics = getattr(self, '_stream_metrics', None)
            if metrics is not None:
                metrics.mark('request_failed')
            self.is_streaming = False
            await self.disconnect()
            await self.write_event(
                Error(text=str(err), code=err.__class__.__name__).event()
            )
            raise err

    def _start_text_render(self):
        if self._stream_task is None:
            self._stream_task = asyncio.create_task(self._handle_synthesize(
                self._synthesize, text_source=self._text_source, metrics=self._stream_metrics))

    async def disconnect(self):
        metrics = getattr(self, '_stream_metrics', None)
        if self.is_streaming and metrics is not None:
            metrics.mark('request_cancelled')
        source = getattr(self, '_text_source', None)
        if source is not None:
            source.cancel()
        task = getattr(self, '_stream_task', None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.is_streaming = False
        self._stream_text = ''
        self._synthesize = None
        self._text_source = None
        self._stream_task = None

    async def _handle_synthesize(
        self, synthesize: Synthesize, send_start: bool = True, send_stop: bool = True,
        text_source=None, metrics=None
    ) -> bool:
        """Handle synthesis request."""

        metrics = metrics or RequestMetrics()
        metrics.mark('request_received')
        raw_text = synthesize.text
        if (getattr(self.cli_args, 'pcm_streaming', False) and
                len(raw_text) > getattr(self.cli_args, 'pcm_max_text', 2048) - 4):
            metrics.mark('request_failed')
            await self.write_event(Error(text='TTS text exceeds streaming limit', code='TextTooLong').event())
            return False
        text = " ".join(raw_text.strip().splitlines())

        if not text and text_source is None:
            metrics.mark('request_empty')
            _LOGGER.warning("Empty text received")
            if send_stop:
                await self.write_event(AudioStop().event())
            return True


        # Add a sacrificial prefix to prevent the first word from being swallowed
        # by the voice prompt "blend region". This prefix audio will be trimmed later.
        text = text_source if text_source is not None else "... " + text

        voice_name: Optional[str] = None

        if synthesize.voice is not None:
            voice_name = synthesize.voice.name

        if voice_name is None:
            voice_name = self.cli_args.voice

        # Extract voice name from model name if it's in format "pocket-tts-{voice}"
        if voice_name and voice_name.startswith("pocket-tts-"):
            voice_name = voice_name.replace("pocket-tts-", "", 1)

        if voice_name not in PREDEFINED_VOICE_NAMES:
            _LOGGER.warning(
                "Voice '%s' not found, using default '%s'", voice_name, self.cli_args.voice
            )
            voice_name = self.cli_args.voice

        assert voice_name is not None

        return await self._handle_pcm_stream(voice_name, text, metrics, send_start, send_stop)

    async def _handle_pcm_stream(self, voice, text, metrics, send_start, send_stop):
        if isinstance(text, str) and len(text) > getattr(self.cli_args, 'pcm_max_text', 2048):
            await self.write_event(Error(text="TTS text exceeds streaming limit", code="TextTooLong").event())
            return False
        stream = PCMStream(asyncio.get_running_loop())
        try:
            job = self.render_worker.submit(self._produce_pcm, voice, text, metrics, stream)
        except RenderBusy:
            metrics.mark('request_busy')
            await self.write_event(Error(text="TTS engine is busy", code="ServerBusy").event())
            return False
        job.add_done_callback(stream.notify)
        sent = False
        try:
            if send_start:
                await self.write_event(AudioStart(rate=self.tts_model.sample_rate, width=2, channels=1).event())
            while True:
                data = await stream.read(job)
                if data is None: break
                await self.write_event(AudioChunk(audio=data, rate=self.tts_model.sample_rate, width=2, channels=1).event())
                sent = True
                metrics.mark('first_pcm_send_complete')
            if not sent:
                raise ValueError('Model produced no deliverable PCM')
            metrics.mark('pcm_delivery_complete')
            if send_stop: await self.write_event(AudioStop().event())
            metrics.mark('request_complete')
        except asyncio.CancelledError:
            stream.cancel()
            metrics.mark('request_cancelled_pending' if job.cancel() else 'request_cancelled_running_or_done')
            raise
        except Exception:
            stream.cancel(); job.cancel()
            metrics.mark('request_failed')
            await self.write_event(Error(text="TTS synthesis failed", code="SynthesisError").event())
            return False
        finally:
            stream.cancel()
            if isinstance(text, TextStream):
                text.cancel()
            def consume(future):
                if not future.cancelled(): future.exception()
            job.add_done_callback(consume)
        return True

    def _produce_pcm(self, voice, text, metrics, stream):
        metrics.mark('render_start')
        remaining = int(self.tts_model.sample_rate * getattr(self.cli_args, 'pcm_max_seconds', 120))
        deadline = time.monotonic() + 130
        try:
            for sentence in ([text] if isinstance(text, str) else text):
                if stream.stopped.is_set():
                    raise StreamCancelled()
                if not sentence.startswith('... '):
                    sentence = '... ' + sentence
                remaining -= self._produce_segment(voice, sentence, metrics, stream, remaining, deadline)
        except StreamCancelled:
            metrics.mark('render_cancelled')
            return
        except BaseException:
            metrics.mark('render_failed')
            raise
        finally:
            metrics.mark('render_closed')
        metrics.mark('render_done')

    def _produce_segment(self, voice, text, metrics, stream, limit, deadline):
        generator = None
        try:
            if voice not in _VOICE_STATES:
                _VOICE_STATES[voice] = self.tts_model.get_state_for_audio_prompt(getattr(self.tts_model, 'reference', None) or PREDEFINED_VOICE_NAMES[voice])
            rate = self.tts_model.sample_rate
            target = max(int(rate * getattr(self.cli_args, 'pcm_buffer_seconds', 2)), int(rate * PREFIX_MAX_DURATION), 1)
            arrays = []; total = 0; incremental = None; tail = numpy.empty(0, dtype=numpy.float32)
            peak = 0.; padding = int(rate * .05); onset = True
            began = time.monotonic()
            generator = self.tts_model.generate_audio_stream(model_state=_VOICE_STATES[voice], text_to_generate=text, copy_state=True)
            for chunk in generator:
                if time.monotonic() > deadline: raise TimeoutError('TTS generation timed out')
                if stream.stopped.is_set(): raise StreamCancelled()
                a = numpy.asarray(chunk.detach().cpu().numpy(), dtype=numpy.float32)
                if a.ndim != 1 or not numpy.isfinite(a).all(): raise ValueError('Invalid model PCM')
                total += len(a)
                if total > limit: raise ValueError('Model PCM exceeds limit')
                if not len(a): continue
                if incremental is not True:
                    arrays.append(a)
                    if incremental is False or total < target: continue
                    incremental = (getattr(self.cli_args, 'pcm_allow_slow', False) or (time.monotonic()-began)/(total/rate) <= .9)
                    if not incremental:
                        metrics.mark('pcm_slow_fallback'); continue
                    metrics.mark('pcm_incremental_start')
                    a = numpy.concatenate(arrays); arrays = []
                peak = max(peak, float(numpy.abs(a).max()))
                threshold = max(peak * .01, 1e-8)
                if onset:
                    indices = numpy.where(numpy.abs(a) > threshold)[0]
                    if not len(indices): continue
                    a = a[max(0, int(indices[0])-padding):]; onset = False
                tail = numpy.concatenate((tail, a))
                indices = numpy.where(numpy.abs(tail) > threshold)[0]
                cut = min(max(0, len(tail)-padding), int(indices[-1])+1) if len(indices) else 0
                if cut:
                    data = (tail[:cut].clip(-1., 1.)*32767).astype('<i2').tobytes()
                    metrics.mark('first_incremental_pcm_ready'); stream.send(data)
                    tail = tail[cut:]
            if incremental is True:
                # A peak-relative silence threshold cannot distinguish a quiet
                # final sentence from silence. At EOF retain every sample that
                # survives PCM quantization; trim only actual PCM-zero suffix.
                pcm_tail = (tail.clip(-1., 1.)*32767).astype('<i2')
                indices = numpy.flatnonzero(pcm_tail)
                if len(indices):
                    pcm_tail = pcm_tail[:int(indices[-1])+padding+1]
                elif len(pcm_tail) > padding: pcm_tail = pcm_tail[:padding]
                if len(pcm_tail): stream.send(pcm_tail.tobytes())
            elif arrays:
                stream.send(self._finish_pcm_arrays(arrays, voice, metrics))
        finally:
            if generator is not None and hasattr(generator, 'close'):
                generator.close()
        return total

    def _finish_pcm_arrays(self, all_audio_arrays, voice_name, metrics):
        sample_rate = self.tts_model.sample_rate
        width, channels = 2, 1
        full_audio = numpy.concatenate(all_audio_arrays)

        # Find and remove the sacrificial prefix ("...") by detecting the pause after it
        # This adapts to different voice speeds rather than using a fixed duration
        silence_threshold = 0.01
        max_amplitude = numpy.abs(full_audio).max()
        threshold = max_amplitude * silence_threshold

        # Minimum time before we start looking for the pause (avoid false early detection)
        min_prefix_samples = int(sample_rate * PREFIX_MIN_DURATION)
        # Maximum time to search for the prefix end
        max_prefix_samples = int(sample_rate * PREFIX_MAX_DURATION)
        # Minimum silence duration to consider it the gap after "..."
        min_silence_samples = int(sample_rate * PREFIX_SILENCE_GAP)

        # Find where the prefix ends by looking for a silence gap
        prefix_end = 0
        if len(full_audio) > min_prefix_samples:
            search_end = min(len(full_audio), max_prefix_samples)
            is_silent = numpy.abs(full_audio[:search_end]) < threshold

            # Look for a silence gap after the minimum prefix duration
            i = min_prefix_samples
            while i < search_end:
                if is_silent[i]:
                    # Found start of silence, check if it's long enough
                    silence_start = i
                    while i < search_end and is_silent[i]:
                        i += 1
                    silence_length = i - silence_start
                    if silence_length >= min_silence_samples:
                        # Found the gap after the prefix - start after this silence
                        prefix_end = i
                        break
                else:
                    i += 1

        if prefix_end > 0:
            _LOGGER.debug("Trimming prefix: %d samples (%.3fs)",
                          prefix_end, prefix_end / sample_rate)
            full_audio = full_audio[prefix_end:]

        # Trim any remaining leading silence
        non_silent_indices = numpy.where(numpy.abs(full_audio) > threshold)[0]
        if len(non_silent_indices) > 0:
            padding_samples = int(sample_rate * 0.05)  # 50ms padding
            first_non_silent = max(0, non_silent_indices[0] - padding_samples)
            full_audio = full_audio[first_non_silent:]

        # Preserve quiet speech in the full-buffer path too. Only PCM zeros
        # are removable at the end, not samples below a peak-relative threshold.
        full_audio = (full_audio.clip(-1.0, 1.0) * 32767).astype('<i2')
        audible = numpy.flatnonzero(full_audio)
        if len(audible):
            full_audio = full_audio[:int(audible[-1]) + int(sample_rate * .05) + 1]
        audio_bytes = full_audio.tobytes()
        if audio_bytes:
            # Full buffering + trimming: first deliverable PCM exists only now.
            # This is not model-stream TTFT or speaker playback latency.
            metrics.mark('first_pcm_ready')

        return audio_bytes


async def main():
    from rust_tts_backend import RustTTSModel
    parser = argparse.ArgumentParser(description='Polish Pocket TTS over Wyoming')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=10201)
    parser.add_argument('--pcm-buffer-seconds', type=float, default=2)
    args = parser.parse_args()
    if not math.isfinite(args.pcm_buffer_seconds) or not .02 <= args.pcm_buffer_seconds <= 30:
        parser.error('PCM buffer must be between 0.02 and 30 seconds')
    args.voice = 'shefowl'
    args.pcm_streaming = True
    args.pcm_allow_slow = False
    args.pcm_max_text = 2048
    args.pcm_max_seconds = 120
    logging.basicConfig(level=logging.INFO)
    model = RustTTSModel.from_environment()
    worker = RenderWorker()
    server = None
    try:
        _VOICE_STATES['shefowl'] = model.get_state_for_audio_prompt(model.reference)
        attribution = Attribution(name='shefowl', url='https://huggingface.co/shefowl/pocket-tts-polish-6l')
        voice = TtsVoice(name='shefowl', description='Polish reference voice', attribution=attribution,
                         installed=True, version=None, languages=['pl'], speakers=None)
        info = Info(tts=[TtsProgram(name='pocket-tts-pl', description='Pocket TTS Polish / Rust CPU',
                                   attribution=attribution, installed=True, voices=[voice], version='1.0.1',
                                   supports_synthesize_streaming=True)])
        server = AsyncServer.from_uri(f'tcp://{args.host}:{args.port}')
        _LOGGER.info('Ready: Polish voice shefowl')
        await server.run(partial(PocketTTSEventHandler, info, args, model, worker))
    finally:
        if server is not None:
            await server.stop()
        await asyncio.to_thread(worker.close)
        await asyncio.to_thread(model.close)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
