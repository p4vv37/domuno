"""Fresh-clone regressions. No actual installs, services, network or models."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard_mod = load('boot_guard', 'llm/backend.py')
assets = load('tts_assets', 'tts-pocket-wyoming/prepare_assets.py')


class GuardTests(unittest.TestCase):
    def run_guard(self, state, *, containers='', docker_ok=True, backend_running=False):
        def command(argv, **kwargs):
            if argv[0] == 'pgrep':
                return SimpleNamespace(returncode=0 if backend_running else 1)
            self.assertEqual(argv[:3], ['docker','container','ls'])
            return SimpleNamespace(returncode=0 if docker_ok else 1, stdout=containers)
        with patch.object(guard_mod.subprocess, 'run', side_effect=command), patch.object(guard_mod.time, 'sleep'):
            guard_mod.guard(boot_id='test-boot',state_dir=state)

    def test_fresh_board_needs_no_legacy_containers_and_allows_one_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state'
            self.run_guard(state,containers='unrelated-cpu-service\n')
            self.assertTrue((state/'attempt-test-boot').is_file())
            with self.assertRaises(FileExistsError):
                self.run_guard(state)

    def test_running_stt_backup_or_backend_blocks_before_lock(self):
        for options in ({'containers':'whisper-turbo-qnn'},
                        {'containers':'whisper-medium-qnn-backup'},
                        {'backend_running':True}, {'docker_ok':False}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                state=Path(tmp)/'state'
                with self.assertRaises(RuntimeError):
                    self.run_guard(state,**options)
                self.assertFalse(state.exists())


class VersionTests(unittest.TestCase):
    def test_version_policy_compatibility_without_model_calls(self):
        for version, policy, accepted in (('0.11.0', 'reuse', True), ('0.11.0', 'reset', True),
                                          ('0.9.3', 'reuse', False), ('0.9.3', 'reset', True),
                                          ('0.10.0', 'reuse', False), ('unknown', 'reuse', False)):
            with self.subTest(version=version, policy=policy):
                result = SimpleNamespace(stdout='GenieX CLI Version:     v'+version+'\n')
                with patch.object(guard_mod.subprocess, 'run', return_value=result) as command:
                    if accepted:
                        self.assertEqual(guard_mod.check_version('/test/geniex', policy), version)
                    else:
                        with self.assertRaises(RuntimeError):
                            guard_mod.check_version('/test/geniex', policy)
                    self.assertEqual(command.call_args.args[0], ['/test/geniex', '--skip-update', '--version'])
                    self.assertEqual(command.call_args.kwargs['timeout'], 20)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ha-install-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.deploy=self.root/'path with spaces'
        self.deploy.mkdir()
        shutil.copy(ROOT/'setup.sh', self.deploy/'setup.sh')
        shutil.copytree(ROOT/'gateway/languages', self.deploy/'gateway/languages')
        (self.deploy/'llm').mkdir()
        shutil.copy(ROOT/'llm/backend.py', self.deploy/'llm/backend.py')
        fake=self.root/'bin'
        fake.mkdir()
        uv=fake/'uv'
        uv.write_text('#!'+sys.executable+'''\nimport json, os, pathlib, sys
with open(os.environ['MOCK_UV_CALLS'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1] == 'venv':
    target=pathlib.Path('.venv/bin'); target.mkdir(parents=True)
    (target/'python').symlink_to(sys.executable)
''')
        systemctl=fake/'systemctl'
        systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOCK_SYSTEMCTL_CALLS"\n')
        sleep=fake/'sleep'
        sleep.write_text('#!/bin/sh\nexit 0\n')
        for path in (uv,systemctl,sleep): path.chmod(0o755)
        self.env={k:v for k,v in os.environ.items() if k not in (
            'GGUF','TOKENIZER','COUNTER_URL','NCTX','DECIDE_APPLY','GATEWAY_PORT','COUNTER_PORT','CACHE_POLICY','RESPONSE_STYLE','ROUTER_MISS_LOGGING','AGENT_LANGUAGE','STT_PROVIDER','TTS_PROVIDER',
            'LLM_WAIT_SOUND','LLM_WAIT_SOUND_FILE','LLM_WAIT_SOUND_VOLUME')}
        self.env.update(PATH=str(fake)+':'+os.environ['PATH'],
                        XDG_CONFIG_HOME=str(self.root/'config'),
                        MOCK_UV_CALLS=str(self.root/'uv-calls'),
                        MOCK_SYSTEMCTL_CALLS=str(self.root/'systemctl-calls'))

    def install(self, **config):
        return subprocess.run(['bash',str(self.deploy/'setup.sh')], env={**self.env,**config},
                              cwd=self.deploy, capture_output=True, text=True)

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    def test_lva_volume_dependency_is_installed_inline(self):
        result = self.install(LLM_WAIT_SOUND='1', LLM_WAIT_SOUND_VOLUME='lva',
                              COUNTER_URL='http://127.0.0.1:18193/count')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'uv-calls').read_text().splitlines()]
        self.assertIn(['pip', 'install', '--python', str(self.deploy / '.venv/bin/python'),
                       'websockets==15.0.1'], calls)

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    def test_english_gateway_does_not_install_polish_dictionaries(self):
        result = self.install(AGENT_LANGUAGE='en', COUNTER_URL='http://127.0.0.1:18193/count')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root/'uv-calls').read_text()
        self.assertIn('simplemma==2.0.0', calls)
        self.assertNotIn('polish-inflection', calls)
        unit = (self.root/'config/systemd/user/qwen-ha-gateway.service').read_text()
        self.assertIn('Environment=AGENT_LANGUAGE=en', unit)

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    def test_external_stt_is_not_installed_started_or_restarted(self):
        binary = self.root/'geniex'
        binary.write_text('#!/bin/sh\necho "GenieX CLI Version: v0.11.0"\n')
        binary.chmod(0o755)
        result = self.install(FULL_STACK='1', GENIEX_BIN=str(binary), AGENT_LANGUAGE='en',
                              STT_PROVIDER='external', TTS_PROVIDER='external',
                              COUNTER_URL='http://127.0.0.1:18193/count')
        self.assertEqual(result.returncode, 0, result.stderr)
        units = self.root/'config/systemd/user'
        self.assertTrue((units/'qwen25-3b-backend.service').exists())
        self.assertFalse((units/'qwen25-whisper-npu.service').exists())
        self.assertNotIn('whisper', (self.root/'systemctl-calls').read_text())

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    def test_invalid_language_or_provider_stops_before_dependency_install(self):
        for config in ({'AGENT_LANGUAGE': 'xx'}, {'STT_PROVIDER': 'unknown'},
                       {'TTS_PROVIDER': 'unknown'}, {'FULL_STACK': '1', 'AGENT_LANGUAGE': 'en'}):
            with self.subTest(config=config):
                result = self.install(**config)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root/'uv-calls').exists())

    def test_public_prepare_skips_external_speech_assets(self):
        shutil.copy(ROOT/'run.sh', self.deploy/'run.sh')
        (self.deploy/'.env').write_text('AGENT_LANGUAGE=en\nSTT_PROVIDER=external\nTTS_PROVIDER=external\n')
        tokenizer = self.deploy/'assets/qwen-tokenizer'
        tokenizer.mkdir(parents=True)
        for name in ('tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json'):
            (tokenizer/name).write_text('{}')
        binary = self.root/'bin/geniex'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        machine = self.root/'bin/uname'
        machine.write_text('#!/bin/sh\necho aarch64\n')
        machine.chmod(0o755)
        result = subprocess.run(['bash', str(self.deploy/'run.sh'), 'prepare'],
            env={**self.env, 'GENIEX_BIN': str(binary)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.deploy/'stt').exists())
        self.assertFalse((self.deploy/'tts-pocket-wyoming').exists())

    def test_public_start_and_stop_leave_external_speech_untouched(self):
        script = (ROOT/'run.sh').read_text()
        # Model/device preflight is tested separately. Exercise the real public
        # lifecycle selection with fake Docker/systemd and no model installation.
        preflight = '"$ROOT/run.sh" check || fail \'First resolve the items marked MISSING.\''
        self.assertEqual(script.count(preflight), 1)
        (self.deploy/'run.sh').write_text(script.replace(preflight, 'true'))
        (self.deploy/'.env').write_text('AGENT_LANGUAGE=en\nSTT_PROVIDER=external\nTTS_PROVIDER=external\n')
        (self.deploy/'setup.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.deploy/'setup.sh').chmod(0o755)
        docker = self.root/'bin/docker'
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOCK_DOCKER_CALLS"\n')
        docker.chmod(0o755)
        env = {**self.env, 'MOCK_DOCKER_CALLS': str(self.root/'docker-calls')}
        for action in ('start', 'stop'):
            result = subprocess.run(['bash', str(self.deploy/'run.sh'), action],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root/'docker-calls').read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('/lva/docker-compose.yml' in call for call in calls))
        self.assertNotIn('whisper', (self.root/'systemctl-calls').read_text())

    @unittest.skipIf(os.geteuid()==0,'installer intentionally refuses root')
    def test_uv_installs_into_quoted_target_and_rerun_keeps_venv(self):
        for _ in range(2):
            result=self.install(DECIDE_APPLY='0', COUNTER_URL='http://127.0.0.1:18193/count')
            self.assertEqual(result.returncode,0,result.stderr)
        calls=[json.loads(line) for line in (self.root/'uv-calls').read_text().splitlines()]
        self.assertEqual(sum(call[0]=='venv' for call in calls),1)
        pip=[call for call in calls if call[:2]==['pip','install']]
        # Two runs, each installs simplemma and the optional polish-inflection.
        self.assertEqual(len(pip),4)
        self.assertTrue(any('polish-inflection==0.7.3' in call for call in pip))
        self.assertIn(str(self.deploy/'.venv/bin/python'),pip[0])
        unit=(self.root/'config/systemd/user/qwen-ha-gateway.service').read_text()
        self.assertNotIn('--decide-apply',unit)
        self.assertIn('"'+str(self.deploy/'.venv/bin/python')+'"',unit)
        self.assertIn('--nctx 10240',unit)
        self.assertIn('restart qwen-ha-gateway.service',(self.root/'systemctl-calls').read_text())

    @unittest.skipIf(os.geteuid()==0,'installer intentionally refuses root')
    def test_prompt_counter_is_required_before_any_install(self):
        result=self.install()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('A prompt counter is required',result.stderr)
        self.assertFalse((self.root/'uv-calls').exists())

    @unittest.skipIf(os.geteuid()==0,'installer intentionally refuses root')
    def test_large_context_generates_local_counter_and_installs_its_dependency(self):
        gguf=self.root/'model.gguf'; gguf.touch()
        tokenizer=self.root/'tokenizer'; tokenizer.mkdir()
        (tokenizer/'tokenizer.json').write_text('{}')
        result=self.install(NCTX='10240',GGUF=str(gguf),TOKENIZER=str(tokenizer))
        self.assertEqual(result.returncode,0,result.stderr)
        units=self.root/'config/systemd/user'
        self.assertIn('--gguf "'+str(gguf)+'"',(units/'qwen-ha-counter.service').read_text())
        self.assertIn('--prompt-counter-url',(units/'qwen-ha-gateway.service').read_text())
        self.assertIn('transformers==4.57.1',(self.root/'uv-calls').read_text())

    @unittest.skipIf(os.geteuid()==0,'installer intentionally refuses root')
    def test_full_stack_generates_paths_and_starts_llm_before_stt(self):
        binary=self.root/'geniex'
        binary.write_text('#!/bin/sh\necho \"GenieX CLI Version:     v0.11.0\"\n');binary.chmod(0o755)
        result=self.install(FULL_STACK='1',GENIEX_BIN=str(binary),
                              COUNTER_URL='http://127.0.0.1:18193/count')
        self.assertEqual(result.returncode,0,result.stderr)
        units=self.root/'config/systemd/user'
        backend=(units/'qwen25-3b-backend.service').read_text()
        self.assertIn(str(self.deploy/'llm/backend.py'),backend)
        self.assertIn('--nctx 10240',backend)
        gateway_unit=(units/'qwen-ha-gateway.service').read_text()
        self.assertIn('--cache-policy reuse', gateway_unit)
        self.assertIn('--think=false --metrics', backend)
        self.assertIn('LimitCORE=0', backend)
        self.assertIn('--metrics-dir', gateway_unit)
        self.assertIn(str(self.deploy/'logs/metrics'), gateway_unit)
        self.assertNotIn('%h/domuno',backend)
        calls=(self.root/'systemctl-calls').read_text()
        self.assertLess(calls.index('start qwen25-3b-backend.service'),
                        calls.index('start qwen25-whisper-npu.service'))
        self.assertNotIn('restart qwen25-3b-backend',calls)

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    def test_gateway_only_migration_leaves_backend_and_external_counter_running(self):
        result = self.install(FULL_STACK='0', NCTX='10240', GGUF='', TOKENIZER='',
                              COUNTER_URL='http://127.0.0.1:18193/count', CACHE_POLICY='reuse')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root/'systemctl-calls').read_text()
        self.assertIn('restart qwen-ha-gateway.service', calls)
        self.assertNotIn('qwen25-3b-backend', calls)
        self.assertNotIn('whisper', calls)
        self.assertNotIn('qwen-ha-counter', calls)
        self.assertNotIn('transformers', (self.root/'uv-calls').read_text())
        unit = (self.root/'config/systemd/user/qwen-ha-gateway.service').read_text()
        self.assertIn('--cache-policy reuse', unit)
        self.assertIn('--prompt-counter-url "http://127.0.0.1:18193/count"', unit)
        # systemd path settings reject quotes; only ExecStart supports them.
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertNotIn('EnvironmentFile="', unit)
        self.assertIn('WorkingDirectory=' + str(self.deploy), unit)
        self.assertIn('--router-miss-log-dir', unit)
        self.assertNotIn('--router-miss-until', unit)
        self.assertIn('Environment=ROUTER_MISS_LOGGING=0', unit)

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    def test_legacy_requires_explicit_reset_before_installing_dependencies(self):
        binary = self.root/'geniex'
        binary.write_text('#!/bin/sh\necho "GenieX CLI Version:     v0.9.3"\n')
        binary.chmod(0o755)
        result = self.install(FULL_STACK='1', GENIEX_BIN=str(binary),
                              COUNTER_URL='http://127.0.0.1:18193/count')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'uv-calls').exists())
        result = self.install(FULL_STACK='1', GENIEX_BIN=str(binary), CACHE_POLICY='reset',
                              COUNTER_URL='http://127.0.0.1:18193/count')
        self.assertEqual(result.returncode, 0, result.stderr)
        units = self.root/'config/systemd/user'
        self.assertIn('--cache-policy reset', (units/'qwen-ha-gateway.service').read_text())
        self.assertNotIn('--think=', (units/'qwen25-3b-backend.service').read_text())

    @unittest.skipIf(os.geteuid()==0, 'installer intentionally refuses root')
    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
    def test_generated_units_pass_real_systemd_parser_in_paths_with_spaces(self):
        runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
        if not runtime_dir or not (Path(runtime_dir) / 'bus').exists():
            self.skipTest('no user systemd bus')
        if subprocess.run(['systemctl', '--user', 'show-environment'],
                          capture_output=True).returncode != 0:
            self.skipTest('no user systemd session')
        gguf = self.root/'model.gguf'
        gguf.touch()
        tokenizer = self.root/'tokenizer'
        tokenizer.mkdir()
        (tokenizer/'tokenizer.json').write_text('{}')
        result = self.install(NCTX='10240', GGUF=str(gguf), TOKENIZER=str(tokenizer))
        self.assertEqual(result.returncode, 0, result.stderr)
        units = self.root/'config/systemd/user'
        paths = [units/'qwen-ha-gateway.service', units/'qwen-ha-counter.service']
        command = [shutil.which('systemd-analyze'), '--user', 'verify']
        parsed = subprocess.run(command + [str(path) for path in paths], capture_output=True, text=True)
        if 'SO_PASSRIGHTS' in parsed.stderr or 'SO_PASSCRED' in parsed.stderr:
            self.skipTest('Sandbox blocks systemd parser socket setup')
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        content = paths[0].read_text()
        self.assertIn('Type=notify', content)
        self.assertIn('--router-miss-log-dir', content)
        self.assertNotIn('--router-miss-until', content)
        paths[0].write_text(content.replace('WorkingDirectory='+str(self.deploy),
                                          'WorkingDirectory="'+str(self.deploy)+'"'))
        broken = subprocess.run(command + [str(paths[0])], capture_output=True, text=True)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn('WorkingDirectory= path is not absolute', broken.stderr)

    def test_prepare_does_not_populate_an_explicit_tokenizer_directory(self):
        shutil.copy(ROOT/'run.sh', self.deploy/'run.sh')
        fake = self.root/'bin'
        for name, script in {'uname': 'echo aarch64', 'geniex': 'exit 0', 'curl': 'exit 99'}.items():
            command = fake/name
            command.write_text('#!/bin/sh\n'+script+'\n')
            command.chmod(0o755)
        tokenizer = self.root/'custom tokenizer'
        tokenizer.mkdir()
        (self.deploy/'.env').write_text(f'TOKENIZER="{tokenizer}"\nSTT_PROVIDER=external\nTTS_PROVIDER=external\n')
        env = {**self.env, 'GENIEX_BIN': str(fake/'geniex')}
        command = ['bash', str(self.deploy/'run.sh'), 'prepare']
        missing = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn('configured TOKENIZER directory', missing.stderr)
        (tokenizer/'tokenizer.json').write_text('{}')
        configured = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertEqual(list(tokenizer.iterdir()), [tokenizer/'tokenizer.json'])

    def test_public_start_refuses_missing_requirements_before_install_or_build(self):
        shutil.copy(ROOT/'run.sh',self.deploy/'run.sh')
        (self.deploy/'.env').write_text('GGUF=""\nQNN_LIB_DIR=""\n')
        docker=self.root/'bin/docker'
        docker.write_text('#!/bin/sh\nprintf "%s\n" "$*" >> "$MOCK_DOCKER_CALLS"\n')
        docker.chmod(0o755)
        env={**self.env,'MOCK_DOCKER_CALLS':str(self.root/'docker-calls')}
        result=subprocess.run(['bash',str(self.deploy/'run.sh'),'start'],env=env,
                              capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('MISSING:',result.stdout)
        self.assertEqual((self.root/'docker-calls').read_text().splitlines(), ['info', 'compose version'])
        self.assertFalse((self.root/'uv-calls').exists())
        calls=(self.root/'systemctl-calls').read_text()
        self.assertNotIn(' start ',calls)
        self.assertNotIn(' restart ',calls)


class PackagingTests(unittest.TestCase):
    def test_offline_tts_config_preserves_architecture_and_rejects_unknown_assets(self):
        original=('flow_lm:\n  transformer:\n    num_layers: 6\n  lookup_table:\n'
                  '    tokenizer_path: hf://shefowl/pocket-tts-polish-6l/pl_bpe4000.model@revision\n'
                  'weights_path: "hf://shefowl/pocket-tts-polish-6l/model.safetensors@revision"\n')
        converted=assets.local_config(original)
        self.assertIn('num_layers: 6',converted)
        self.assertIn('/assets/pl_bpe4000.model',converted)
        self.assertIn('/assets/model.safetensors',converted)
        self.assertNotIn('hf://',converted)
        with self.assertRaises(ValueError):
            assets.local_config('weights_path: https://invalid.example/other-file\n')

    def test_local_python_imports_are_shipped(self):
        # Check sibling imports that must ship with each entry point.
        gateway_modules = {
            'chat_gateway', 'command_router', 'context_catalog', 'gateway_metrics',
            'latency_stats', 'lva_volume', 'prompt_counter', 'languages', 'wait_sound',
        }
        for path in ROOT.rglob('*.py'):
            if any(part in ('.venv','assets','runtime','tests','tools') for part in path.relative_to(ROOT).parts): continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node,ast.ImportFrom) or not node.module: continue
                module=node.module.split('.')[-1]
                if module.startswith('qnn_') or module in gateway_modules | {'tts_render_worker', 'rust_tts_backend'}:
                    self.assertTrue((path.parent/(module+'.py')).is_file()
                                    or (path.parent/module/'__init__.py').is_file(), (path.name, module))

    def test_env_template_is_public_but_runtime_secrets_stay_ignored(self):
        if not shutil.which('git'): self.skipTest('git unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git','init','-q',tmp],check=True)
            (Path(tmp)/'.gitignore').write_text((ROOT/'.gitignore').read_text())
            candidates=['.env','.env.example','.env.local','stt/assets/model.bin','stt/runtime/vendor.whl']
            result=subprocess.run(['git','-C',tmp,'check-ignore','--stdin'],
                input='\n'.join(candidates)+'\n',capture_output=True,text=True)
            self.assertEqual(set(result.stdout.splitlines()),set(candidates)-{'.env.example'})


if __name__ == '__main__':
    unittest.main()
