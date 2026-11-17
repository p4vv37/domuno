"""Allow one backend startup per boot, before Whisper. No imports load models."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request

BACKEND_URL = 'http://127.0.0.1:18192/v1'
MODEL = 'bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0'
WHISPER_PREFIXES = ('whisper-small-qnn', 'whisper-medium-qnn', 'whisper-turbo-qnn')


def check_version(binary, cache_policy='reuse'):
    """Check the CLI version without serving or loading a model."""
    result = subprocess.run([binary, '--skip-update', '--version'],
                            capture_output=True, text=True, check=True, timeout=20)
    match = re.search(r'^GenieX CLI Version:\s*v([0-9]+\.[0-9]+\.[0-9]+)\s*$',
                      result.stdout, re.MULTILINE)
    version = match.group(1) if match else None
    if version != '0.11.0' and not (version == '0.9.3' and cache_policy == 'reset'):
        raise RuntimeError('Use GenieX 0.11.0, or explicitly select reset policy for 0.9.3')
    return version


def guard(*, boot_id=None, state_dir=None):
    """Refuse a second cold load in one boot or while another NPU client runs."""
    boot = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    state = state_dir or Path.home() / '.local/state/ha-local-agent'
    backend = subprocess.run(['pgrep', '-x', 'geniex'], capture_output=True)
    if backend.returncode != 1:
        raise RuntimeError('Backend already running or process inventory unavailable')
    # Only running containers matter. A fresh installation need not contain the
    # old Small/Medium deployments. Docker failure must not look like an empty list.
    inventory = None
    for attempt in range(30):
        inventory = subprocess.run(
            ['docker', 'container', 'ls', '--format', '{{.Names}}'],
            capture_output=True, text=True)
        if inventory.returncode == 0:
            break
        if attempt == 29:
            raise RuntimeError('Docker inventory unavailable; no model load attempted')
        time.sleep(1)
    if any(name.startswith(WHISPER_PREFIXES) for name in inventory.stdout.splitlines()):
        raise RuntimeError('Stop Whisper NPU before cold-loading the LLM')
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / ('attempt-' + boot), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def probe(nctx):
    """Wait for the backend socket, then run exactly one real inference."""
    # GET /models does not load a model. Retry only socket readiness, never POST.
    for attempt in range(60):
        try:
            with urllib.request.urlopen(BACKEND_URL + '/models', timeout=3):
                pass
            break
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ConnectionRefusedError) or attempt == 59:
                raise
            time.sleep(0.25)
    print('NATIVE_BACKEND_SOCKET_READY', flush=True)
    body = {
        'model': MODEL,
        'messages': [{'role': 'user',
                      'content': 'Ile wynosi dwa plus dwa? Odpowiedz tylko cyfrą.'}],
        'compute': 'HTP0', 'ngl': -1, 'nctx': nctx, 'enable_think': False,
        'temperature': 0.1, 'top_k': 1, 'seed': 42, 'max_tokens': 32, 'stream': False,
    }
    request = urllib.request.Request(
        BACKEND_URL + '/chat/completions', data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    start = time.monotonic()
    print('SINGLE_COLD_LOAD_ATTEMPT_HTP0_NO_ASR', flush=True)
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print('HTTP_FAILURE', exc.code, exc.read(16384).decode(errors='replace'), flush=True)
        raise
    elapsed = time.monotonic() - start
    print(json.dumps({'seconds': elapsed, 'response': result}, ensure_ascii=False), flush=True)
    text = result['choices'][0]['message'].get('content', '').strip()
    assert text == '4', 'Unexpected answer; inspect output instead of reloading'
    print('FIRST_INFERENCE_CONTENT_PASS_DEVICE_PROOF_REQUIRES_NATIVE_LOG', flush=True)


def wait_stt():
    """Wait for the Whisper container healthcheck; never retry a failed load."""
    for attempt in range(90):
        state = json.loads(subprocess.check_output(
            ['docker', 'inspect', 'whisper-turbo-qnn', '--format', '{{json .State}}'],
            text=True))
        if not state['Running']:
            raise RuntimeError('Whisper NPU stopped; no automatic retry')
        health = state.get('Health', {}).get('Status')
        if health == 'healthy':
            print('WHISPER_NPU_HEALTHY', flush=True)
            break
        if health == 'unhealthy' or attempt == 89:
            raise RuntimeError('Whisper NPU not healthy; inspect logs, no automatic retry')
        time.sleep(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backend startup checks')
    parser.add_argument('command', choices=['guard', 'probe', 'stt-ready', 'version'])
    parser.add_argument('--nctx', type=int, default=10240)
    parser.add_argument('--geniex-bin', default='geniex')
    parser.add_argument('--cache-policy', choices=['reuse', 'reset'], default='reuse')
    args = parser.parse_args()
    if args.command == 'guard':
        guard()
    elif args.command == 'probe':
        probe(args.nctx)
    elif args.command == 'version':
        print(check_version(args.geniex_bin, args.cache_policy))
    else:
        wait_stt()
