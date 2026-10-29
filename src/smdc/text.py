"""Non-destructive text processing: normalization, cleaning, language filtering.

The raw text is never modified. Processing adds `text_norm` (what a reader
sees, on one line), `text_clean` (for lexicon/topic models), a detected
language and, when a comment falls outside the target languages, the reason it
is excluded. Excluded comments stay in the database; exports filter them.

Characters are classified by Unicode category rather than a hand-written
emoji range, so non-Latin scripts (CJK, Hangul, Arabic...) are never erased.
"""

import re
import threading
import unicodedata

from .config import Process
from .privacy import redact_mentions

SHORT_TEXT = 20  # characters; below this the word-list fallback may rescue English

# Fallback markers for short English comments that statistical detection
# misses (from Campos-Moinier's EN_short_text.py; keys lowercased so they match).
SHORT_EN_WORDS = {
    "and", "are", "as", "at", "badass", "big", "boring", "but", "crazy", "cringe", "cut",
    "cute", "done", "dumb", "end", "fake", "fat", "garbage", "good", "great", "hair", "idiot",
    "is", "it", "or", "real", "sexy", "slop", "stupid", "that", "the", "their", "them", "they",
    "this", "trash", "ugly", "was", "well", "were", "woke", "worst", "you", "your"}
EN_ABBREVIATIONS = {
    "af", "ai", "ar", "ac", "asmr", "asap", "btc", "btw", "cod", "crypto", "dl", "eth", "f",
    "fps", "gg", "gta", "idc", "idk", "imo", "l", "lgbt", "lol", "ml", "mmorpg", "nft", "nlp",
    "omg", "pls", "pve", "pvp", "r", "rpg", "thx", "tlou", "ty", "u", "vr", "w", "wow", "wp",
    "wtf"}

EMOJI_EXTRA = {"‍", "︎", "️", "⃣"}  # ZWJ, variation selectors, keycap
APOSTROPHES = {"'", "’"}
WS_RE = re.compile(r"\s+")
REASONS = ("empty_after_cleaning", "other_language", "undetected")


def normalize(text: str) -> str:
    """NFC-normalizes and collapses all whitespace (newlines included) to single spaces."""
    return WS_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def clean(text: str, remove_emoji: bool = True, remove_punct: bool = True) -> str:
    """Strips emoji/symbols and punctuation, keeping letters of every script.

    Removed characters become spaces (so "great🔥game" stays two words).
    Apostrophes between letters survive, normalized to "'" (don't, l'homme).

    Args:
        text: Normalized text.
        remove_emoji: Drop Unicode symbols (So, Sk, Me), ZWJ and variation selectors.
        remove_punct: Drop punctuation (P*), math and currency symbols (Sm, Sc).
    """
    out = []
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if remove_emoji and (cat in ("So", "Sk", "Me", "Cs", "Co") or ch in EMOJI_EXTRA):
            out.append(" ")
        elif remove_punct and (cat[0] == "P" or cat in ("Sm", "Sc")):
            inner = 0 < i < len(text) - 1 and text[i - 1].isalnum() and text[i + 1].isalnum()
            out.append("'" if ch in APOSTROPHES and inner else " ")
        else:
            out.append(ch)
    return WS_RE.sub(" ", "".join(out)).strip()


def is_english_short_text(text: str) -> bool:
    """Word-list fallback: an all-ASCII text with a common English word, or an
    English abbreviation plus at least one other token."""
    tokens = text.lower().split()
    if not tokens or not all(t.isascii() for t in tokens):
        return False
    if any(t in SHORT_EN_WORDS for t in tokens):
        return True
    return len(tokens) >= 2 and any(t in EN_ABBREVIATIONS for t in tokens)


_detectors: dict[tuple, object] = {}
_detectors_lock = threading.Lock()


def _detector(candidates: tuple[str, ...]):
    with _detectors_lock:
        return _build_detector(candidates)


def _build_detector(candidates: tuple[str, ...]):
    if candidates not in _detectors:
        try:
            from lingua import IsoCode639_1, LanguageDetectorBuilder
        except ImportError as e:
            raise RuntimeError("language filtering needs lingua: pip install "
                               "'smdc[lang]' (or set languages = [])") from e
        codes = [getattr(IsoCode639_1, c.upper()) for c in candidates]
        _detectors[candidates] = LanguageDetectorBuilder.from_iso_codes_639_1(*codes).build()
    return _detectors[candidates]


def detect(texts: list[str], candidates: list[str]) -> list[tuple[str | None, float]]:
    """Most likely language (ISO 639-1) and its confidence for each text."""
    if not texts:
        return []
    results = _detector(tuple(candidates)).compute_language_confidence_values_in_parallel(texts)
    out = []
    for cv in results:
        top = cv[0] if cv else None
        if top is None or top.value <= 0:
            out.append((None, 0.0))
        else:
            out.append((top.language.iso_code_639_1.name.lower(), round(top.value, 4)))
    return out


def process(texts: list[str], cfg: Process) -> list[tuple]:
    """Processes raw texts.

    Returns:
        One (text_norm, text_clean, lang, lang_conf, excluded_reason) tuple per
        input; excluded_reason is None for kept items, else one of REASONS.
    """
    norms = [normalize(t) for t in texts]
    if cfg.redact_mentions:
        norms = [redact_mentions(t) for t in norms]
    cleans = [clean(t, cfg.remove_emoji, cfg.remove_punct) for t in norms]
    todo = [i for i, c in enumerate(cleans) if c]
    langs: dict[int, tuple] = {}
    if cfg.languages:
        langs = dict(zip(todo, detect([cleans[i] for i in todo], cfg.candidates)))
    out = []
    for i, (n, c) in enumerate(zip(norms, cleans)):
        lang, conf = langs.get(i, (None, 0.0))
        reason, lang = _classify(c, lang, conf, cfg)
        out.append((n, c, lang, conf, reason))
    return out


def _classify(text: str, lang: str | None, conf: float, cfg: Process) -> tuple[str | None, str | None]:
    """Returns (excluded_reason, lang); a text rescued by the word list is labelled 'en'."""
    if not text:
        return "empty_after_cleaning", lang
    if not cfg.languages or (lang in cfg.languages and conf >= cfg.min_confidence):
        return None, lang
    if "en" in cfg.languages and len(text) < SHORT_TEXT and is_english_short_text(text):
        return None, "en"
    return ("undetected" if lang is None or conf < cfg.min_confidence else "other_language"), lang
