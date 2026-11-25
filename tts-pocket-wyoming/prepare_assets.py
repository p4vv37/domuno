#!/usr/bin/env python3
"""Prepare public Pocket TTS assets for offline use. Never runs a model."""
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
# Commit of the v4.0.1 tag; preserve architecture and text normalization.
CONFIG_BASE = ('https://raw.githubusercontent.com/kyutai-labs/pocket-tts/'
               'a922b912dfa366975294afb568d815b305acde5d/pocket_tts/config/')
PUBLIC_MODEL_BASE = 'https://huggingface.co/kyutai/pocket-tts-without-voice-cloning/resolve/'
VOICE_REV = 'a7056f505a4a8ca72d8b5fb1440071d6f1a35739'
OFFICIAL_MODELS = {
    'en': ('english', VOICE_REV, '00eac05ed3d16bdc3f6b5d598874019c34a89214', 'alba'),
    'es': ('spanish', '1e08e6a23401048648a9fdcfde2f89348215c2a7',
           '4e1e0a3e611c51c0b4ed8174fc10f32a54644303', 'lola'),
}
REMOTE_ASSET = re.compile(r'(?:hf|https?)://[^\s\'\"]+')


def local_config(text, language='pl'):
    """Preserve the publisher's architecture, replacing only asset paths."""
    if language == 'pl':
        def local(match):
            filename = match.group().split('@')[0].rsplit('/', 1)[-1]
            if filename not in ('model.safetensors', 'pl_bpe4000.model'):
                raise ValueError('Unexpected remote asset in Polish model config')
            return '/assets/' + filename
        return REMOTE_ASSET.sub(local, text)

    # Fixed voices use the upstream public weights; there is no gated download
    # or network fallback when the model runs inside the container.
    text = re.sub(r'^weights_path_without_voice_cloning:[^\n]*\n?', '', text, flags=re.M)
    for key, filename in [('weights_path', 'model.safetensors'),
                          ('tokenizer_path', 'tokenizer.json')]:
        text, count = re.subn(rf'^([ \t]*{key}:[ \t]*)\S+[^\n]*',
                              rf'\g<1>/assets/{language}/{filename}', text, flags=re.M)
        if count != 1:
            raise ValueError(f'Expected exactly one {key} in upstream config')
    if REMOTE_ASSET.search(text):
        raise ValueError('Unexpected remote asset in upstream model config')
    return text


def download(url, path):
    temporary = path.with_name(path.name + '.part')
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as out:
            while block := response.read(1024 * 1024):
                out.write(block)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def model_sources(language):
    if language == 'pl':
        files = ('model.safetensors', 'pl_bpe4000.model', 'reference.wav', 'config.yaml')
        return {name: f'{MODEL_URL}/{name}' for name in files}
    name, model_rev, tokenizer_rev, voice = OFFICIAL_MODELS[language]
    path = f'languages/{name}'
    return {
        'model.safetensors': f'{PUBLIC_MODEL_BASE}{model_rev}/{path}/model.safetensors',
        'tokenizer.json': f'{PUBLIC_MODEL_BASE}{tokenizer_rev}/{path}/tokenizer.json',
        'reference.safetensors': f'{PUBLIC_MODEL_BASE}{VOICE_REV}/{path}/embeddings/{voice}.safetensors',
        'config.yaml': CONFIG_BASE + name + '.yaml',
    }


def prepare_model(root, language):
    directory = root if language == 'pl' else root / language
    directory.mkdir(parents=True, exist_ok=True)
    for name, url in model_sources(language).items():
        target = directory / name
        if name != 'config.yaml':
            if not target.exists():
                download(url, target)
            continue
        if target.exists():
            # Preserve an existing offline config, but migrate older PL configs.
            if language == 'pl':
                text = target.read_text(encoding='utf-8')
                localized = local_config(text)
                if text != localized:
                    target.write_text(localized, encoding='utf-8')
            continue
        temporary = target.with_name('config.yaml.upstream')
        try:
            download(url, temporary)
            localized = local_config(temporary.read_text(encoding='utf-8'), language)
            temporary.write_text(localized, encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def prepare_runtime(root):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=('pl', 'en', 'es'), default='pl')
    parser.add_argument('--output', type=Path, default=Path(__file__).parent/'assets',
                        help='Common asset root (default: assets beside this script)')
    args = parser.parse_args()
    root = args.output.resolve()
    prepare_model(root, args.language)
    prepare_runtime(root)
    print(f'Prepared public {args.language.upper()} model, reference, offline config and ARM64 runtime.')


if __name__ == '__main__':
    main()
