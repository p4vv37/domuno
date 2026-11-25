"""Polish TTS text normalization, independent of the model backend."""
import re

# Preserve URLs, identifiers, dates, times and version numbers before treating
# standalone digits as speakable numbers.
_NUMBER_RE = re.compile(
    r"(?P<protected>https?://[^\s]+"
    r"|(?<!\w)[0-9]+(?:[.:/-][0-9]+){2,}(?!\w)"
    r"|(?<!\w)[0-9]+[:/-][0-9]+(?!\w)"
    r"|\b(?=\w*[0-9])(?=\w*[A-Za-z_])\w+(?:[./:-]\w+)*\b)"
    r"|(?<![\w.,:/+-])(?P<number>[+-]?(?:[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+|[0-9]+)"
    r"(?:[.,][0-9]+)?)(?!\w|[.,:/-][0-9])(?P<unit>[ \t]*(?:°C|%))?"
)


def normalize_pl_numbers(text):
    """Speak standalone numbers and units, preserving structured identifiers."""
    try:
        from num2words import num2words
    except ImportError:
        return text

    def replace(match):
        if match['protected']:
            return match[0]
        token = re.sub(r'[ \u00a0\u202f]', '', match['number']).replace(',', '.')
        sign = 'minus ' if token.startswith('-') else 'plus ' if token.startswith('+') else ''
        integer, separator, fraction = token.lstrip('+-').partition('.')
        value = int(integer)
        words = sign + num2words(value, lang='pl')
        if separator:
            words += ' przecinek ' + ' '.join(
                num2words(int(digit), lang='pl') for digit in fraction)
        unit = (match['unit'] or '').strip()
        if unit == '%':
            words += ' procent'
        elif unit == '°C':
            degree = ('stopnia' if separator else 'stopień' if value == 1 else
                      'stopnie' if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14 else 'stopni')
            words += ' ' + degree + ' Celsjusza'
        return words

    return _NUMBER_RE.sub(replace, text)
