"""Spanish strategy for single device commands."""
from pathlib import Path

from ..base import Language


class Strategy(Language):
    code = 'es'
    vocabulary_path = Path(__file__).with_name('vocabulary.json')
