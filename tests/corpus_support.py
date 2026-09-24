"""The corpus the calibration tests measure against, and when to skip them.

A handful of tests are not unit tests at all: they check that the graph does not
collapse into one cluster, that the segmenter's decode is plausible, that every bold
span in a real lesson survives the parse, that a real lesson exports faithfully. Those
answers only mean something against a *real* corpus -- a shelf of dozens of hand-made
lessons -- and a fresh clone ships one sample passage, so they have nothing to measure.

Skipping is the honest outcome, and the reason says which of the two situations it is:
no corpus at all, or one too small to calibrate against. `MIN_REAL_CORPUS` is the size
at which the measurements in these tests started to mean something; below it they
would pass or fail for reasons that have nothing to do with the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import corpus_dir

# Enough lessons for the graph to have clusters and the segmenter to have a corpus to
# be plausible across. The corpus this was written against has twenty-odd.
MIN_REAL_CORPUS = 15


def lesson_count() -> int:
    folder = corpus_dir()
    return len(list(folder.glob("*.md"))) if folder.is_dir() else 0


def has_real_corpus() -> bool:
    """Whether there is a corpus worth calibrating against. For skipif decorators,
    which run before any fixture could."""
    return lesson_count() >= MIN_REAL_CORPUS


def real_corpus() -> Path:
    """The configured corpus, or skip the test that asked for it."""
    folder = corpus_dir()
    lessons = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if len(lessons) < MIN_REAL_CORPUS:
        pytest.skip(
            f"needs a corpus of at least {MIN_REAL_CORPUS} lessons to calibrate against "
            f"({len(lessons)} found at {folder})")
    return folder
