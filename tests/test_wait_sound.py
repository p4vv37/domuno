"""Waiting feedback with a fake audio player and fake inference transport."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from test_gateway import GATEWAY
import test_metrics as metrics_tests
import qwen_wait_sound as sound


class FakePlayer:
    def __init__(self):
        self.done = threading.Event()
        self.reaped = threading.Event()

    def poll(self):
        return 0 if self.done.is_set() else None

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('fake player', timeout)
        self.reaped.set()
        return 0

    def kill(self):
        self.done.set()


class SoundTests(unittest.TestCase):
    def test_public_audio_preparation_installs_only_missing_enabled_client(self):
        for mode in ('disabled', 'present', 'missing', 'fresh-prepare', 'lva', 'package-failure'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binaries = root / 'bin'
                binaries.mkdir()
                (root / 'run.sh').write_bytes((GATEWAY.parent / 'run.sh').read_bytes())
                (root / '.env').write_text('LLM_WAIT_SOUND=' + ('0' if mode == 'disabled' else '1') +
                                          '\nLLM_WAIT_SOUND_VOLUME=' + ('lva' if mode == 'lva' else '80') + '\n')
                (binaries / 'dirname').symlink_to(shutil.which('dirname'))
                trace = root / 'packages.jsonl'
                manager = binaries / 'apt-get'
                manager.write_text('#!' + sys.executable + '\nimport json, os, pathlib, sys\n'
                    'with open(os.environ["MOCK_APT_TRACE"], "a") as out:\n'
                    '    out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                    'if os.environ["MOCK_APT_FAIL"] == "1": sys.exit(7)\n'
                    'if sys.argv[1] == "install":\n'
                    '    client = pathlib.Path(sys.argv[0]).with_name("paplay")\n'
                    '    client.write_text("#!/bin/sh\\nexit 0\\n")\n'
                    '    client.chmod(0o755)\n')
                manager.chmod(0o755)
                privilege = binaries / 'sudo'
                privilege.write_text('#!/bin/sh\nexec "$@"\n')
                privilege.chmod(0o755)
                if mode in ('present', 'lva'):
                    (binaries / 'paplay').write_text('#!/bin/sh\nexit 0\n')
                    (binaries / 'paplay').chmod(0o755)
                uv_trace = root / 'uv.jsonl'
                if mode == 'lva':
                    uv = binaries / 'uv'
                    uv.write_text('#!' + sys.executable + '\nimport json, os, pathlib, sys\n'
                        'with open(os.environ["MOCK_UV_TRACE"], "a") as out:\n'
                        '    out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                        'if sys.argv[1] == "venv":\n'
                        '    binary = pathlib.Path(sys.argv[-1]) / "bin/python"\n'
                        '    binary.parent.mkdir(parents=True)\n'
                        '    binary.symlink_to(sys.executable)\n'
                        'else:\n'
                        '    assert sys.argv[-1] == "websockets==15.0.1"\n')
                    uv.chmod(0o755)
                if mode == 'fresh-prepare':
                    for name, body in (('uname', 'echo aarch64'), ('geniex', 'exit 0')):
                        executable = binaries / name
                        executable.write_text('#!/bin/sh\n' + body + '\n')
                        executable.chmod(0o755)
                    (binaries / 'mkdir').symlink_to(shutil.which('mkdir'))
                    tokenizer = root / 'assets/qwen-tokenizer'
                    tokenizer.mkdir(parents=True)
                    for name in ('tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json'):
                        (tokenizer / name).write_text('{}')
                    with (root / '.env').open('a') as settings:
                        settings.write('STT_PROVIDER=external\nTTS_PROVIDER=external\n')
                action = 'prepare' if mode == 'fresh-prepare' else 'prepare-audio'
                result = subprocess.run([shutil.which('bash'), str(root / 'run.sh'), action],
                    env={**os.environ, 'PATH': str(binaries), 'MOCK_APT_TRACE': str(trace),
                         'GENIEX_BIN': str(binaries / 'geniex'),
                         'MOCK_UV_TRACE': str(uv_trace),
                         'MOCK_APT_FAIL': '1' if mode == 'package-failure' else '0'},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 7 if mode == 'package-failure' else 0, result.stderr)
                calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                expected = ([['update'], ['install', '-y', '--no-install-recommends', 'pulseaudio-utils']]
                            if mode in ('missing', 'fresh-prepare') else [['update']] if mode == 'package-failure' else [])
                self.assertEqual(calls, expected)
                if mode == 'lva':
                    uv_calls = [json.loads(line) for line in uv_trace.read_text().splitlines()]
                    self.assertEqual(uv_calls[0][0], 'venv')
                    self.assertEqual(uv_calls[1], ['pip', 'install', '--python',
                        str(root / '.venv/bin/python'), 'websockets==15.0.1'])

    def test_disabled_needs_no_audio_dependencies_and_bad_configuration_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sound.shutil, 'which') as lookup:
            self.assertIsNone(sound.from_environment())
            lookup.assert_not_called()
        for env in ({'LLM_WAIT_SOUND': 'yes'},
                    {'LLM_WAIT_SOUND': '1', 'LLM_WAIT_SOUND_VOLUME': '-1'},
                    {'LLM_WAIT_SOUND': '1', 'LLM_WAIT_SOUND_VOLUME': '101'},
                    {'LLM_WAIT_SOUND': '1', 'LLM_WAIT_SOUND_VOLUME': 'loud'}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    sound.from_environment()

    def test_missing_player_or_file_disables_sound_without_starting_a_process(self):
        for player, filename in ((None, ''), ('/fake/paplay', '/nonexistent/wait.wav')):
            with patch.dict(os.environ, {'LLM_WAIT_SOUND': '1', 'LLM_WAIT_SOUND_FILE': filename}, clear=True), \
                    patch.object(sound.shutil, 'which', return_value=player), \
                    patch('sys.stderr', new=io.StringIO()), patch.object(sound.subprocess, 'Popen') as spawn:
                self.assertIsNone(sound.from_environment())
                spawn.assert_not_called()

    def test_default_tone_is_short_and_player_is_nonblocking_without_overlap(self):
        player = FakePlayer()
        adapter = sound.WaitSound('/fake/paplay', volume=25)
        self.addCleanup(adapter.close)
        with patch.object(sound.subprocess, 'Popen', return_value=player) as spawn:
            self.assertTrue(adapter.play())
            self.assertFalse(player.done.is_set())  # play did not wait for the player
            self.assertFalse(adapter.play())
            self.assertEqual(spawn.call_count, 1)
            argv = spawn.call_args.args[0]
            self.assertIn('--volume=16384', argv)
            self.assertNotIn('shell', spawn.call_args.kwargs)
            with wave.open(argv[-1], 'rb') as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth()), (1, 2))
                self.assertLess(audio.getnframes() / audio.getframerate(), .25)
            adapter.close()
            self.assertTrue(player.reaped.is_set())
            self.assertFalse(Path(argv[-1]).exists())
            self.assertFalse(adapter.play())

    def test_custom_path_remains_a_single_argument_and_preserves_pulse_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'wait $(example) with spaces.wav'
            filename.write_bytes(b'fixture')
            with patch.dict(os.environ, {'LLM_WAIT_SOUND': '1', 'LLM_WAIT_SOUND_FILE': str(filename),
                                        'LLM_WAIT_SOUND_VOLUME': '80',
                                        'PULSE_SERVER': 'unix:/test/pulse'}), \
                    patch.object(sound.shutil, 'which', return_value='/fake/paplay'):
                adapter = sound.from_environment()
            self.addCleanup(adapter.close)
            player = FakePlayer()
            with patch.object(sound.subprocess, 'Popen', return_value=player) as spawn:
                self.assertTrue(adapter.play())
                self.assertEqual(spawn.call_args.args[0][-1], str(filename))
                self.assertEqual(spawn.call_args.kwargs['env']['PULSE_SERVER'], 'unix:/test/pulse')

    def test_failed_spawn_is_best_effort_and_hung_player_is_killed_and_reaped(self):
        adapter = sound.WaitSound('/fake/paplay')
        self.addCleanup(adapter.close)
        with patch.object(sound.subprocess, 'Popen', side_effect=OSError), \
                patch('sys.stderr', new=io.StringIO()) as errors:
            self.assertFalse(adapter.play())
            self.assertFalse(adapter.play())
            self.assertEqual(errors.getvalue().count('WARNING:'), 1)
        player = FakePlayer()
        adapter.TIMEOUT = .01
        with patch.object(sound.subprocess, 'Popen', return_value=player):
            self.assertTrue(adapter.play())
            self.assertTrue(player.reaped.wait(1))
            self.assertTrue(player.done.is_set())

    def test_public_sound_test_uses_env_without_starting_models_or_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'gateway').mkdir()
            (root / 'run.sh').write_bytes((GATEWAY.parent / 'run.sh').read_bytes())
            (root / 'gateway/qwen_wait_sound.py').write_bytes((GATEWAY / 'qwen_wait_sound.py').read_bytes())
            (root / '.env').write_text('LLM_WAIT_SOUND=1\nLLM_WAIT_SOUND_VOLUME=10\n')
            player = root / 'paplay'
            trace = root / 'player.json'
            player.write_text('#!' + sys.executable + '\nimport json, os, sys, wave\n'
                'with wave.open(sys.argv[-1]) as audio:\n'
                '    assert audio.getnframes() > 0\n'
                'with open(os.environ["MOCK_SOUND_TRACE"], "w") as out:\n'
                '    json.dump(sys.argv[1:], out)\n')
            player.chmod(0o755)
            result = subprocess.run(['bash', str(root / 'run.sh'), 'sound-test'], cwd=root,
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                     'MOCK_SOUND_TRACE': str(trace)}, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Audio player completed', result.stdout)
            self.assertIn('--volume=6554', json.loads(trace.read_text()))


class GatewaySoundTests(unittest.TestCase):
    def test_llm_json_and_sse_play_once_without_changing_request_or_response(self):
        handler = metrics_tests.GatewayMetricsTests()
        for stream in (False, True):
            def play():
                self.assertEqual(handler.forwarded, [])
                return True
            audio = SimpleNamespace(play=Mock(side_effect=play))
            response = ({'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]}
                        if stream else {'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}]})
            raw = metrics_tests.event(response) + b'data: [DONE]\n\n' if stream else json.dumps(response).encode()
            body = {'messages': [{'role': 'user', 'content': 'question'}], 'stream': stream}
            output, rows = handler.request(response=metrics_tests.FakeResponse([raw]), body=body,
                                           wait_sound=audio, response_style='original')
            audio.play.assert_called_once_with()
            self.assertEqual(output, raw)
            self.assertEqual(handler.forwarded[0]['messages'], body['messages'])
            self.assertTrue(rows[0]['wait_sound_started'])
            self.assertIsNotNone(rows[0]['wait_sound_ms'])

    def test_router_budget_and_queue_rejections_remain_silent(self):
        handler = metrics_tests.GatewayMetricsTests()
        audio = SimpleNamespace(play=Mock())
        for kind in ('ack', 'clarify', 'toolcall'):
            decision = SimpleNamespace(kind=SimpleNamespace(value=kind), response='Done', reason='ambiguous-target',
                                       tool_call=SimpleNamespace(name='fixture', arguments={}))
            handler.request(wait_sound=audio, decide_apply=True, decision_fn=lambda _: decision)
        handler.request(wait_sound=audio, prompt_counter=lambda _: 99999)
        gate = SimpleNamespace(acquire=lambda **_: False)
        with patch.object(metrics_tests.gateway.threading, 'BoundedSemaphore', return_value=gate):
            handler.request(wait_sound=audio)
        audio.play.assert_not_called()

    def test_sound_failure_does_not_prevent_or_retry_inference(self):
        handler = metrics_tests.GatewayMetricsTests()
        audio = SimpleNamespace(play=Mock(side_effect=OSError('private audio failure')))
        with patch('sys.stderr', new=io.StringIO()) as errors:
            _, rows = handler.request(wait_sound=audio)
        self.assertEqual(len(handler.forwarded), 1)
        self.assertEqual(rows[0]['outcome'], 'completed')
        self.assertFalse(rows[0]['wait_sound_started'])
        self.assertNotIn('private audio failure', errors.getvalue())

    def test_legacy_reset_is_not_announced_as_an_extra_request(self):
        handler = metrics_tests.GatewayMetricsTests()
        audio = SimpleNamespace(play=Mock(return_value=True))
        handler.request(wait_sound=audio, cache_policy='reset')
        self.assertEqual(len(handler.forwarded), 2)
        audio.play.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
