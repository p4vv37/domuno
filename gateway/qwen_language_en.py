"""English closed command grammar using the existing multilingual lemmatizer."""
from qwen_language import Command, Language


class Strategy(Language):
    code = 'en'
    area_prepositions = ('in', 'at')
    reserved = frozenset(('not', 'do', 'please', 'in', 'at', 'and', 'then', 'if', 'after', 'to', 'by'))
    percentage_unit = r'percent|per cent'
    position_prepositions = ('to', 'at')

    def lemma(self, text):
        words = text.split()
        if words and words[0].casefold() == 'the':
            words = words[1:]
        return super().lemma(' '.join(words))

    def parse(self, text):
        tokens = self.normalize(text).replace('’', "'").split()
        intent, index, width = self.find_command(tokens)
        if intent is None:
            return Command(reason='no-command')
        if any(word in ('not', "don't", 'never') for word in tokens[:index]):
            return Command(reason='negation')
        if tokens[:index] not in ([], ['please']):
            return Command(reason='outside-grammar')
        target, position, error = self.split_position(' '.join(text.split()[index+width:]))
        if error:
            return Command(intent=intent, target=target, reason=error, position=position)
        tail = self.normalize(target).split()
        if (any(word in ('and', 'then', 'if', 'after', 'to', 'by', 'not', "don't", 'never') for word in tail)
                or any('%' in word or word.startswith('percent') for word in tail)
                or self.find_command(tail)[0] is not None):
            return Command(reason='outside-grammar')
        if intent == 'position' and position is None:
            return Command(reason='outside-grammar')
        return Command(intent, target, position=position)
