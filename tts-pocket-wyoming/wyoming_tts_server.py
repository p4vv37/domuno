#!/usr/bin/env python3
"""Independent Wyoming bridge for the official Kyutai Pocket TTS server.

The model and Rust runtime stay in their own processes. This file only translates
Wyoming synthesis requests to the official ``POST /tts`` endpoint and translates
the streamed WAV response back to Wyoming PCM events.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing, suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import struct
import sys
from typing import AsyncIterator, Optional

import aiohttp
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import async_read_event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop

from text_normalization import normalize_pl_numbers

_LOGGER = logging.getLogger('domuno-tts')
_MAX_HEADER = 1 << 20
_MAX_TEXT_CHARS = 4_000
_MAX_PCM_BYTES = 16 << 20
_CHUNK_MS = 20
_KYUTAI_WEIGHTS = 'https://huggingface.co/kyutai/pocket-tts-without-voice-cloning'
_VOICES = {
    'pl': ('shefowl', 'pl-PL', 'shefowl', 'https://huggingface.co/shefowl/pocket-tts-polish-6l'),
    'en': ('alba', 'en-US', 'Kyutai', _KYUTAI_WEIGHTS),
    'es': ('lola', 'es-ES', 'Kyutai', _KYUTAI_WEIGHTS),
}


class BackendError(RuntimeError):
    """The official TTS backend could not produce a complete response."""


class BackendBusy(BackendError):
    """The single synthesis slot is already in use."""


@dataclass(frozen=True)
class WavFormat:
    rate: int
    width: int
    channels: int

    @property
    def frame_bytes(self) -> int:
        return self.width * self.channels


def parse_wav_header(data: bytes) -> Optional[tuple[WavFormat, int]]:
    """Return the PCM format and first data byte, or None while incomplete."""
    if len(data) < 12:
        return None
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise BackendError('backend did not return a RIFF/WAVE stream')
    fmt = None
    offset = 12
    while True:
        if offset + 8 > len(data):
            return None
        chunk_id = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], 'little')
        if chunk_id == b'fmt ':
            end = offset + 8 + size
            if len(data) < end:
                return None
            if size < 16:
                raise BackendError('backend WAV format chunk is too short')
            audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack_from(
                '<HHIIHH', data, offset + 8)
            if audio_format != 1:
                raise BackendError('backend WAV is not uncompressed PCM')
            if channels != 1 or not 8_000 <= rate <= 48_000 or bits != 16:
                raise BackendError('unsupported backend WAV format')
            if block_align != 2 or byte_rate != rate * block_align:
                raise BackendError('backend WAV block alignment is invalid')
            fmt = WavFormat(rate=rate, width=bits // 8, channels=channels)
            offset = end + (size & 1)
        elif chunk_id == b'data':
            if fmt is None:
                raise BackendError('backend WAV data appears before its format')
            # Kyutai uses a deliberately huge placeholder data size. Ignore it
            # and read PCM until HTTP EOF.
            if offset + 8 > _MAX_HEADER:
                raise BackendError('backend WAV header is too large')
            return fmt, offset + 8
        else:
            if size > _MAX_HEADER:
                raise BackendError('backend WAV header is too large')
            end = offset + 8 + size + (size & 1)
            if len(data) < end:
                return None
            offset = end


class PocketBackend:
    """Minimal async client for the official Pocket TTS HTTP server."""

    def __init__(self, url: str, timeout: float = 300.0):
        self._tts_url = url.rstrip('/') + '/tts'
        self._health_url = url.rstrip('/') + '/health'
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=10, sock_read=timeout))
        self._lock = asyncio.Lock()

    async def close(self):
        await self._session.close()

    async def healthy(self) -> bool:
        try:
            async with self._session.get(self._health_url, timeout=aiohttp.ClientTimeout(total=3)) as response:
                return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def synthesize(self, text: str) -> AsyncIterator[AudioStart | AudioChunk]:
        """Yield the audio stream; no successful terminator is implied here."""
        # No await between checking and taking an unlocked asyncio.Lock.
        if self._lock.locked():
            raise BackendBusy()
        async with self._lock:
            try:
                async with self._session.post(self._tts_url, data={'text': text}) as response:
                    if response.status != 200:
                        raise BackendError(f'backend returned HTTP {response.status}')
                    if response.content_type != 'audio/wav':
                        raise BackendError('backend did not return audio/wav')
                    header = bytearray()
                    pcm = bytearray()
                    fmt: Optional[WavFormat] = None
                    chunk_bytes = 0
                    pcm_seen = 0
                    async for raw in response.content.iter_any():
                        if fmt is None:
                            header.extend(raw)
                            parsed = parse_wav_header(bytes(header))
                            if parsed is None:
                                if len(header) > _MAX_HEADER:
                                    raise BackendError('backend WAV header is too large')
                                continue
                            fmt, offset = parsed
                            chunk_bytes = max(fmt.frame_bytes,
                                              (fmt.rate * _CHUNK_MS // 1000) * fmt.frame_bytes)
                            pcm.extend(header[offset:])
                            pcm_seen += len(header) - offset
                            header.clear()
                            yield AudioStart(rate=fmt.rate, width=fmt.width,
                                             channels=fmt.channels)
                        else:
                            pcm.extend(raw)
                            pcm_seen += len(raw)
                        if pcm_seen > _MAX_PCM_BYTES:
                            raise BackendError('backend audio exceeded the size limit')
                        while len(pcm) >= chunk_bytes:
                            chunk = bytes(pcm[:chunk_bytes])
                            del pcm[:chunk_bytes]
                            yield AudioChunk(rate=fmt.rate, width=fmt.width,
                                             channels=fmt.channels, audio=chunk)
                    if fmt is None:
                        raise BackendError('backend WAV is incomplete')
                    if pcm_seen == 0:
                        raise BackendError('backend returned no audio')
                    if len(pcm) % fmt.frame_bytes:
                        raise BackendError('backend returned a partial PCM frame')
                    if pcm:
                        yield AudioChunk(rate=fmt.rate, width=fmt.width,
                                         channels=fmt.channels, audio=bytes(pcm))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise BackendError(f'backend connection failed: {type(exc).__name__}') from exc


class Handler(AsyncEventHandler):
    def __init__(self, reader, writer, backend: PocketBackend, info: Info, language: str = 'pl'):
        super().__init__(reader, writer)
        self.backend = backend
        self.info = info
        self.language = language

    async def run(self) -> None:
        # Keep reading while rendering: Wyoming's default loop waits for the
        # handler and cannot see a disconnect if the backend stops sending PCM.
        incoming = asyncio.create_task(async_read_event(self.reader))
        render = None
        try:
            while (event := await incoming) is not None:
                if not Synthesize.is_type(event.type):
                    if not await self.handle_event(event):
                        return
                    incoming = asyncio.create_task(async_read_event(self.reader))
                    continue
                render = asyncio.create_task(self.handle_event(event))
                incoming = asyncio.create_task(async_read_event(self.reader))
                done, _ = await asyncio.wait((render, incoming),
                                             return_when=asyncio.FIRST_COMPLETED)
                if incoming in done and incoming.result() is None:
                    return
                if not await render:
                    return
                render = None
        except OSError:
            # A reset may surface on either the read or the write side.
            pass
        finally:
            tasks = [task for task in (incoming, render) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.writer.close()

    async def handle_event(self, event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info.event())
            return True
        if SynthesizeStart.is_type(event.type) or SynthesizeChunk.is_type(event.type) \
                or SynthesizeStop.is_type(event.type):
            await self.write_event(Error(
                text='Streaming text input is not supported',
                code='streaming-not-supported').event())
            return True
        if not Synthesize.is_type(event.type):
            return False
        try:
            request = Synthesize.from_event(event)
            if not isinstance(request.text, str):
                raise TypeError
        except (KeyError, TypeError, ValueError):
            await self.write_event(Error(text='Invalid synthesis request',
                                         code='invalid-request').event())
            return True
        text = request.text.strip()
        if not text:
            await self.write_event(Error(text='Text is empty', code='invalid-text').event())
            return True
        if len(request.text) > _MAX_TEXT_CHARS:
            await self.write_event(Error(text=f'Text exceeds {_MAX_TEXT_CHARS} characters',
                                         code='text-too-long').event())
            return True
        try:
            if self.language == 'pl':
                text = normalize_pl_numbers(text)
            if len(text) > _MAX_TEXT_CHARS:
                await self.write_event(Error(text=f'Normalized text exceeds {_MAX_TEXT_CHARS} characters',
                                             code='text-too-long').event())
                return True
            async with aclosing(self.backend.synthesize(text)) as stream:
                async for audio in stream:
                    await self.write_event(audio.event())
            await self.write_event(AudioStop().event())
        except OSError:
            return False
        except BackendBusy:
            await self.write_event(Error(text='TTS is busy; retry later', code='busy').event())
        except Exception as exc:
            _LOGGER.warning('TTS synthesis failed: %s', type(exc).__name__)
            await self.write_event(Error(text='Speech synthesis failed',
                                         code='synthesis-failed').event())
        return True


def make_info(voice: str, version: str, language: str = 'pl') -> Info:
    _, locale, creator, model_url = _VOICES[language]
    program_attribution = Attribution(
        name='Kyutai Pocket TTS',
        url='https://github.com/kyutai-labs/pocket-tts')
    return Info(tts=[TtsProgram(
        name=f'pocket-tts-{language}',
        description='Pocket TTS / official Kyutai Rust',
        attribution=program_attribution,
        installed=True,
        version=version,
        voices=[TtsVoice(
            name=voice,
            description=f'Pocket TTS ({locale})',
            attribution=Attribution(name=creator, url=model_url),
            installed=True,
            version=version,
            languages=[locale])],
        supports_synthesize_streaming=False)])


async def start_rust_backend(binary: str, config: str, voice: str, port: int):
    command = [binary, 'serve', '--host', '127.0.0.1', '--port', str(port),
               '--config', config, '--default-voice', voice, '--quantize']
    return await asyncio.create_subprocess_exec(
        *command, env={**os.environ, 'RAYON_NUM_THREADS': '1'},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)


async def wait_ready(backend: PocketBackend, process, timeout: float):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise BackendError('backend process exited during startup')
        if await backend.healthy():
            return
        await asyncio.sleep(1)
    raise BackendError('backend did not become ready')


async def stop_rust_backend(process, timeout=5):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
    await process.wait()


async def run(args):
    backend = PocketBackend(args.backend_url or f'http://127.0.0.1:{args.backend_port}')
    process = None
    tasks = set()
    server = None
    loop = asyncio.get_running_loop()
    signals = []
    try:
        if args.backend_binary:
            process = await start_rust_backend(
                args.backend_binary, args.config, args.backend_voice or args.voice,
                args.backend_port)
            await wait_ready(backend, process, args.startup_timeout)
        info = make_info(args.voice, args.version, args.language)
        server = AsyncServer.from_uri(f'tcp://{args.host}:{args.port}')
        task = asyncio.create_task(server.run(
            lambda reader, writer: Handler(reader, writer, backend, info, args.language)))
        tasks.add(task)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
                signals.append(sig)
        stop_task = asyncio.create_task(stop.wait())
        tasks.add(stop_task)
        child_task = asyncio.create_task(process.wait()) if process is not None else None
        if child_task is not None:
            tasks.add(child_task)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()  # Surface bind failures instead of leaving them unobserved.
        if child_task in done and stop_task not in done:
            raise BackendError(f'backend exited unexpectedly (status {child_task.result()})')
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if server is not None:
                await server.stop()
        finally:
            try:
                await backend.close()
            finally:
                if process is not None:
                    await stop_rust_backend(process)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=10201)
    parser.add_argument('--backend-url', help='Defaults to loopback on --backend-port')
    parser.add_argument('--backend-binary', default=os.environ.get('POCKET_TTS_BACKEND_BINARY', ''))
    parser.add_argument('--backend-port', type=int, default=18080)
    parser.add_argument('--language', choices=tuple(_VOICES),
                        default=os.environ.get('AGENT_LANGUAGE') or 'pl')
    parser.add_argument('--assets-dir', type=Path, default=Path('/assets'))
    parser.add_argument('--config', default=os.environ.get('POCKET_TTS_CONFIG'))
    parser.add_argument('--voice', default=os.environ.get('POCKET_TTS_VOICE'))
    parser.add_argument('--backend-voice', default=os.environ.get('POCKET_TTS_BACKEND_VOICE'))
    parser.add_argument('--version', default=os.environ.get('POCKET_TTS_VERSION', 'kyutai-v4.0.1'))
    parser.add_argument('--startup-timeout', type=float, default=120)
    args = parser.parse_args(argv)
    if args.language not in _VOICES:
        parser.error('AGENT_LANGUAGE must be pl, en or es')
    profile = args.assets_dir if args.language == 'pl' else args.assets_dir / args.language
    args.config = args.config or str(profile / 'config.yaml')
    args.voice = args.voice or _VOICES[args.language][0]
    reference = 'reference.wav' if args.language == 'pl' else 'reference.safetensors'
    args.backend_voice = args.backend_voice or str(profile / reference)
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except BackendError as exc:
        print(f'TTS backend error: {exc}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
