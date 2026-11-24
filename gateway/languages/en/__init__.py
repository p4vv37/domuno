"""English closed command grammar using the existing multilingual lemmatizer."""
from pathlib import Path

from ..base import Language


class Strategy(Language):
    code = 'en'
    vocabulary_path = Path(__file__).with_name('vocabulary.json')
