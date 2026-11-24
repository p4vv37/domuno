"""Polish command grammar and optional SGJP inflection strategy."""
from functools import lru_cache
from pathlib import Path

from ..base import Language

try:
    from polish_inflection import odmien_fraze, odmien_lub_none, odmien_warianty, podaj
except ImportError:
    odmien_fraze = odmien_lub_none = odmien_warianty = podaj = None

_CASES = ('nom', 'gen', 'dat', 'acc', 'ins', 'loc', 'voc')


class Strategy(Language):
    code = 'pl'
    vocabulary_path = Path(__file__).with_name('vocabulary.json')

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
