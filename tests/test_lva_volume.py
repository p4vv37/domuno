"""LVA output-volume events through the subscription interface, no audio/network."""
import io
import json
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_gateway import GATEWAY
from qwen_lva_volume import LvaVolume
import qwen_wait_sound as sound
from test_wait_sound import FakePlayer


class Socket:
    def __init__(self):
        self.messages = queue.Queue()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def recv(self, timeout):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if message is None:
            raise OSError('disconnected')
        return message

    def send_event(self, kind, **data):
        self.messages.put(json.dumps({'event': kind, 'data': data}))

    def close(self):
        self.messages.put(None)


class VolumeTests(unittest.TestCase):
    def assert_volume(self, source, expected):
        deadline = time.monotonic() + 1
        while source.get() != expected and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.assertEqual(source.get(), expected)

    def test_snapshot_live_changes_output_mute_and_ignored_microphone_mute(self):
        socket = Socket()
        connect = Mock(return_value=socket)
        source = LvaVolume('ws://127.0.0.1:6055', connect)
        self.addCleanup(source.close)
        self.assertIsNone(source.get())
        socket.send_event('snapshot', volume=.8, volume_muted=False, muted=True,
                          last_stt_text='PRIVATE', last_tts_text='PRIVATE')
        self.assertTrue(source.ready.wait(1))
        self.assert_volume(source, .8)
        socket.send_event('volume_changed', volume=.4)
        self.assert_volume(source, .4)
        socket.send_event('volume_muted', muted=True)
        self.assert_volume(source, 0)
        socket.send_event('volume_muted', muted=False)
        self.assert_volume(source, .4)
        socket.send_event('volume_changed', volume=0)
        self.assert_volume(source, 0)
        self.assertIsNone(connect.call_args.kwargs['proxy'])
        self.assertLessEqual(connect.call_args.kwargs['max_size'], 65536)

    def test_invalid_volume_is_ignored_and_disconnect_invalidates_cached_state(self):
        socket = Socket()
        allow = threading.Event()
        def connect(*_, **__):
            if allow.is_set():
                raise OSError('offline')
            allow.set()
            return socket
        source = LvaVolume('ws://127.0.0.1:6055', connect)
        self.addCleanup(source.close)
        socket.send_event('snapshot', volume=.6)
        self.assertTrue(source.ready.wait(1))
        for value in (True, '0.9', -1, 2, float('nan'), float('inf')):
            socket.send_event('volume_changed', volume=value)
        socket.messages.put('not json')
        socket.send_event('muted', muted=True)  # This is microphone mute.
        socket.send_event('volume_changed', volume=.7)
        self.assert_volume(source, .7)
        socket.close()
        self.assert_volume(source, None)
        source.close()
        self.assertFalse(source._thread.is_alive())

    def test_reconnect_takes_a_new_snapshot_and_closes_cleanly(self):
        first, second = Socket(), Socket()
        connects = Mock(side_effect=[first, second, OSError('offline')])
        retry = patch.object(LvaVolume, 'RETRY_SECONDS', .01)
        retry.start()
        self.addCleanup(retry.stop)
        source = LvaVolume('ws://127.0.0.1:6055', connects)
        self.addCleanup(source.close)
        first.send_event('snapshot', volume=.3)
        self.assertTrue(source.ready.wait(1))
        first.close()
        second.send_event('snapshot', volume=.9)
        self.assert_volume(source, .9)
        source.close()
        self.assertFalse(source._thread.is_alive())

    def test_audio_uses_cached_level_with_no_connection_or_wait_on_play_path(self):
        source = SimpleNamespace(get=Mock(return_value=.8), close=Mock())
        adapter = sound.WaitSound('/fake/paplay', lva_volume=source)
        self.addCleanup(adapter.close)
        player = FakePlayer()
        with patch.object(sound.subprocess, 'Popen', return_value=player) as spawn:
            self.assertTrue(adapter.play())
            self.assertIn('--volume=52429', spawn.call_args.args[0])
            source.get.assert_called_once_with()
        adapter.close()
        source.close.assert_called()

    def test_unknown_or_muted_lva_is_silent_without_a_fixed_volume_fallback(self):
        for value in (None, 0):
            with self.subTest(value=value):
                source = SimpleNamespace(get=lambda: value, close=Mock())
                adapter = sound.WaitSound('/fake/paplay', volume=100, lva_volume=source)
                try:
                    with patch.object(sound.subprocess, 'Popen') as spawn, patch('sys.stderr', new=io.StringIO()):
                        self.assertFalse(adapter.play())
                        spawn.assert_not_called()
                finally:
                    adapter.close()

    def test_invalid_endpoint_is_rejected_before_connecting(self):
        for url in ('http://127.0.0.1:6055', 'ws://', 'not-a-url'):
            connector = Mock()
            with self.subTest(url=url), self.assertRaises(ValueError):
                LvaVolume(url, connector)
            connector.assert_not_called()

    def test_environment_selects_lva_subscription_with_one_bounded_startup_wait(self):
        source = SimpleNamespace(get=lambda: .75, close=Mock(), ready=SimpleNamespace(wait=Mock()))
        connect = Mock()
        env = {'LLM_WAIT_SOUND': '1', 'LLM_WAIT_SOUND_VOLUME': 'lva',
               'LLM_WAIT_SOUND_FILE': '', 'LVA_VOLUME_URL': 'ws://127.0.0.1:6060'}
        with patch.dict(sound.os.environ, env), \
                patch.dict(sys.modules, {'websockets.sync.client': SimpleNamespace(connect=connect)}), \
                patch('qwen_lva_volume.LvaVolume', return_value=source) as factory, \
                patch.object(sound.shutil, 'which', return_value='/fake/paplay'):
            adapter = sound.from_environment()
        try:
            factory.assert_called_once_with('ws://127.0.0.1:6060', connect)
            source.ready.wait.assert_called_once_with(timeout=1)
        finally:
            adapter.close()
        source.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
