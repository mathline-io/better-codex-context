"""Token counts, the one measure of the whole plugin: o200k_base in pure Python, tiktoken 0.14's algorithm.

tiktoken counts in two steps: a regular expression splits the text into pieces (words with their leading
space and apostrophe suffix, runs of up to three digits, punctuation runs, whitespace), and every piece that
is not itself in the vocabulary is merged byte pair by byte pair, always the adjacent pair of lowest rank
first, leftmost on a tie (the `_byte_pair_merge` of its Rust core); the pieces left are the tokens.

Here the same: the expression of tiktoken_ext.openai_public.o200k_base, with the Unicode categories it
names (\\p{L}, \\p{N}, \\p{M} …), which Python's `re` lacks, built as character ranges from `unicodedata`,
and `\\s` spelled out as Unicode White_Space as the Rust regex crate has it (Python's \\s also takes
U+001C–U+001F). Pure-ASCII text, most code and logs, is split by the same expression with ASCII classes
only: the same pieces, 17x faster (measured on 1,000 real texts). The vocabulary is OpenAI's o200k_base.tiktoken,
the table of 200k merge ranks the algorithm needs (the data, not tiktoken's code), shipped gzipped as
assets/o200k_base.ranks.gz and checked against its published sha256; no network and no tiktoken are needed. It is cached in ~/.cache/better-codex-context as marshal (27 ms to load instead of
141 ms to parse) and the expression per Unicode version (213 ms to build). Checked against tiktoken 0.14 on
1,128 real texts (4.4M characters, 1.56M tokens) and edge cases: identical counts.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import gzip
import hashlib
import json
import marshal
from pathlib import Path
import re
import sys
import unicodedata

from .util import atomic_write, user_directory

NAME = "o200k_base"
SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"  # of the uncompressed file, as tiktoken
VOCABULARY = Path(__file__).resolve().parents[2] / "assets" / f"{NAME}.ranks.gz"
NO_RANK = 1 << 62
# Unicode White_Space, the Rust regex crate's \s.
SPACE = "\\t\\n\\x0b\\x0c\\r \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000"
CATEGORIES = {"L": ("Lu", "Ll", "Lt", "Lm", "Lo"), "N": ("Nd", "Nl", "No"), "UPPER": ("Lu", "Lt", "Lm", "Lo", "Mn", "Mc", "Me"),
              "LOWER": ("Ll", "Lm", "Lo", "Mn", "Mc", "Me")}


def directory() -> Path:
    return user_directory("XDG_CACHE_HOME", ".cache") / "better-codex-context"


@lru_cache(maxsize=1)
def _ranks() -> dict[bytes, int]:
    # Named by the vocabulary's hash and the Python version (marshal's format is per version): one content only.
    cache = directory() / f"{NAME}-{SHA256[:16]}.{sys.implementation.cache_tag}.marshal"
    try:
        return marshal.loads(cache.read_bytes())
    except (OSError, EOFError, ValueError, TypeError):
        pass
    data = gzip.decompress(VOCABULARY.read_bytes())
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError(f"{VOCABULARY} is not the o200k_base vocabulary (sha256 mismatch)")
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines() if line)}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cache, marshal.dumps(ranks))
    except (OSError, ValueError):  # ValueError: ContextError, a refused symlink. The cache is optional:
        pass  # counting never fails for it, the next process parses again
    return ranks


def _ranges(codepoints: list[int]) -> str:
    """A character-class body for sorted code points, as ranges."""
    parts, start, previous = [], None, None
    for cp in codepoints + [None]:
        if start is not None and cp != previous + 1:
            low, high = f"\\U{start:08x}", f"\\U{previous:08x}"
            parts.append(low if start == previous else f"{low}-{high}")
            start = None
        if start is None and cp is not None:
            start = cp
        previous = cp
    return "".join(parts)


def _classes() -> dict[str, str]:
    """The Unicode categories the expression names, as class bodies, cached per Unicode version."""
    cache = directory() / f"{NAME}.classes.{unicodedata.unidata_version}.json"
    try:
        return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    members = {name: [] for name in CATEGORIES}
    for cp in range(0x110000):
        category = unicodedata.category(chr(cp))
        for name, wanted in CATEGORIES.items():
            if category in wanted:
                members[name].append(cp)
    classes = {name: _ranges(cps) for name, cps in members.items()}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cache, json.dumps(classes).encode())
    except (OSError, ValueError):
        pass
    return classes


def _expression(c: dict[str, str]) -> re.Pattern:
    """tiktoken's o200k_base expression over the given class bodies."""
    letters_digits, suffix = c["L"] + c["N"], "(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
    return re.compile("|".join([
        f"[^\\r\\n{letters_digits}]?[{c['UPPER']}]*[{c['LOWER']}]+{suffix}",
        f"[^\\r\\n{letters_digits}]?[{c['UPPER']}]+[{c['LOWER']}]*{suffix}",
        f"[{c['N']}]{{1,3}}",
        f" ?[^{SPACE}{letters_digits}]+[\\r\\n/]*",
        f"[{SPACE}]*[\\r\\n]+",
        f"[{SPACE}]+(?![^{SPACE}])",
        f"[{SPACE}]+",
    ]))


@lru_cache(maxsize=1)
def _pattern() -> re.Pattern:
    return _expression(_classes())


@lru_cache(maxsize=1)
def _ascii_pattern() -> re.Pattern:
    """The same expression for pure-ASCII text, where the categories reduce to these classes."""
    return _expression({"L": "A-Za-z", "N": "0-9", "UPPER": "A-Z", "LOWER": "a-z"})


@lru_cache(maxsize=65536)
def _merged(piece: bytes) -> int:
    """Tokens of a piece that is not in the vocabulary: tiktoken's _byte_pair_merge. parts[i] is
    [start of part i, rank of the pair it starts]; merge the lowest-ranked pair (leftmost on a tie), update
    the ranks of its neighbours, repeat until no pair is in the vocabulary."""
    ranks = _ranks()
    parts = [[i, ranks.get(piece[i:i + 2], NO_RANK)] for i in range(len(piece) - 1)]
    parts += [[len(piece) - 1, NO_RANK], [len(piece), NO_RANK]]

    def rank(i: int) -> int:
        return ranks.get(piece[parts[i][0]:parts[i + 3][0]], NO_RANK) if i + 3 < len(parts) else NO_RANK

    while True:
        best, at = NO_RANK, -1
        for i in range(len(parts) - 1):
            if parts[i][1] < best:
                best, at = parts[i][1], i
        if best == NO_RANK:
            return len(parts) - 1
        if at > 0:
            parts[at - 1][1] = rank(at - 1)
        parts[at][1] = rank(at)
        del parts[at + 1]


def count(text: str) -> int:
    """Exactly what tiktoken's o200k_base encode_ordinary counts."""
    ranks = _ranks()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates: replaced as tiktoken does
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    total = 0
    for piece in (_ascii_pattern() if text.isascii() else _pattern()).findall(text):
        data = piece.encode("utf-8")
        total += 1 if data in ranks else _merged(data)
    return total
