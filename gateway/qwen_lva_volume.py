"""Read-only LVA output-volume subscription. Network work stays off the request path."""
import json
import math
import threading
from urllib.parse import urlsplit


class LvaVolume:
    """Cache the current output volume, or None while disconnected."""
    RETRY_SECONDS = 1

    def __init__(self, url, connect):
        endpoint = urlsplit(url)
        if endpoint.scheme not in ('ws', 'wss') or not endpoint.hostname:
            raise ValueError('LVA_VOLUME_URL must be a ws:// or wss:// endpoint')
        self._url, self._connect = url, connect
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.ready = threading.Event()
        self._volume = None
        self._muted = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def get(self):
        with self._lock:
            if self._volume is None:
                return None
            return 0.0 if self._muted else self._volume

    def _accept(self, raw):
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict) or not isinstance(event.get('data'), dict):
            return
        kind, data = event.get('event'), event['data']
        with self._lock:
            if kind in ('snapshot', 'volume_changed'):
                value = data.get('volume')
                if (type(value) not in (int, float) or not math.isfinite(value)
                        or not 0 <= value <= 1):
                    return
                self._volume = value
                if kind == 'snapshot':
                    self._muted = data.get('volume_muted') is True
                self.ready.set()
            elif kind == 'volume_muted' and type(data.get('muted')) is bool:
                self._muted = data['muted']
            # Microphone mute and conversation text are unrelated to output volume.

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._connect(self._url, open_timeout=1, close_timeout=.2,
                                   max_size=65536, max_queue=4, proxy=None) as connection:
                    while not self._stop.is_set():
                        try:
                            self._accept(connection.recv(timeout=.25))
                        except TimeoutError:
                            continue
            except Exception:
                pass  # Audio feedback must never interfere with inference or log payloads.
            finally:
                with self._lock:
                    self._volume, self._muted = None, False
                    self.ready.clear()
            self._stop.wait(self.RETRY_SECONDS)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
