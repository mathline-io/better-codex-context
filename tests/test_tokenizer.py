"""The plugin's one token count (tokenizer.py): o200k_base exactly as tiktoken 0.14 counts it."""
from __future__ import annotations
import _isolation  # noqa: F401

import random
import unittest

from better_codex_context import tokenizer
from better_codex_context.util import token_measure

# Counted by tiktoken 0.14.0 (o200k_base, encode_ordinary).
KNOWN = [("hello world", 2), ("Hello world's THE'S  x\r\n\r\n  \t y", 9), ("123456789 12.5% 3,000", 12),
         ("😀👍🏽 é مرحبا 中文测试，。", 12), ("a\x1cb\x1dc\x1e d\x1f", 8), ("   ", 1), ("\n\n\n", 1),
         ("x" * 5000, 625), ("=" * 3000, 48), ("a1b2c3" * 400, 2400), ("\ud800 lone", 2), ("ſ'S 'LL 'Ve", 6),
         ("\xa0　 x", 4), ("def is_expired(self):\n    return self.ttl < now()\n", 14),
         ("未经第二人复核，周五不得部署支付服务；", 13)]


def educational_merge(ranks, piece: bytes) -> int:
    """tiktoken's _educational.bpe_encode, as an independent reference for the merge."""
    parts = [bytes([b]) for b in piece]
    while True:
        best = None
        for i in range(len(parts) - 1):
            rank = ranks.get(parts[i] + parts[i + 1])
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, i)
        if best is None:
            return len(parts)
        i = best[1]
        parts = parts[:i] + [parts[i] + parts[i + 1]] + parts[i + 2:]


def mixed_text(seed: int, length: int = 400) -> str:
    alphabet = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \t\n\r'.,;:!?()[]{}=+-*/_\"\\"
                "中文测试规则部署复核éüñçßЖжΩω😀👍مرحبا　\xa0 ́\x1c")
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(length))


class TokenizerTests(unittest.TestCase):
    def test_known_counts_of_tiktoken(self):
        for text, expected in KNOWN:
            self.assertEqual(expected, tokenizer.count(text), repr(text[:40]))
        self.assertEqual({"count": 2, "method": "o200k_base", "exact": True}, token_measure("hello world"))

    def test_the_merge_matches_tiktokens_reference_algorithm(self):
        ranks, rng = tokenizer._ranks(), random.Random(5)
        pieces = [bytes(rng.randrange(256) for _ in range(rng.randrange(2, 24))) for _ in range(300)]
        pieces += [text.encode("utf-8", "replace") for text, _ in KNOWN if len(text) < 200]
        for piece in pieces:
            if piece not in ranks:
                self.assertEqual(educational_merge(ranks, piece), tokenizer._merged(piece), piece)

    def test_ascii_text_splits_the_same_with_the_ascii_classes(self):
        for seed in range(50):
            text = "".join(c for c in mixed_text(seed) if c.isascii())
            self.assertEqual(tokenizer._pattern().findall(text), tokenizer._ascii_pattern().findall(text))


if __name__ == "__main__":
    unittest.main()
