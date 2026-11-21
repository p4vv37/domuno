"""Whisper decoder prompt-planning regressions. No NPU or model load."""
from pathlib import Path
import sys
import unittest

STT = Path(__file__).resolve().parents[1] / 'stt'
sys.path.insert(0, str(STT))

from qnn_whisper_engine import forced_decoder_token  # noqa: E402


class PromptPlanTests(unittest.TestCase):
    def test_control_prefix_then_prompt_then_free_generation(self):
        prefix = (10, 11, 12, 13)
        prompt = (20, 21)
        self.assertEqual(
            [forced_decoder_token(prefix, prompt, step) for step in range(7)],
            [11, 12, 13, 20, 21, None, None])

    def test_empty_prompt_starts_free_generation_after_control_tokens(self):
        prefix = (10, 11, 12, 13)
        self.assertEqual(
            [forced_decoder_token(prefix, (), step) for step in range(5)],
            [11, 12, 13, None, None])


if __name__ == '__main__':
    unittest.main()
