"""Opt-in CPU-only Pocket Rust adapter; no model downloads or audio files.

The configured daemon must be the pinned Pocket WAV-streaming implementation.
It appends 200ms silence, intentionally preserved here for existing prefix/PCM
policy to handle. A clean close-delimited HTTP EOF cannot prove semantic EOS;
HTTP framing errors, empty/odd PCM and invalid headers do fail synthesis.
"""
import http.client
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlencode, urlsplit

# Preserve structured values before considering standalone numbers.
_NUMBER_RE = re.compile(
    r"(?P<protected>https?://[^\s]+"
    r"|(?<!\w)[0-9]+(?:[.:/-][0-9]+){2,}(?!\w)"
    r"|(?<!\w)[0-9]+[:/-][0-9]+(?!\w)"
    r"|\b(?=\w*[0-9])(?=\w*[A-Za-z_])\w+(?:[./:-]\w+)*\b)"
    r"|(?<![\w.,:/+-])(?P<number>[+-]?(?:[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+|[0-9]+)"
    r"(?:[.,][0-9]+)?)(?!\w|[.,:/-][0-9])(?P<unit>[ \t]*(?:°C|%))?"
)


def normalize_pl_numbers(text):
    """Speak standalone numbers and units, preserving structured identifiers.

    Fractional digits are read individually to preserve zeros and precision.
    Dates, times, URLs and identifiers are deliberately left unchanged.
    """
    from num2words import num2words

    def replace(match):
        if match['protected']:
            return match[0]
        token = re.sub(r'[ \u00a0\u202f]', '', match['number']).replace(',', '.')
        sign = 'minus ' if token.startswith('-') else 'plus ' if token.startswith('+') else ''
        integer, separator, fraction = token.lstrip('+-').partition('.')
        value = int(integer)
        words = sign + num2words(value, lang='pl')
        if separator:
            words += ' przecinek ' + ' '.join(num2words(int(digit), lang='pl') for digit in fraction)
        unit = (match['unit'] or '').strip()
        if unit == '%':
            words += ' procent'
        elif unit == '°C':
            degree = ('stopnia' if separator else 'stopień' if value == 1 else
                      'stopnie' if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14 else 'stopni')
            words += ' ' + degree + ' Celsjusza'
        return words

    return _NUMBER_RE.sub(replace, text)


class RustBackendError(RuntimeError):
    """Safe payload-free backend failure."""


class RustBackendBusy(RustBackendError):
    """An admitted generation has not closed its response yet."""


class _Audio:
    def __init__(self, values): self.values = values
    def detach(self): return self
    def cpu(self): return self
    def numpy(self): return self.values


def _sentence_spans(text):
    """Conservative, lossless boundaries, not a tokenizer or a word budget.

    Ambiguous ellipses, initials and common Polish abbreviations stay intact.
    Whitespace belongs to the preceding span, including trailing whitespace.
    """
    start = 0
    abbreviations = {'dr', 'prof', 'np', 'm.in', 'itd', 'itp', 'ul', 'godz', 'nr'}
    for match in re.finditer(r'[.!?]+[ \t\r\n]+(?=[A-ZĄĆĘŁŃÓŚŹŻ])', text):
        punctuation = match.group().rstrip()
        end = match.start() + len(punctuation)
        token = re.search(r'([\w.]+)$', text[:end - 1])
        if (punctuation.startswith('.') and
                (len(punctuation) != 1 or
                 (token and (token[1].lower() in abbreviations or len(token[1]) == 1
                             or token[1].isdigit())))):
            continue
        yield text[start:match.end()]
        start = match.end()
    if text[start:].strip():
        yield text[start:]


def split_ready_text(text):
    """Return complete, conservative sentence spans and the unfinished suffix."""
    spans = list(_sentence_spans(text))
    return spans[:-1], spans[-1] if spans else ''


