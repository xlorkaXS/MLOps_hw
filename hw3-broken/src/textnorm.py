"""Нормализация текста и шинглы — общие для очистки, сплита и проверки контаминации.

Один модуль на все три стадии специально: если нормализация разъедется,
дедупликация и проверка контаминации начнут мерить разные вещи, и проверка
станет зелёной при реальном пересечении.
"""

import re
import unicodedata

_SPACES = re.compile(r"\s+")
_DASHES = str.maketrans({"—": "-", "–": "-", "‑": "-", " ": " "})
_OPTIONS = re.compile(r"\b(?:варианты\s+ответа|варианты|options)\s*:")


def normalize_text(text: str) -> str:
    """Каноническая форма строки: NFKC, единые тире, схлопнутые пробелы, нижний регистр."""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return _SPACES.sub(" ", text).strip().lower()


def normalize_group(topic: str) -> str:
    """Каноническая форма названия темы — ключ группы для сплита.

    В источнике одна и та же тема встречается в нескольких написаниях:
    «... - 2025» и «... — 2025», плюс склейка алиасов через «|».
    Без нормализации это разные группы, и сплит по группам протекает.
    """
    return normalize_text(topic.split("|")[0])


def question_for_near_dup(text: str) -> str:
    """Сравнить формулировку вопроса, не зависящую от порядка вариантов.

    Если явного блока вариантов нет, используется весь текст. Одинаковая
    нормализация обязательна и в clean, и в проверке train/test.
    """
    normalized = normalize_text(text)
    match = _OPTIONS.search(normalized)
    if match and normalized[:match.start()].strip():
        return normalized[:match.start()].strip()
    return normalized


def shingles(text: str, size: int) -> set[str]:
    """Множество словных n-грамм — вход для MinHash."""
    words = re.findall(r"\w+", text)
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}
