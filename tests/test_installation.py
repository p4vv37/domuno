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


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ha-install-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.deploy=self.root/'path with spaces'
        self.deploy.mkdir()
        shutil.copy(ROOT/'setup.sh', self.deploy/'setup.sh')
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
            'GGUF','TOKENIZER','COUNTER_URL','NCTX','DECIDE_APPLY','GATEWAY_PORT','COUNTER_PORT')}
        self.env.update(PATH=str(fake)+':'+os.environ['PATH'],
                        XDG_CONFIG_HOME=str(self.root/'config'),
                        MOCK_UV_CALLS=str(self.root/'uv-calls'),
                        MOCK_SYSTEMCTL_CALLS=str(self.root/'systemctl-calls'))

    def install(self, **config):
        return subprocess.run(['bash',str(self.deploy/'setup.sh')], env={**self.env,**config},
                              cwd=self.deploy, capture_output=True, text=True)

    @unittest.skipIf(os.geteuid()==0,'installer intentionally refuses root')
    def test_uv_installs_into_quoted_target_and_rerun_keeps_venv(self):
        for _ in range(2):
            result=self.install(DECIDE_APPLY='0')
            self.assertEqual(result.returncode,0,result.stderr)
        calls=[json.loads(line) for line in (self.root/'uv-calls').read_text().splitlines()]
        self.assertEqual(sum(call[0]=='venv' for call in calls),1)
        pip=[call for call in calls if call[:2]==['pip','install']]
        self.assertEqual(len(pip),2)
        self.assertIn(str(self.deploy/'.venv/bin/python'),pip[0])
        unit=(self.root/'config/systemd/user/qwen-ha-gateway.service').read_text()
        self.assertNotIn('--decide-apply',unit)
        self.assertIn('"'+str(self.deploy/'.venv/bin/python')+'"',unit)
        self.assertIn('--nctx 4096',unit)
        self.assertIn('restart qwen-ha-gateway.service',(self.root/'systemctl-calls').read_text())

    @unittest.skipIf(os.geteuid()==0,'installer intentionally refuses root')
    def test_large_context_requires_counter_before_any_install(self):
        result=self.install(NCTX='10240')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('GGUF + TOKENIZER',result.stderr)
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
        binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o755)
        result=self.install(FULL_STACK='1',GENIEX_BIN=str(binary))
        self.assertEqual(result.returncode,0,result.stderr)
        units=self.root/'config/systemd/user'
        backend=(units/'qwen25-3b-backend.service').read_text()
        self.assertIn(str(self.deploy/'llm/backend.py'),backend)
        self.assertIn('--nctx 4096',backend)
        self.assertNotIn('%h/ha-local-agent',backend)
        calls=(self.root/'systemctl-calls').read_text()
        self.assertLess(calls.index('start qwen25-3b-backend.service'),
                        calls.index('start qwen25-whisper-npu.service'))
        self.assertNotIn('restart qwen25-3b-backend',calls)

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
        self.assertEqual((self.root/'docker-calls').read_text().strip(),'info')
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
        # Catches the missing qnn_stt_diagnostics import in the copied STT server.
        for path in ROOT.rglob('*.py'):
            if any(part in ('.venv','assets','runtime') for part in path.parts): continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node,ast.ImportFrom) or not node.module: continue
                module=node.module.split('.')[-1]
                if module.startswith(('qnn_','qwen_')) or module in ('tts_render_worker','rust_tts_backend'):
                    self.assertTrue((path.parent/(module+'.py')).is_file(),(path.name,module))

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
