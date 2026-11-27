"""Token estimation and Korean-aware tokenization shared by chunking and keyword search."""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence

_HANGUL = r"\uac00-\ud7a3"
_WORD_RE = re.compile(rf"[0-9A-Za-z{_HANGUL}]+(?:[._-][0-9A-Za-z{_HANGUL}]+)*")
_HANGUL_RUN_RE = re.compile(rf"[{_HANGUL}]+")


def estimate_tokens(text: str) -> int:
    """Approximate token count (Korean ~1.5 chars/token, other ~3.5 chars/token).

    Matches the estimator used by AGTools so chunk budgets stay comparable.
    """

    if not text:
        return 0
    hangul = len(re.findall(rf"[{_HANGUL}]", text))
    other = len(text) - hangul
    return max(1, int(hangul / 1.5 + other / 3.5))


def korean_bigrams(word: str) -> List[str]:
    """Syllable bigrams of a Hangul run so particles/inflections still match."""

    grams: List[str] = []
    for run in _HANGUL_RUN_RE.findall(word):
        if len(run) < 2:
            continue
        for index in range(len(run) - 1):
            grams.append(run[index : index + 2])
    return grams


def search_tokens(text: str, *, max_tokens: int = 20000) -> List[str]:
    """Tokens used for the keyword index: lowercased words plus Korean bigrams."""

    tokens: List[str] = []
    if not text:
        return tokens
    for word in _WORD_RE.findall(text.lower()):
        tokens.append(word)
        if len(tokens) >= max_tokens:
            return tokens
        for gram in korean_bigrams(word):
            tokens.append(gram)
            if len(tokens) >= max_tokens:
                return tokens
    return tokens


def fts_body(text: str, *, max_tokens: int = 20000) -> str:
    """Indexable FTS5 body: de-duplicated token stream (bigram-expanded)."""

    seen: set[str] = set()
    ordered: List[str] = []
    for token in search_tokens(text, max_tokens=max_tokens):
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return " ".join(ordered)


def fts_query(text: str, *, max_terms: int = 32) -> str:
    """Safe FTS5 OR-query built from query tokens (quoted, no operator injection)."""

    terms: List[str] = []
    seen: set[str] = set()
    for token in search_tokens(text):
        if len(token) < 2 and not re.match(rf"^[{_HANGUL}]$", token):
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= max_terms:
            break
    if not terms:
        return ""
    return " OR ".join('"' + term.replace('"', "") + '"' for term in terms)


def query_terms(text: str) -> List[str]:
    """Distinct query words (without bigram expansion) used for coverage scoring."""

    return [word for word in _WORD_RE.findall(text.lower()) if word]


def coverage(text: str, terms: Sequence[str]) -> float:
    if not terms:
        return 0.0
    lowered = text.lower()
    hits = sum(1 for term in terms if term in lowered)
    return hits / len(terms)


def merge_terms(sequence: Iterable[str]) -> List[str]:
    return [term for term in sequence if term]


def excerpt(text: str, query: str, *, max_chars: int = 480) -> str:
    """Query-centred excerpt with the query words kept intact."""

    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    lowered = cleaned.lower()
    position = -1
    for term in query_terms(query):
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return cleaned[:max_chars].rstrip() + "…"
    start = max(0, position - max_chars // 3)
    end = min(len(cleaned), start + max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(cleaned) else ""
    return prefix + cleaned[start:end].strip() + suffix
