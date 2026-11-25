"""Polish TTS text normalization, without model inference or services."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'number_normalization', ROOT / 'tts-pocket-wyoming/text_normalization.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


@unittest.skipUnless(importlib.util.find_spec('num2words'), 'num2words not installed')
class NumberNormalizationTests(unittest.TestCase):
    def test_numbers_units_and_sentence_punctuation(self):
        cases = {
            '0': 'zero',
            'Jasność: 50.': 'Jasność: pięćdziesiąt.',
            '-5': 'minus pięć',
            '+2': 'plus dwa',
            '-0,05': 'minus zero przecinek zero pięć',
            '21.05': 'dwadzieścia jeden przecinek zero pięć',
            '1,050': 'jeden przecinek zero pięć zero',
            '22 000': 'dwadzieścia dwa tysiące',
            '22\u00a0000': 'dwadzieścia dwa tysiące',
            '22\u202f000': 'dwadzieścia dwa tysiące',
            '1000000': 'milion',
            '50%': 'pięćdziesiąt procent',
            '50 %': 'pięćdziesiąt procent',
            '1°C': 'jeden stopień Celsjusza',
            '2 °C': 'dwa stopnie Celsjusza',
            '5°C': 'pięć stopni Celsjusza',
            '12°C': 'dwanaście stopni Celsjusza',
            '22°C': 'dwadzieścia dwa stopnie Celsjusza',
            '112°C': 'sto dwanaście stopni Celsjusza',
            '-1°C': 'minus jeden stopień Celsjusza',
            '21,05°C': 'dwadzieścia jeden przecinek zero pięć stopnia Celsjusza',
            'Brak zmian.': 'Brak zmian.',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                actual = backend.normalize_pl_numbers(text)
                self.assertEqual(actual, expected)
                self.assertEqual(backend.normalize_pl_numbers(actual), actual)

    def test_structured_values_remain_unchanged(self):
        for text in ('21:05', '2026-11-16', '16.11.2026', '16/11/2026',
                     '192.168.1.10', 'v1.2.3', 'light.room_2', 'Qwen2',
                     'https://example.org/50?value=2', '1-5'):
            with self.subTest(text=text):
                self.assertEqual(backend.normalize_pl_numbers(text), text)
        self.assertEqual(
            backend.normalize_pl_numbers('O 21:05 było 2°C.'),
            'O 21:05 było dwa stopnie Celsjusza.')


if __name__ == '__main__':
    unittest.main()
