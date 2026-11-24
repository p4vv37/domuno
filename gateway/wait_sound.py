"""Optional one-shot PulseAudio feedback. No HA tools, TTS or language logic."""
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave


def _write_tone(path):
    """A quiet 180 ms two-note cue, generated once without downloaded assets."""
    rate = 16000
    samples = []
    for index in range(int(rate * .18)):
        seconds = index / rate
        note_time = seconds if seconds < .07 else seconds - .11
        if .07 <= seconds < .11:
            value = 0
        else:
            envelope = max(0, min(1, note_time / .01, (.07 - note_time) / .01))
            frequency = 440 if seconds < .07 else 554.37
            value = int(10000 * envelope * math.sin(2 * math.pi * frequency * note_time))
        samples.append(value)
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(struct.pack('<' + 'h' * len(samples), *samples))


class WaitSound:
    """Start without waiting for playback; bound overlap and child lifetime."""
    TIMEOUT = 3

    def __init__(self, player, filename='', volume=80, lva_volume=None):
        self._volume = volume
        self._lva_volume = lva_volume
        self._temporary = None
        if not filename:
            self._temporary = tempfile.TemporaryDirectory(prefix='ha-wait-sound-')
            filename = str(Path(self._temporary.name) / 'wait.wav')
            _write_tone(filename)
        self._argv = [player, '--client-name=domuno', '--stream-name=LLM waiting',
                      filename]
        self._environment = dict(os.environ)
        self._environment.setdefault('PULSE_SERVER', f'unix:/run/user/{os.getuid()}/pulse/native')
        self._lock = threading.Lock()
        self._process = None
        self._closed = False
        self._warned = False

    def _warn(self, message='LLM waiting sound unavailable; inference is unchanged'):
        if not self._warned:
            self._warned = True
            print('WARNING: ' + message, file=sys.stderr)

    @property
    def lva_volume(self):
        return self._lva_volume

    def _level(self):
        """Normalized 0-1 level, or None while LVA volume is unavailable."""
        if self._lva_volume is None:
            return self._volume / 100
        # LVA reports whole percentages to mpv. PulseAudio and mpv already use
        # cubic software scales, so map the value once, without cubing it again.
        level = self._lva_volume.get()
        return None if level is None else round(level * 100) / 100

    def play(self):
        """True means a player was launched, not that sound was heard."""
        with self._lock:
            if self._closed or self._process is not None and self._process.poll() is None:
                return False
            level = self._level()
            if level is None:
                self._warn('LVA output volume unavailable; waiting cue skipped')
                return False
            if level == 0:
                return False
            argv = self._argv[:-1] + ['--volume=' + str(round(level * 65536)), self._argv[-1]]
            try:
                process = subprocess.Popen(argv, env=self._environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            except OSError:
                self._warn()
                return False
            self._process = process
            threading.Thread(target=self._reap, args=(process,), daemon=True).start()
            return True

    def _reap(self, process):
        try:
            if process.wait(timeout=self.TIMEOUT) != 0:
                self._warn()
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            self._warn()

    def close(self):
        with self._lock:
            self._closed = True
            if self._process is not None and self._process.poll() is None:
                self._process.kill()
                self._process.wait()
            if self._temporary is not None:
                self._temporary.cleanup()
        if self._lva_volume is not None:
            self._lva_volume.close()


def from_environment():
    """Disabled by default; missing optional audio support never blocks startup."""
    enabled = os.environ.get('LLM_WAIT_SOUND', '0')
    if enabled not in ('0', '1'):
        raise ValueError('LLM_WAIT_SOUND must be 0 or 1')
    if enabled == '0':
        return None
    setting = os.environ.get('LLM_WAIT_SOUND_VOLUME', 'lva')
    try:
        volume = 80 if setting == 'lva' else int(setting)
    except ValueError:
        raise ValueError('LLM_WAIT_SOUND_VOLUME must be lva or an integer from 0 to 100') from None
    if not 0 <= volume <= 100:
        raise ValueError('LLM_WAIT_SOUND_VOLUME must be lva or an integer from 0 to 100')
    if volume == 0:
        return None
    player = shutil.which('paplay')
    filename = os.environ.get('LLM_WAIT_SOUND_FILE', '')
    if player is None or filename and (not Path(filename).is_absolute() or not Path(filename).is_file()):
        print('WARNING: LLM waiting sound disabled: check paplay and LLM_WAIT_SOUND_FILE', file=sys.stderr)
        return None
    source = None
    if setting == 'lva':
        try:
            from websockets.sync.client import connect
        except ImportError:
            print('WARNING: LVA volume sync unavailable; run ./run.sh prepare-audio', file=sys.stderr)
            return None
        from lva_volume import LvaVolume
        source = LvaVolume(os.environ.get('LVA_VOLUME_URL', 'ws://127.0.0.1:6055'), connect)
        # One bounded startup wait; play() never performs network I/O or waits.
        source.ready.wait(timeout=1)
    try:
        return WaitSound(player, filename, volume, lva_volume=source)
    except OSError:
        if source is not None:
            source.close()
        print('WARNING: LLM waiting sound disabled: audio initialization failed', file=sys.stderr)
        return None


if __name__ == '__main__':
    # Standalone check using the same configuration and adapter as the gateway.
    try:
        adapter = from_environment()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if adapter is None:
        raise SystemExit('Waiting sound disabled or unavailable. Check LLM_WAIT_SOUND, paplay and the audio file.')
    try:
        if adapter.lva_volume is not None:
            current = adapter.lva_volume.get()
            if current is None:
                raise SystemExit('LVA volume unavailable. Check LVA_VOLUME_URL and the LVA peripheral WebSocket.')
            if round(current * 100) == 0:
                raise SystemExit('LVA output is muted or at zero volume. Waiting cue suppressed.')
        if not adapter.play() or adapter._process.wait(timeout=adapter.TIMEOUT) != 0:
            raise SystemExit('Audio playback failed. Check the user PulseAudio/PipeWire connection.')
        print('Audio player completed. Check that the cue was audible.')
    except subprocess.TimeoutExpired:
        raise SystemExit('Audio playback timed out.') from None
    finally:
        adapter.close()
