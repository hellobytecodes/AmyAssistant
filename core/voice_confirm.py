"""
voice_confirm.py
----------------
Turns what the user *said* (as transcribed by Gemini from the microphone) into
a yes / no / unknown verdict, in Persian and English.

A "no" anywhere wins over a "yes" ("بله ولی صبر کن" = no, wait), and anything
unclear is None -- so an unclear answer never approves a dangerous action.
"""

from __future__ import annotations

import re
from typing import Optional

_CHAR_MAP = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "آ": "ا", "أ": "ا", "إ": "ا", "ة": "ه", "ۀ": "ه"})
_STRIP_RE = re.compile(r"[\u064B-\u065F\u0670\u200c\u200d\u0640]")  # harakat, ZWNJ, tatweel

_YES = [
    "بله", "بلی", "آره", "اره", "آری", "اری", "باشه", "اوکی", "اکی", "تایید", "تایید میکنم", "تایید کن", "حتما",
    "درسته", "موافقم", "انجام بده", "انجامش بده", "انجام بدید", "بکن",
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "go ahead", "do it",
    "proceed", "absolutely", "correct", "affirmative",
]
_NO = [
    "نه", "نخیر", "نکن", "نکنید", "نمیخوام", "نمی خوام", "نمیخواد", "لغو", "کنسل", "صبر کن", "صبر", "بیخیال",
    "منصرف", "اشتباه", "ول کن", "نزن", "نبند", "پاک نکن", "نه نه", "نمیدونم", "نمی دونم", "مطمئن نیستم", "not", "unsure", "maybe",
    "no", "nope", "don't", "dont", "do not", "stop", "cancel", "wait", "never mind", "nevermind", "abort",
]


def _norm(text: str) -> str:
    text = (text or "").translate(_CHAR_MAP)
    text = _STRIP_RE.sub("", text)
    text = re.sub(r"[^\w\s']", " ", text)          # keep apostrophes for "don't"
    return " " + re.sub(r"\s+", " ", text).strip().lower() + " "


def _has(norm_text: str, phrases: list[str]) -> bool:
    for phrase in phrases:
        p = _norm(phrase)
        if p in norm_text:                          # both sides padded with spaces = whole-word match
            return True
    return False


def classify_answer(text: str) -> Optional[str]:
    """'yes', 'no', or None when the text isn't a clear answer."""
    norm = _norm(text)
    if not norm.strip():
        return None
    # "پاک نکن" contains "پاک کن"? No -- but "نکن" alone is caught by _NO first anyway.
    if _has(norm, _NO):
        return "no"
    if _has(norm, _YES):
        return "yes"
    return None
