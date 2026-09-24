"""Corpus-wide sanity check on language segmentation.

There is no gold-standard label set for "which words here are Spanish", so this
does not measure accuracy. It measures *implausibility*: a Spanish span that is
mostly English function words, or an English span that is mostly Spanish ones,
is almost certainly a decode error. Counting those gives a cheap regression
signal that can be run after every change to the segmenter.
"""

from __future__ import annotations

import io
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import corpus_dir  # noqa: E402
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import diglot  # noqa: E402

CORPUS = corpus_dir()
MIN_WORDS = 3
TOLERANCE = 0.34


def main() -> None:
    articles = diglot.parse_corpus(CORPUS)
    total_bad = 0
    total_spans = 0
    offenders: list[tuple[str, str, str, float]] = []

    for article in articles:
        bad = 0
        spans = 0
        for block in article.blocks:
            if block.kind != "p":
                continue
            for span in block.spans:
                words = diglot._WORD_RE.findall(span.text)
                if len(words) < MIN_WORDS:
                    continue
                spans += 1
                wrong = [
                    w for w in words
                    if (diglot.word_score(w) < 0) if span.lang == "es"
                ] if span.lang == "es" else [
                    w for w in words if diglot.word_score(w) > 0
                ]
                ratio = len(wrong) / len(words)
                if ratio > TOLERANCE:
                    bad += 1
                    offenders.append((article.slug, span.lang, span.text.strip()[:110], ratio))
        total_bad += bad
        total_spans += spans
        flag = "  <-- check" if bad else ""
        print(f"{article.slug[:52]:<54} spans {spans:>4}   suspect {bad:>3}{flag}")

    print()
    print(f"TOTAL: {total_bad} suspect spans out of {total_spans} ({total_bad / max(total_spans, 1):.2%})")
    print()
    print("Worst offenders:")
    for slug, lang, text, ratio in sorted(offenders, key=lambda o: -o[3])[:15]:
        print(f"  [{lang} {ratio:.0%}] {slug[:30]}")
        print(f"        {text}")

    counts = Counter(lang for _, lang, _, _ in offenders)
    print()
    print(f"by language: {dict(counts)}")


if __name__ == "__main__":
    main()
