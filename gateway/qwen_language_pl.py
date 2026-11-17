"""Polish command grammar and optional SGJP inflection strategy."""
from functools import lru_cache
from qwen_language import Command, Language

try:
    from polish_inflection import odmien_fraze, odmien_lub_none, odmien_warianty, podaj
except ImportError:
    odmien_fraze = odmien_lub_none = odmien_warianty = podaj = None

_CASES = ('nom', 'gen', 'dat', 'acc', 'ins', 'loc', 'voc')


class Strategy(Language):
    code = 'pl'
    area_prepositions = ('w', 'we')
    reserved = frozenset(('nie', 'prosze', 'w', 'we', 'i', 'oraz', 'potem', 'jesli', 'za', 'na', 'do'))
    supports_forms = odmien_warianty is not None
    percentage_unit = r'procent(?:ach|ów|y|a)?'
    position_prepositions = ('na', 'w', 'do')

    def parse(self, text):
        tokens = self.normalize(text).split()
        intent, index, width = self.find_command(tokens)
        if intent is None:
            return Command(reason='no-command')
        if 'nie' in tokens[:index]:
            return Command(reason='negation')
        if tokens[:index] not in ([], ['prosze']):
            return Command(reason='outside-grammar')
        target, position, error = self.split_position(' '.join(text.split()[index+width:]))
        if error:
            return Command(intent=intent, target=target, reason=error, position=position)
        tail = self.normalize(target).split()
        if (any(word in self.reserved - {'prosze', 'w', 'we'} for word in tail)
                or any('%' in word or word.startswith('procent') for word in tail)
                or self.find_command(tail)[0] is not None):
            return Command(reason='outside-grammar')
        if intent == 'position' and position is None:
            return Command(reason='outside-grammar')
        if intent == 'position' and tail[:1] in (['pozycje'], ['pozycja']):
            target = ' '.join(target.split()[1:])
        return Command(intent, target, position=position)

    def warmup(self):
        self.lemma('lampę')
        self.forms('lampa')
        self.forms('pokój')
        self._number_words()

    def _head_lemma(self, alias):
        """(lemma, numbers) of the alias head, or None when not safely inflectable."""
        if odmien_warianty is None:
            return None
        head = next((token for token in alias.split() if any(c.isalpha() for c in token)), None)
        if head is None:
            return None
        try:
            analyses = podaj(head)
        except Exception:
            return None
        nominative = [a for a in analyses if a.przypadek == 'nom']
        if not nominative:
            return None
        numbers = tuple(sorted({a.liczba for a in nominative}))
        if not numbers or odmien_lub_none(nominative[0].lemat, 'nom') is None:
            return None
        return nominative[0].lemat, numbers

    @lru_cache(maxsize=1024)
    def forms(self, alias):
        """Generated (normalized form, number) pairs for one alias.

        A multi-word alias is inflected only when its head is a known noun. That
        keeps `Lampa sufitowa` working and stops the heuristic from rewriting
        brand-like names (`Shelly Plus 2PM` -> `Shelly Plusa 2PM`). Lowercasing is
        deliberate: the analyzer treats capitalized words as proper names.
        """
        if odmien_warianty is None:
            return ()
        alias = alias.lower()
        head = self._head_lemma(alias)
        if head is None:
            return ()
        lemma, numbers = head
        # Generate both numbers; shared target resolution checks uniqueness.
        numbers = ('sg', 'pl') if 'sg' in numbers else numbers
        tokens = alias.split()
        out = []
        for case in _CASES:
            for number in numbers:
                try:
                    if len(tokens) == 1:
                        forms = odmien_warianty(lemma, case, number)
                    else:
                        forms = (odmien_fraze(alias, case, number),)
                except Exception:
                    continue
                for form in forms:
                    norm = self.normalize(form)
                    if not norm or (norm == self.normalize(alias) and case != 'nom'):
                        continue
                    out.append((norm, number))
        return tuple(out)
