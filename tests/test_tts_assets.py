"""Asset preparation contracts without network or an ARM board."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'tts-pocket-wyoming' / 'prepare_assets.py'
SPEC = importlib.util.spec_from_file_location('prepare_assets', SCRIPT)
assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assets)

PL_CONFIG = ('weights_path: hf://shefowl/pocket-tts-polish-6l/model.safetensors\n'
             '    tokenizer_path: hf://shefowl/pocket-tts-polish-6l/pl_bpe4000.model\n')
OFFICIAL_CONFIG = ('weights_path: hf://kyutai/pocket-tts/weights.safetensors@revision\n'
                   'weights_path_without_voice_cloning: hf://kyutai/public/model.safetensors@revision\n'
                   'replace_characters:\n'
                   "  '’': ''''\n"
                   '    tokenizer: tokenizers\n'
                   '    tokenizer_path: hf://kyutai/public/tokenizer.json@revision\n')


class AssetPreparationTest(unittest.TestCase):
    def test_cli_prepares_all_languages_under_common_root(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / SCRIPT.name
            root = script.parent / 'assets'
            tar_bytes = io.BytesIO()
            with tarfile.open(fileobj=tar_bytes, mode='w:gz') as archive:
                for name in ('pocket-tts', 'LICENSE'):
                    data = name.encode()
                    member = tarfile.TarInfo(f'upstream-release/{name}')
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            release = tar_bytes.getvalue()

            calls = []

            def download(url, target):
                calls.append(url)
                if url == assets.RUST_URL:
                    target.write_bytes(release)
                elif url.endswith('config.yaml'):
                    target.write_text(PL_CONFIG, encoding='utf-8')
                elif url.endswith('.yaml'):
                    target.write_text(OFFICIAL_CONFIG, encoding='utf-8')
                else:
                    target.write_bytes(b'artifact')

            with (patch.object(assets, '__file__', str(script)),
                  patch.object(assets, 'RUST_SHA', hashlib.sha256(release).hexdigest()),
                  patch.object(assets, 'download', download)):
                for args in ([], ['--language', 'en', '--output', str(root)], ['--language', 'es']):
                    with patch('sys.argv', [str(script), *args]):
                        assets.main()
                # Existing config overrides and downloaded artifacts are not fetched again.
                with (root / 'en/config.yaml').open('a', encoding='utf-8') as config:
                    config.write('# custom\n')
                with patch('sys.argv', [str(script), '--language', 'en']):
                    assets.main()

            self.assertEqual({p.name for p in root.iterdir()}, {
                'en', 'es', 'model.safetensors', 'pl_bpe4000.model', 'reference.wav',
                'config.yaml', 'pocket-tts-arm64.tar.gz', 'pocket-tts', 'LICENSE',
            })
            self.assertEqual((root / 'pocket-tts').stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / 'LICENSE').read_bytes(), b'LICENSE')
            self.assertIn('tokenizer_path: /assets/pl_bpe4000.model', (root / 'config.yaml').read_text())
            for language in ('en', 'es'):
                base = root / language
                self.assertEqual({p.name for p in base.iterdir()}, {
                    'model.safetensors', 'reference.safetensors', 'tokenizer.json', 'config.yaml',
                })
                config = (base / 'config.yaml').read_text(encoding='utf-8')
                self.assertIn(f'weights_path: /assets/{language}/model.safetensors', config)
                self.assertIn(f'tokenizer_path: /assets/{language}/tokenizer.json', config)
                self.assertIn("  '’': ''''", config)
                self.assertNotIn('hf://', config)
                self.assertNotIn('weights_path_without_voice_cloning', config)
            self.assertTrue((root / 'en/config.yaml').read_text(encoding='utf-8').endswith('# custom\n'))
            self.assertEqual(len(calls), len(set(calls)))
            self.assertEqual(len(calls), 13)

    def test_uses_pinned_public_model_and_voice_paths(self):
        for language, name, voice in [('en', 'english', 'alba'), ('es', 'spanish', 'lola')]:
            sources = assets.model_sources(language)
            self.assertRegex(sources['config.yaml'], rf'/[a-f0-9]{{40}}/pocket_tts/config/{name}\.yaml$')
            for key, suffix in [('model.safetensors', 'model.safetensors'),
                                ('tokenizer.json', 'tokenizer.json'),
                                ('reference.safetensors', f'embeddings/{voice}.safetensors')]:
                self.assertRegex(sources[key], rf'/pocket-tts-without-voice-cloning/resolve/'
                                  rf'[a-f0-9]{{40}}/languages/{name}/{suffix}$')

    def test_fails_closed_on_unknown_remote_asset(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected remote'):
            assets.local_config(OFFICIAL_CONFIG + 'extra_path: hf://unexpected/model\n', 'es')

    def test_download_does_not_publish_a_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'artifact'
            with patch.object(assets.urllib.request, 'urlopen') as opener:
                opener.return_value.__enter__.return_value.read.side_effect = [b'data', OSError('lost link')]
                with self.assertRaisesRegex(OSError, 'lost link'):
                    assets.download('https://example.org/asset', target)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
