"""Invisible and control characters: one definition for all untrusted text.

Text from notes, captures and plugins can carry characters a person does not
see but a model reads: bidi overrides, zero-width characters, Unicode TAG
characters (a classic way to smuggle hidden instructions), variation selectors,
fillers and private-use code points. Everything that passes untrusted text on
uses `is_invisible` or `strip_invisible` from here.
"""

from __future__ import annotations

import re
import unicodedata

# Categories that are never visible text: format, private use, surrogates, unassigned.
INVISIBLE_CATEGORIES = frozenset(("Cf", "Co", "Cs", "Cn"))
# Visible-looking categories that still render as nothing (or are ignored by people).
INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),    # soft hyphen
    (0x034F, 0x034F),    # combining grapheme joiner
    (0x115F, 0x1160),    # Hangul choseong/jungseong fillers
    (0x180E, 0x180E),    # Mongolian vowel separator
    (0x200B, 0x200F),    # zero-width space/joiners, LRM, RLM
    (0x202A, 0x202E),    # bidi embeddings and overrides
    (0x2060, 0x2064),    # word joiner, invisible operators
    (0x2066, 0x2069),    # bidi isolates
    (0x3164, 0x3164),    # Hangul filler
    (0xFE00, 0xFE0F),    # variation selectors
    (0xFEFF, 0xFEFF),    # byte order mark / zero-width no-break space
    (0xFFA0, 0xFFA0),    # halfwidth Hangul filler
    (0xE0000, 0xE007F),  # TAG characters
    (0xE0100, 0xE01EF),  # variation selectors supplement
)

_SUSPECT = re.compile(r"[^\x20-\x7e]")  # printable ASCII never needs a closer look


def is_invisible(ch: str) -> bool:
    """True for a character that people do not see but a model may read."""
    if unicodedata.category(ch) in INVISIBLE_CATEGORIES:
        return True
    code = ord(ch)
    return any(low <= code <= high for low, high in INVISIBLE_RANGES)


def is_unsafe(ch: str) -> bool:
    """Invisible or a control character (category Cc, which includes newline and tab)."""
    return unicodedata.category(ch) == "Cc" or is_invisible(ch)


def strip_invisible(text: str, keep: str = "") -> str:
    """Remove invisible and control characters, except the characters listed in keep."""

    def replace(match: re.Match[str]) -> str:
        ch = match.group()
        return ch if ch in keep or not is_unsafe(ch) else ""

    return _SUSPECT.sub(replace, text)
