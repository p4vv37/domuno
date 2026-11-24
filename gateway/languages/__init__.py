"""Router language interface and registry of bundled strategies."""
from functools import lru_cache
from importlib import import_module
import os

from .base import Command, Language, normalize

__all__ = ('Command', 'Language', 'normalize', 'language_codes', 'get_language', 'default_language')

# Load only the selected strategy, so English never loads Polish dictionaries.
_STRATEGIES = {'pl': '.pl', 'en': '.en'}


def language_codes():
    return tuple(_STRATEGIES)


@lru_cache(maxsize=4)
def get_language(code='pl', vocabulary=None):
    if code not in _STRATEGIES:
        raise ValueError('AGENT_LANGUAGE must be one of: ' + ', '.join(language_codes()))
    module = import_module(_STRATEGIES[code], __name__)
    return module.Strategy(vocabulary)


def default_language():
    return get_language(os.environ.get('AGENT_LANGUAGE', 'pl'),
                        os.environ.get('ROUTER_VOCABULARY') or None)
