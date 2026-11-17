"""Small language Strategy interface. No HA catalogs or model dependencies."""
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
import json
import os
from pathlib import Path
import re

try:
    import simplemma
except ImportError:
    simplemma = None


_DIACRITICS = str.maketrans('ąćęłńóśźżĄĆĘŁŃÓŚŹŻ', 'acelnoszzACELNOSZZ')


def normalize(text):
    """Lowercase, strip Polish diacritics and collapse whitespace."""
    return ' '.join(text.translate(_DIACRITICS).lower().split())


def load_vocabulary(path, reserved):
    """Load a complete language vocabulary. Invalid configuration fails at startup.

    Only verbs and spoken replies are data. Tool mappings, supported domains,
    grammar and selector checks remain code, independent of local synonyms.
    """
    with Path(path).open(encoding='utf-8') as source:
        data = json.load(source)
    if not isinstance(data, dict) or set(data) != {'commands', 'responses'}:
        raise ValueError('Vocabulary requires commands and responses')
    commands, responses = data['commands'], data['responses']
    required = {'close', 'open', 'on', 'off'}
    if not isinstance(commands, dict) or not required <= set(commands) <= required | {'position'}:
        raise ValueError('Vocabulary commands require close, open, on and off, with optional position')
    seen, normalized = set(), {}
    for intent, words in commands.items():
        if not isinstance(words, list) or not words:
            raise ValueError('Each intent requires a nonempty list of command phrases')
        verbs = set()
        for word in words:
            if not isinstance(word, str) or not word.split() or not all(token.isalpha() for token in word.split()):
                raise ValueError('Command synonyms must be alphabetic words or phrases')
            verb = normalize(word)
            if any(token in reserved for token in verb.split()) or verb in seen:
                raise ValueError('Command synonyms conflict after normalization')
            verbs.add(verb)
        seen.update(verbs)
        normalized[intent] = frozenset(verbs)
    required_responses = {'success', 'default_clarification', 'clarifications'}
    allowed_responses = required_responses | {'failure'}  # legacy optional key
    if not isinstance(responses, dict) or not required_responses <= set(responses) <= allowed_responses:
        raise ValueError('Vocabulary responses have missing or unknown keys')
    clarifications = responses['clarifications']
    reasons = {'negation', 'catalog-unavailable', 'no-target', 'ambiguous-area',
               'missing-area', 'area-mismatch', 'ambiguous-target'}
    optional = {'plural-target', 'invalid-position', 'position-unavailable'}
    if not isinstance(clarifications, dict) or not reasons <= set(clarifications) <= reasons | optional:
        raise ValueError('Vocabulary clarifications have missing or unknown reasons')
    spoken = [responses[key] for key in ('success', 'default_clarification')]
    if 'failure' in responses:
        spoken.append(responses['failure'])
    if any(not isinstance(text, str) or not text.strip() or len(text) > 300
           for text in spoken + list(clarifications.values())):
        raise ValueError('Spoken responses must contain 1-300 characters')
    return {'commands': normalized, 'responses': responses}


@dataclass(frozen=True)
class Command:
    intent: str | None = None
    target: str = ''
    reason: str | None = None
    position: int | None = None


class Language:
    """Language strategies parse commands and prepare names, never choose devices."""
    code = ''
    area_prepositions = ()
    reserved = frozenset()
    supports_forms = False
    percentage_unit = ''
    position_prepositions = ()

    def __init__(self, vocabulary=None):
        path = vocabulary or Path(__file__).with_name('router_' + self.code + '.json')
        self.vocabulary = load_vocabulary(path, self.reserved)
        self.commands = sorted(((intent, phrase.split())
                                for intent, phrases in self.vocabulary['commands'].items()
                                for phrase in phrases), key=lambda item: (-len(item[1]), item[1]))

    normalize = staticmethod(normalize)

    @property
    def has_lemma(self):
        return simplemma is not None

    def lemma(self, text):
        if simplemma is not None:
            text = ' '.join(simplemma.lemmatize(token, lang=self.code) for token in text.split())
        return self.normalize(text)

    def forms(self, alias):
        return ()

    def find_command(self, tokens):
        for index in range(len(tokens)):
            for intent, words in self.commands:
                if tokens[index:index+len(words)] == words:
                    return intent, index, len(words)
        return None, -1, 0

    def parse(self, text):
        raise NotImplementedError

    @lru_cache(maxsize=1)
    def _number_words(self):
        # Reuse the same library as TTS instead of hand-maintaining number lists.
        # Digits still work if this optional runtime dependency is unavailable.
        try:
            from num2words import num2words
        except ImportError:
            return {}
        return {self.lemma(num2words(value, lang=self.code).replace('-', ' ')): value
                for value in range(101)}

    def split_position(self, text):
        """Extract one terminal, absolute integer percentage, preserving names.

        `invalid-position` is reserved for a syntactically complete absolute
        position whose value is outside 0-100. Unknown, fractional or relative
        syntax returns no error here and is classified by the language grammar.
        """
        unit = re.search(r'(?:%|\b(?:' + self.percentage_unit + r'))\s*$', text, re.IGNORECASE)
        if unit is None:
            return text, None, None
        words = text[:unit.start()].split()
        counts = ((1,) if words and re.fullmatch(r'[+-]?\d+', words[-1])
                  else range(min(4, len(words)), 0, -1))
        for count in counts:
            number = ' '.join(words[-count:])
            if re.fullmatch(r'[+-]?\d+', number):
                value = int(number)
            else:
                value = self._number_words().get(self.lemma(number.replace('-', ' ')))
            if value is None:
                continue
            target = words[:-count]
            if not target or self.normalize(target[-1]) not in self.position_prepositions:
                continue
            target.pop()
            if not target:
                continue
            if not 0 <= value <= 100:
                return ' '.join(target), value, 'invalid-position'
            return ' '.join(target), value, None
        return text, None, None

    def clarify(self, reason):
        responses = self.vocabulary['responses']
        return responses['clarifications'].get(reason, responses['default_clarification'])

    def warmup(self):
        self.lemma('lights')
        self._number_words()


@lru_cache(maxsize=4)
def get_language(code='pl', vocabulary=None):
    """A fixed two-strategy factory, not a plugin discovery framework."""
    if code not in ('pl', 'en'):
        raise ValueError('AGENT_LANGUAGE must be pl or en')
    module = import_module('qwen_language_' + code)
    return module.Strategy(vocabulary)


def default_language():
    return get_language(os.environ.get('AGENT_LANGUAGE', 'pl'),
                        os.environ.get('ROUTER_VOCABULARY') or None)
