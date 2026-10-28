import re
from collections import Counter
from itertools import pairwise

_ROOTS = [
    "хуй", "хуе", "хуя", "хуи",
    "пизд",
    "ебат", "ебан", "ебал", "ебуч", "еблан", "ъеб", "уеб",
    "муда", "мудак", "мудил",
    "бля", "блят",
    "сволоч",
    "гнид",
    "тварь",
    "выблядок",
    "ссука", "сука", "сучк",
    "прошманд", "шманд", "шлюх", "шлюш", "курв", "мраз",
]

_LETTER_GAP = r"[^а-яёa-z0-9]{0,2}"
_REPEATED_CHAR_RE = re.compile(r"(.)\1{9,}", re.DOTALL)
_SPAM_WORD_RE = re.compile(r"[\w@#]+", re.UNICODE)


def _collapse_repeated(text: str) -> str:
    result = []
    previous = ""
    for ch in text:
        if ch == previous and ch.isalnum():
            continue
        result.append(ch)
        previous = ch
    return "".join(result)


def _root_pattern(root: str) -> str:
    collapsed_root = _collapse_repeated(root)
    return _LETTER_GAP.join(re.escape(ch) for ch in collapsed_root)


_PATTERNS = [
    re.compile(_root_pattern(root), re.IGNORECASE)
    for root in dict.fromkeys(_collapse_repeated(root) for root in _ROOTS)
]

_LEET_MAP = str.maketrans({
    "0": "о", "3": "з", "4": "ч", "1": "i", "@": "а", "$": "с",
})


def _normalize(text: str) -> str:
    return _collapse_repeated(text.lower().translate(_LEET_MAP))


def contains_profanity(text: str) -> bool:
    normalized = _normalize(text)
    return any(p.search(normalized) for p in _PATTERNS)


_EXPLICIT_ANATOMY_WORDS = {
    "пенис",
    "вагина",
    "влагалище",
    "клитор",
    "анус",
}

_EXPLICIT_CONTEXT_WORDS = {
    "огромный",
    "большой",
    "громадный",
    "длиный",
    "толстый",
    "твердый",
    "маленький",
    "стоячий",
    "половой",
    "мой",
    "твой",
    "его",
    "наш",
    "ваш",
    "свой",
    "сосать",
    "сосал",
    "сосала",
    "сосали",
    "соси",
    "отсосать",
    "отсосал",
    "отсосала",
}


def contains_explicit_sexual(text: str) -> bool:
    words = [
        _collapse_repeated(word.casefold().replace("ё", "е"))
        for word in re.findall(r"[а-яА-ЯёЁ]+", text)
    ]
    if any(word in _EXPLICIT_ANATOMY_WORDS for word in words):
        return True
    for index, word in enumerate(words):
        if word != "член":
            continue
        context = words[max(0, index - 3) : index + 3]
        if any(item in _EXPLICIT_CONTEXT_WORDS for item in context):
            return True
    return False


def is_spam_text(text: str) -> bool:
    if _REPEATED_CHAR_RE.search(text):
        return True
    words = [word.casefold() for word in _SPAM_WORD_RE.findall(text)]
    if len(words) >= 5:
        run = 1
        for previous, current in pairwise(words):
            run = run + 1 if current == previous else 1
            if run >= 5:
                return True
    if len(words) >= 8:
        most_common = Counter(words).most_common(1)[0][1]
        if most_common >= 8 and most_common * 2 >= len(words):
            return True
    compact = "".join(ch for ch in text if not ch.isspace())
    return len(compact) >= 100 and len(set(compact.casefold())) <= 3
