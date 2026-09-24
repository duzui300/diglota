"""Render a parsed article back to text with language tags made visible.

Segmentation is the one part of this app that cannot be unit-tested by
assertion -- "is this run of words Spanish?" has no oracle. So it is checked by
eye instead, through this: Spanish is wrapped in brackets, glosses are shown in
angle brackets, and focus words keep their asterisks. If a line reads cleanly,
the parser got it right.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import corpus_dir  # noqa: E402
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import diglot  # noqa: E402

CORPUS = corpus_dir()


def render(article: diglot.Article) -> str:
    out: list[str] = [f"# {article.title}", f"by {article.author}", ""]
    for block in article.blocks:
        if block.kind == "h":
            out.append(f"\n{'#' * block.level} {block.plain}\n")
            continue
        if block.kind in ("byline", "date"):
            out.append(f"  [{block.kind}: {block.plain}]")
            continue
        line = []
        for span in block.spans:
            if span.lang == "es":
                text = f"*{span.text}*" if span.bold else span.text
                gloss = f"<{span.gloss}>" if span.gloss else ""
                line.append(f"{{ES {text.strip()}}}{gloss}")
            else:
                line.append(span.text)
        out.append("".join(line).strip())
    return "\n".join(out)


def main() -> None:
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    articles = diglot.parse_corpus(CORPUS)
    for article in articles:
        if wanted and wanted not in article.slug:
            continue
        print("=" * 100)
        print(render(article))
        print()
        print(f"--- focus vocab ({len(article.focus_pairs())}) ---")
        for pair in article.focus_pairs()[:25]:
            print(f"    {pair.es!r}  ->  {pair.en!r}")
        print(f"--- anchors ({len(article.vocab)}) ---")
        for anchor in article.vocab[:8]:
            print(f"    {anchor.term!r} / {anchor.gloss!r} / {len(anchor.examples)} ex")
        print(f"--- grammar ({len(article.grammar)}) ---")
        for note in article.grammar[:8]:
            print(f"    {note.title!r}")
            print(f"        ex : {(note.example or '')[:110]!r}")
            print(f"        exp: {(note.explanation or '')[:110]!r}")


if __name__ == "__main__":
    main()