class RustTTSModel:
    sample_rate = 24000

    def __init__(self, *, base_url, reference=None, timeout=10.0,
                 max_audio_seconds=120, max_generation_seconds=130):
        url = urlsplit(base_url)
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or url.path not in ('', '/') or url.query or url.fragment or url.username):
            raise RustBackendError('Rust backend requires a loopback URL')
        self._host, self._port = url.hostname, url.port
        self.reference = reference
        self.timeout = timeout
        self.max_audio_seconds = min(120, max_audio_seconds)
        # Public construction may tighten bounds, never raise production caps.
        self.max_generation_seconds = min(130, max_generation_seconds)
        self._admission = threading.Lock()
        self._state_lock = threading.Lock()
        self._connection = None
        self._closed = False
        self._child = None

    @classmethod
    def from_environment(cls):
        """Start only an explicit executable with an entirely local config.

        Required variables: POCKET_TTS_RUST_BINARY, POCKET_TTS_RUST_CONFIG,
        POCKET_TTS_RUST_REFERENCE. Paths must already exist and be absolute.
        No fallback voice, tokenizer or model is downloaded.
        """
        paths = []
        for name in ('BINARY', 'CONFIG', 'REFERENCE'):
            value = os.environ.get('POCKET_TTS_RUST_' + name, '')
            if not value or not Path(value).is_absolute() or not Path(value).is_file():
                raise RustBackendError('Rust backend requires existing absolute asset paths')
            paths.append(value)
        binary, config, reference = paths
        # Fail closed on nonlocal assets. The accepted path values deliberately
        # exclude YAML aliases/tags/multiline scalars; preserve all other config.
        text = Path(config).read_text(encoding='utf-8')
        if re.search(r'(?:https?|hf)://', text):
            raise RustBackendError('Rust backend config must be offline')
        found = set()
        for line in text.splitlines():
            match = re.match(r'^\s*(weights_path|tokenizer_path)\s*:\s*(.*?)\s*$', line)
            if not match:
                continue
            key, value = match.groups()
            if value.startswith(('"', "'")) and value.endswith(value[0]):
                value = value[1:-1]
            if not Path(value).is_absolute() or not Path(value).is_file():
                raise RustBackendError('Rust backend config assets must exist locally')
            found.add(key)
        if found != {'weights_path', 'tokenizer_path'}:
            raise RustBackendError('Rust backend config must specify local model and tokenizer')
        # Bind a temporary loopback port solely to select an unused port. There
        # is a short release/bind race; never connect to an existing listener.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        model = cls(base_url='http://127.0.0.1:%d' % port, reference=reference)
        env = dict(os.environ, RAYON_NUM_THREADS='1', RUST_LOG='error',
                   HF_HUB_OFFLINE='1', FIRST_CHUNK_LENGTH_SECONDS='0')
        try:
            model._child = subprocess.Popen(
                [binary, 'serve', '--config', config, '--default-voice', reference,
                 '--quantize', '--host', '127.0.0.1', '--port', str(port)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if model._child.poll() is not None:
                    raise RustBackendError('Rust backend startup failed')
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=.25)
                try:
                    conn.request('GET', '/health')
                    response = conn.getresponse()
                    if response.status == 200 and response.read(128) == b'{"status":"healthy"}':
                        if model._child.poll() is not None:
                            raise RustBackendError('Rust backend startup failed')
                        return model
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    conn.close()
                time.sleep(.05)
            raise RustBackendError('Rust backend readiness timed out')
        except Exception:
            model.close()
            raise RustBackendError('Rust backend startup failed') from None

    def get_state_for_audio_prompt(self, prompt):
        # Voice selection belongs to startup, never to individual HTTP requests.
        if self.reference is not None and str(prompt) != str(self.reference):
            raise RustBackendError('Rust backend voice differs from configured reference')
        return self

    def generate_audio_stream(self, model_state=None, text_to_generate=None, **kwargs):
        import numpy as np
        if not self._admission.acquire(blocking=False):
            raise RustBackendBusy('Rust backend is busy')
        conn = None
        response = None
        drained = False
        cancelled = False
        try:
            if not isinstance(text_to_generate, str) or not text_to_generate.strip():
                raise RustBackendError('Rust backend requires text')
            if not text_to_generate.replace('.', '').strip():
                raise RustBackendError('Rust backend requires spoken text')
            text_to_generate = normalize_pl_numbers(text_to_generate)
            if len(text_to_generate) > 2048:
                raise RustBackendError('Rust backend text exceeds request bound')
            total = 0
            deadline = time.monotonic() + self.max_generation_seconds
            for index, span in enumerate(_sentence_spans(text_to_generate)):
                if time.monotonic() > deadline:
                    raise RustBackendError('Rust backend generation timed out')
                segment = span if index == 0 else '... ' + span
                drained = False
                with self._state_lock:
                    if self._closed:
                        raise RustBackendError('Rust backend is closed')
                    conn = http.client.HTTPConnection(
                        self._host, self._port,
                        timeout=min(self.timeout, max(.001, deadline - time.monotonic())))
                    self._connection = conn
                conn.request('POST', '/tts', urlencode({'text': segment}).encode('ascii'),
                             {'Content-Type': 'application/x-www-form-urlencoded'})
                transport = conn.sock
                transport.settimeout(min(self.timeout, max(.001, deadline - time.monotonic())))
                response = conn.getresponse()
                if response.status != 200 or response.getheader('Content-Type', '').split(';')[0] != 'audio/wav':
                    raise RustBackendError('Rust backend request failed')
                header = response.read(44)
                if len(header) != 44:
                    raise RustBackendError('Invalid Rust PCM stream')
                fields = struct.unpack('<4sI4s4sIHHIIHH4sI', header)
                if (fields[0] != b'RIFF' or fields[2:10] !=
                        (b'WAVE', b'fmt ', 16, 1, 1, 24000, 48000, 2)
                        or fields[10] != 16 or fields[11] != b'data'):
                    raise RustBackendError('Invalid Rust PCM stream')
                remaining = b''
                segment_total = 0
                while True:
                    if time.monotonic() > deadline:
                        raise RustBackendError('Rust backend generation timed out')
                    if not response.isclosed():
                        transport.settimeout(min(self.timeout, max(.001, deadline - time.monotonic())))
                    data = response.read1(4096)
                    if time.monotonic() > deadline:
                        raise RustBackendError('Rust backend generation timed out')
                    if not data:
                        break
                    total += len(data)
                    segment_total += len(data)
                    if total > int(self.max_audio_seconds * self.sample_rate) * 2:
                        raise RustBackendError('Rust backend audio exceeds duration bound')
                    data = remaining + data
                    even = len(data) - len(data) % 2
                    remaining = data[even:]
                    if even:
                        yield _Audio(np.frombuffer(data[:even], dtype='<i2').astype(np.float32) / 32768)
                if not segment_total or remaining or response.length not in (None, 0):
                    raise RustBackendError('Invalid Rust PCM stream')
                drained = True
                response.close()
                response = None
                conn.close()
        except GeneratorExit:
            cancelled = True
            raise
        except RustBackendError:
            raise
        except Exception:
            raise RustBackendError('Rust backend stream failed') from None
        finally:
            # Normal barge-in must not kill the warm model. Drain/discard under
            # admission in this same render worker, bounded by the original
            # socket timeout, wall deadline and remaining PCM budget. No new
            # Rust request enters its otherwise unbounded native waiting queue.
            if cancelled and response is not None:
                try:
                    while time.monotonic() <= deadline:
                        if not response.isclosed():
                            transport.settimeout(min(self.timeout, max(.001, deadline - time.monotonic())))
                        data = response.read1(4096)
                        if time.monotonic() > deadline:
                            break
                        if not data:
                            drained = (segment_total > 0 and segment_total % 2 == 0
                                       and response.length in (None, 0))
                            break
                        total += len(data)
                        segment_total += len(data)
                        if total > int(self.max_audio_seconds * self.sample_rate) * 2:
                            break
                except Exception:
                    drained = False
            if response is not None:
                response.close()
            if conn is not None:
                conn.close()
            with self._state_lock:
                self._connection = None
            # The Rust server serializes work but has unbounded waiting threads.
            # After cancellation/early failure, HTTP close alone does not prove
            # native work stopped. Fail closed rather than queue new requests.
            if conn is not None and not drained:
                self.close()
            self._admission.release()

    def close(self):
        """Close transport and terminate/reap only the child this instance owns."""
        with self._state_lock:
            self._closed = True
            conn = self._connection
            child = self._child
            self._child = None
        if conn is not None:
            if conn.sock is not None:
                try: conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            conn.close()
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            else:
                child.wait()
