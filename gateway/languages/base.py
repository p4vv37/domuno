"""Small language Strategy interface. No HA catalogs or model dependencies."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
import json
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
    allowed_intents = required | {'position', 'brightness'}
    if not isinstance(commands, dict) or not required <= set(commands) <= allowed_intents:
        raise ValueError('Vocabulary commands require close, open, on and off; '
                         'optional: brightness, position')
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
    percentage: int | None = None


@dataclass(frozen=True)
class Grammar:
    """Bundled single-language rules, separate from replaceable vocabulary."""
    area_prepositions: frozenset[str]
    position_prepositions: frozenset[str]
    reserved_synonym_words: frozenset[str]
    prefix_blockers: frozenset[str]
    tail_blockers: frozenset[str]
    negations: frozenset[str]
    percentage_unit_pattern: str
    percentage_prefix: str
    warmup_word: str
    position_labels: frozenset[str] = frozenset()
    brightness_labels: frozenset[str] = frozenset()
    generic_light_words: frozenset[str] = frozenset()
    leading_articles: frozenset[str] = frozenset()
    normalize_apostrophes: bool = False
    allowed_prefix_words: frozenset[str] | None = None

    @classmethod
    def load(cls, path):
        with path.open(encoding='utf-8') as source:
            data = json.load(source)
        word_sets = ('area_prepositions', 'position_prepositions', 'reserved_synonym_words',
                     'prefix_blockers', 'tail_blockers', 'negations')
        optional_sets = ('position_labels', 'brightness_labels', 'generic_light_words',
                         'leading_articles', 'allowed_prefix_words')
        strings = ('percentage_unit_pattern', 'percentage_prefix', 'warmup_word')
        required = set(word_sets + strings)
        allowed = required | set(optional_sets) | {'normalize_apostrophes'}
        if not isinstance(data, dict) or not required <= data.keys() <= allowed:
            raise ValueError(f'{path}: missing or unknown grammar keys')
        for key in word_sets + optional_sets:
            if key not in data:
                continue
            words = data[key]
            if not isinstance(words, list) or not all(
                    isinstance(word, str) and len(word.split()) == 1 for word in words):
                raise ValueError(f'{path}: {key} must be a list of single words')
            data[key] = frozenset(normalize(word) for word in words)
        for key in strings:
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError(f'{path}: {key} must be a nonempty string')
        if not isinstance(data.get('normalize_apostrophes', False), bool):
            raise ValueError(f'{path}: normalize_apostrophes must be a boolean')
        data['percentage_prefix'] = normalize(data['percentage_prefix'])
        try:
            re.compile(data['percentage_unit_pattern'])
        except re.error as exc:
            raise ValueError(f'{path}: invalid percentage_unit_pattern') from exc
        return cls(**data)


class Language(ABC):
    """Language strategies parse commands and prepare names, never choose devices."""
    @property
    @abstractmethod
    def code(self):
        """Language code used by the dictionaries."""

    @property
    @abstractmethod
    def vocabulary_path(self):
        """Path to this strategy's bundled vocabulary."""

    max_prefix_words = 4

    def __init__(self, vocabulary=None):
        self.grammar = Grammar.load(self.vocabulary_path.with_name('grammar.json'))
        path = vocabulary or self.vocabulary_path
        self.vocabulary = load_vocabulary(path, self.grammar.reserved_synonym_words)
        self.commands = sorted(((intent, phrase.split())
                                for intent, phrases in self.vocabulary['commands'].items()
                                for phrase in phrases), key=lambda item: (-len(item[1]), item[1]))

    @property
    def area_prepositions(self):
        return self.grammar.area_prepositions

    normalize = staticmethod(normalize)

    @property
    def has_lemma(self):
        return simplemma is not None

    def _without_leading_article(self, text):
        words = text.split(maxsplit=1)
        if words and words[0].casefold() in self.grammar.leading_articles:
            return words[1] if len(words) == 2 else ''
        return text

    def lemma(self, text):
        text = self._without_leading_article(text)
        if simplemma is not None:
            text = ' '.join(simplemma.lemmatize(token, lang=self.code) for token in text.split())
        return self.normalize(text)

    def forms(self, alias):
        return ()

    def allows_prefix(self, prefix):
        """Allow a short leading phrase unless it contains a scope operator.

        This is not a filler dictionary: unknown politeness/adverb words are
        accepted, while negation, questions, conditions and conjunctions are
        rejected so the complete utterance can reach the LLM.
        """
        if len(prefix) > self.max_prefix_words:
            return False
        rules = self.grammar
        if rules.allowed_prefix_words is not None:
            prefix = [word.rstrip(',') for word in prefix]
            if any(word not in rules.allowed_prefix_words for word in prefix):
                return False
        return not any(word in rules.prefix_blockers for word in prefix)

    def find_command(self, tokens):
        for index in range(len(tokens)):
            for intent, words in self.commands:
                if tokens[index:index+len(words)] == words:
                    return intent, index, len(words)
        return None, -1, 0

    def parse(self, text):
        """Return a Command; override only for a different parsing algorithm."""
        return self.parse_simple(text)

    def parse_simple(self, text):
        """Parse one action; strategies supply grammar rules or implement parse themselves."""
        rules = self.grammar
        tokens = self.normalize(text)
        if rules.normalize_apostrophes:
            tokens = tokens.replace('’', "'")
        tokens = tokens.split()
        intent, index, width = self.find_command(tokens)
        if intent is None:
            return Command(reason='no-command')
        prefix = tokens[:index]
        if any(word in rules.negations for word in prefix):
            return Command(reason='negation')
        if not self.allows_prefix(prefix):
            return Command(reason='outside-grammar')
        target, percentage, error = self.split_position(' '.join(text.split()[index+width:]))
        target, intent = self._strip_label(target, intent)
        tail = self.normalize(target).split()
        if (any(word in rules.tail_blockers for word in tail)
                or any('%' in word or word.startswith(rules.percentage_prefix) for word in tail)
                or self.find_command(tail)[0] is not None
                or (intent in ('position', 'brightness') and (not tail or percentage is None))):
            return Command(reason='outside-grammar')
        return Command(intent, target, reason=error, percentage=percentage)

    def _strip_label(self, target, intent):
        """Drop a leading "jasność"/"pozycję" label and pick the matching intent.

        `ustaw jasność X na 20%` uses the cover-position verb but is a brightness
        request; the label, not the verb, decides which tool is appropriate.
        """
        words = target.split()
        if not words:
            return target, intent
        head = self.normalize(words[0])
        rules = self.grammar
        if head in rules.brightness_labels:
            if intent in ('position', 'brightness'):
                return ' '.join(words[1:]), 'brightness'
        elif intent == 'position' and head in rules.position_labels:
            return ' '.join(words[1:]), 'position'
        return target, intent

    def is_generic_light(self, text):
        """True when the phrase is a bare light noun, not a device name."""
        allowed = self.grammar.generic_light_words
        words = self._without_leading_article(text).split()
        return bool(allowed and len(words) == 1
                    and (self.normalize(words[0]) in allowed or self.lemma(words[0]) in allowed))

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
        unit = re.search(r'(?:%|\b(?:' + self.grammar.percentage_unit_pattern + r'))\s*$', text, re.IGNORECASE)
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
            if not target or self.normalize(target[-1]) not in self.grammar.position_prepositions:
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
        """Load lazy lemma/inflection/number data before the first request.

        The libraries expose no explicit language loader, so one generic word
        primes them. This is startup work, not routing data.
        """
        self.lemma(self.grammar.warmup_word)
        self.forms(self.grammar.warmup_word)
        self._number_words()

