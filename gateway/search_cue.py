"""Prepare a cached Wyoming TTS announcement off the gateway request path."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import wave

from wait_sound import from_environment as audio_from_environment

PHRASES = {
    'pl': ('Sprawdzam w internecie.', 'shefowl', 'pl-PL'),
    'en': ('I’m checking online.', 'alba', 'en-US'),
    'es': ('Voy a buscar en internet.', 'lola', 'es-ES'),
}


def synthesize(path, host, port, text, voice, locale):
    deadline = time.monotonic() + 20
    audio, fmt = bytearray(), None
    with socket.create_connection((host, port), timeout=4) as conn, conn.makefile('rb') as reader:
        event = {'type': 'synthesize', 'data': {'text': text, 'voice': {'name': voice, 'language': locale}}}
        conn.sendall(json.dumps(event).encode() + b'\n')
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('TTS cue deadline exceeded')
            conn.settimeout(min(4, remaining))
            line = reader.readline(8193)
            if not line.endswith(b'\n') or len(line) > 8192:
                raise ValueError('Invalid Wyoming header')
            header = json.loads(line)
            sizes = header.get('data_length', 0), header.get('payload_length', 0)
            if any(type(n) is not int or n < 0 for n in sizes) or sizes[0] > 8192 or len(audio) + sizes[1] > 1024 * 1024:
                raise ValueError('Oversized Wyoming event')
            metadata, pcm = (reader.read(n) for n in sizes)
            if (len(metadata), len(pcm)) != sizes:
                raise ValueError('Incomplete Wyoming event')
            data = {**(header.get('data') or {}), **(json.loads(metadata) if metadata else {})}
            kind = header['type']
            if kind == 'audio-stop':
                break
            if kind not in ('audio-start', 'audio-chunk'):
                raise ValueError('Unexpected Wyoming event')
            current = tuple(data[k] for k in ('channels', 'width', 'rate'))
            if current[0] not in (1, 2) or current[1] not in (1, 2, 3, 4) or not 8000 <= current[2] <= 96000 or fmt not in (None, current):
                raise ValueError('Invalid Wyoming audio format')
            fmt = current
            if kind == 'audio-chunk':
                audio.extend(pcm)
    if not fmt or not audio or len(audio) % (fmt[0] * fmt[1]) or len(audio) > 3 * fmt[0] * fmt[1] * fmt[2]:
        raise ValueError('Invalid cue duration')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        staged = Path(tmp) / 'cue.wav'
        with wave.open(str(staged), 'wb') as output:
            output.setparams((*fmt, 0, 'NONE', 'not compressed'))
            output.writeframes(audio)
        os.replace(staged, path)


class CachedCue:
    def __init__(self, cache, spec, generate=synthesize, player=audio_from_environment):
        fingerprint = hashlib.sha256(json.dumps(spec).encode()).hexdigest()[:20]
        self.path = cache / f'search-{fingerprint}.wav'
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._audio = None
        threading.Thread(target=self._prepare, args=(spec, generate, player), daemon=True).start()

    def _prepare(self, spec, generate, player):
        for delay in (0, 3, 10, 30):
            if self._stop.wait(delay):
                return
            try:
                if not self.path.is_file():
                    generate(self.path, *spec)
                audio = player(cue_file=str(self.path))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
            with self._lock:
                if self._stop.is_set():
                    if audio is not None:
                        audio.close()
                else:
                    self._audio = audio
            return
        print('WARNING: search announcement unavailable; search still works', file=sys.stderr, flush=True)

    def play(self):
        with self._lock:
            return self._audio is not None and self._audio.play()

    def close(self):
        with self._lock:
            self._stop.set()
            if self._audio is not None:
                self._audio.close()
                self._audio = None


def from_environment(language):
    enabled = os.environ.get('SEARCH_CUE', '1')
    if enabled not in ('0', '1'):
        raise ValueError('SEARCH_CUE must be 0 or 1')
    if enabled == '0':
        return None
    path = os.environ.get('SEARCH_CUE_FILE', '')
    if path:
        return audio_from_environment(cue_file=path)
    host = os.environ.get('SEARCH_CUE_TTS_HOST')
    if not host and os.environ.get('TTS_PROVIDER') == 'external':
        return None
    port = int(os.environ.get('SEARCH_CUE_TTS_PORT', '10201'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SEARCH_CUE_TTS_PORT')
    text, default_voice, locale = PHRASES.get(language, PHRASES['en'])
    voice = os.environ.get('SEARCH_CUE_TTS_VOICE') or default_voice
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache').resolve() / 'domuno'
    return CachedCue(cache, (host or '127.0.0.1', port, text, voice, locale))
