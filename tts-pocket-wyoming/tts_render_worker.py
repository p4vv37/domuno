"""Bounded single-model worker; contains no model or protocol dependencies."""
from concurrent.futures import Future
import queue
import threading
import json
import logging
import time
import uuid
import asyncio
from datetime import datetime, timezone


class StreamCancelled(Exception):
    """Consumer disconnected; stop between native generator steps."""


class PCMStream:
    """Bounded thread-to-async PCM bridge; no executor tasks wait on a queue."""
    def __init__(self, loop, slots=8):
        self.loop = loop
        self.queue = queue.Queue(maxsize=slots)
        self.stopped = threading.Event()
        self.wake = asyncio.Event()

    def notify(self, _=None):
        try:
            self.loop.call_soon_threadsafe(self.wake.set)
        except RuntimeError:
            self.stopped.set()

    def send(self, data):
        for offset in range(0, len(data), 2048):
            while not self.stopped.is_set():
                try:
                    self.queue.put(data[offset:offset+2048], timeout=.05)
                    self.notify()
                    break
                except queue.Full:
                    continue
            else:
                raise StreamCancelled()

    async def read(self, job):
        while True:
            self.wake.clear()
            try:
                return self.queue.get_nowait()
            except queue.Empty:
                if job.done():
                    job.result()
                    return None
                await self.wake.wait()

    def cancel(self):
        self.stopped.set()
        self.notify()


class TextStream:
    """Bounded sentence queue owned by one render job for the whole utterance."""
    def __init__(self, slots=32, idle_timeout=30):
        self.queue = queue.Queue(maxsize=slots)
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.idle_timeout = idle_timeout

    def send(self, text):
        if self.stopped.is_set() or self.finished.is_set():
            raise StreamCancelled()
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            raise RenderBusy('TTS sentence queue is full') from None

    def finish(self):
        self.finished.set()

    def cancel(self):
        self.stopped.set()

    def __iter__(self):
        deadline = time.monotonic() + self.idle_timeout
        while True:
            if self.stopped.is_set():
                raise StreamCancelled()
            if self.finished.is_set() and self.queue.empty():
                return
            try:
                text = self.queue.get(timeout=.05)
            except queue.Empty:
                if self.finished.is_set():
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError('TTS text stream stalled')
                continue
            yield text
            deadline = time.monotonic() + self.idle_timeout


class RequestMetrics:
    """Payload-free service boundaries; monotonic offsets, never playback timing.

    Each event is emitted independently so a cancelled client's running render
    can still report completion. The random id carries no caller identity.
    """

    def __init__(self, clock=time.monotonic_ns, emit=None):
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.request_id = uuid.uuid4().hex
        self._clock = clock
        self._origin = clock()
        self._emit = emit or logging.getLogger(__name__).info
        self._lock = threading.Lock()
        self._seen = set()
        self._render_start = None

    def mark(self, event):
        with self._lock:
            if event in self._seen:
                return
            now = self._clock()
            self._seen.add(event)
            if event == 'render_start':
                self._render_start = now
            record = {'schema_version': 1, 'service': 'tts', 'timestamp': self.timestamp,
                      'request_id': self.request_id, 'event': event,
                      'elapsed_ms': (now - self._origin) / 1_000_000}
            if self._render_start is not None:
                record['render_elapsed_ms'] = (now - self._render_start) / 1_000_000
            try:
                self._emit('pocket_tts_metric ' + json.dumps(record, separators=(',', ':')))
            except Exception:
                # Telemetry is best effort: never fail synthesis/audio delivery or
                # echo an emitter exception that could contain private payloads.
                # Deliberately do not swallow BaseException (e.g. interrupts).
                pass


class RenderBusy(Exception):
    """The engine already has its maximum number of admitted jobs."""


class RenderWorker:
    """One engine thread, at most one pending job, no event-loop lock waits.

    Cancellation can skip a pending Future. Once running, cancellation cannot
    release capacity or allow another render to enter the engine prematurely.
    Callables must fully consume/dispose lazy model output before returning.
    """

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)
        self._capacity = threading.BoundedSemaphore(2)
        self._gate = threading.Lock()
        self._engine_lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name='pocket-render', daemon=True)
        self._thread.start()

    def submit(self, render, *args):
        with self._gate:
            if self._closed.is_set():
                raise RenderBusy('TTS engine is shutting down')
            if not self._capacity.acquire(blocking=False):
                raise RenderBusy('TTS engine is busy')
            future = Future()
            try:
                self._queue.put_nowait((future, render, args))
            except queue.Full:
                self._capacity.release()
                raise RenderBusy('TTS engine is busy') from None
            return future

    def _run(self):
        while True:
            try:
                future, render, args = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        with self._engine_lock:
                            result = render(*args)
                    except BaseException as err:
                        future.set_exception(err)
                    else:
                        future.set_result(result)
            finally:
                self._capacity.release()
                self._queue.task_done()
                # Do not retain a disconnected client's PCM or request while idle.
                del future, render, args
                if 'result' in locals():
                    del result

    def close(self, wait=True):
        """Reject new jobs, cancel queued jobs, and finish any running render.

        wait=True must be called outside the event-loop thread.
        """
        with self._gate:
            self._closed.set()
            while True:
                try:
                    future, _, _ = self._queue.get_nowait()
                except queue.Empty:
                    break
                future.cancel()
                self._capacity.release()
                self._queue.task_done()
        if wait:
            self._thread.join()
