#!/usr/bin/env python3
"""Download public Polish TTS assets on the target board. Never runs a model."""
import argparse
import hashlib
from pathlib import Path
import re
import tarfile
import urllib.request

MODEL_REV = 'cd9047ee2fc9d1eb40ed38673d8b220485713863'
MODEL_URL = 'https://huggingface.co/shefowl/pocket-tts-polish-6l/resolve/' + MODEL_REV
RUST_URL = ('https://github.com/kyutai-labs/pocket-tts/releases/download/v4.0.1/'
            'pocket-tts-3.3.0-aarch64-unknown-linux-gnu.tar.gz')
RUST_SHA = '2b9a727bffd86ea6d73370d675cbd91a7db118f11b7f5a60343c2d5da12b187a'


def local_config(text):
    """Preserve the publisher's architecture, replacing only asset URLs."""
    def local(match):
        filename = match.group().split('@')[0].rsplit('/', 1)[-1]
        if filename not in ('model.safetensors', 'pl_bpe4000.model'):
            raise ValueError('Unexpected remote asset in Polish model config')
        return '/assets/' + filename
    return re.sub(r'(?:hf|https?)://[^\s\'\"]+', local, text)


def download(url, path):
    temporary = path.with_name(path.name + '.part')
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as out:
        while block := response.read(1024 * 1024):
            out.write(block)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent/'assets')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ('model.safetensors', 'pl_bpe4000.model', 'reference.wav', 'config.yaml'):
        target = root/name
        # Never overwrite an existing local config on a repeated preparation.
        if not target.exists():
            download(MODEL_URL+'/'+name, target)
    config = root/'config.yaml'
    config.write_text(local_config(config.read_text()), encoding='utf-8')
    archive = root/'pocket-tts-arm64.tar.gz'
    if not archive.exists():
        download(RUST_URL, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != RUST_SHA:
        raise ValueError('Pocket TTS release checksum mismatch')
    with tarfile.open(archive) as source:
        for name in ('pocket-tts', 'LICENSE'):
            members = [m for m in source.getmembers() if Path(m.name).name == name and m.isfile()]
            if len(members) != 1:
                raise ValueError('Unexpected Pocket TTS release layout')
            (root/name).write_bytes(source.extractfile(members[0]).read())
    (root/'pocket-tts').chmod(0o755)
    print('Prepared public Polish model, reference, offline config and ARM64 runtime.')


if __name__ == '__main__':
    main()
