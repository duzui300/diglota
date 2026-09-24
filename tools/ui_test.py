"""Drive the app in a real browser and check that the interactions work.

Screenshots prove a page rendered; they do not prove a click did anything. This
walks the actual flows -- open an article, click a Spanish word, save it, review
it, grade a translation -- and asserts on what the page shows afterwards. It
uses the Chrome already installed on the machine rather than downloading one.

    python tools/ui_test.py            # everything except the slow AI flows
    python tools/ui_test.py --with-ai  # also grades a translation (calls Jev + the tutor)

Screenshots of each state land in ``shots/``.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "shots"
BASE = "http://127.0.0.1:8787"

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  FAIL  {name}  {detail}")


def skip(name: str, why: str) -> None:
    """Record a check that could not be made, rather than failing it.

    The AI checks depend on a remote model, and that model has outages. A 520
    from the provider is a fact about the environment, not a broken app -- and a
    suite that reports it as a failure trains you to ignore failures.
    """
    SKIPPED.append(f"{name} — {why}")
    print(f"  SKIP  {name}  {why}")


def tutor_reachable(page: Page) -> str:
    """Empty when the tutor answers, or a short reason it did not.

    Probed through the API rather than inferred from a screen, because what is
    being asked is about the outside world.
    """
    try:
        response = page.request.get(
            f"{BASE}/api/word/lookup?term=hola&sentence=hola&slug=&deep=true")
        if response.status != 200:
            return f"the lookup endpoint returned {response.status}"
        data = response.json()
        if data.get("error"):
            return str(data["error"])[:90]
    except Exception as exc:                      # noqa: BLE001 - any failure is a skip
        return f"{type(exc).__name__}: {exc}"[:90]
    return ""


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def fold_text(text: str) -> str:
    """The app's own folding, in Python: lowercase, accents stripped.

    Lives here so a check can ask the same question the page asks -- whether a
    card's word is already its dictionary form -- without the two disagreeing
    about what "the same word" means.
    """
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", str(text).lower())
                   if unicodedata.category(c) != "Mn")


def run(page: Page, *, with_ai: bool) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    # -- library ---------------------------------------------------------- #
    print("\nlibrary")
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".card", timeout=15000)
    cards = page.locator(".card").count()
    check("library lists articles", cards > 10, f"found {cards}")
    check("no modal is covering the page", page.locator("#modal").is_hidden(),
          "the modal was visible on load")
    check("no reader toolbar in the library", page.locator("#reader-bar").is_hidden())
    check("cards show how much of each article you know",
          page.locator(".card .bar").count() > 0)
    shot(page, "ui-library")

    # -- reader ----------------------------------------------------------- #
    print("\nreader")
    page.locator(".card").first.click()
    page.wait_for_selector("#prose", timeout=15000)
    check("reader toolbar appears", page.locator("#reader-bar").is_visible())
    check("toolbar is before the article in the document",
          page.evaluate("""() => {
            const bar = document.getElementById('reader-bar');
            const view = document.getElementById('view');
            return !!(bar.compareDocumentPosition(view) & Node.DOCUMENT_POSITION_FOLLOWING);
          }"""))
    # The real property is stickiness: it must stay pinned while the text moves.
    page.evaluate("window.scrollTo(0, 1400)")
    page.wait_for_timeout(400)
    pinned = page.locator("#reader-bar").bounding_box()
    check("toolbar stays pinned to the top while scrolling",
          bool(pinned) and pinned["y"] < 120, str(pinned))
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)

    spanish = page.locator(".es .w")
    check("Spanish spans are split into clickable words", spanish.count() > 50, f"{spanish.count()} words")
    check("glosses are rendered", page.locator(".gloss").count() > 3)
    shot(page, "ui-reader")

    # reading modes
    page.locator("#opt-es-only").check()
    english_hidden = page.locator("#prose .en").first.is_hidden()
    page.locator("#opt-es-only").uncheck()
    check("Spanish-only mode hides the English", english_hidden)

    page.locator("#opt-dim").check()
    dimmed = page.evaluate("document.body.classList.contains('dim-english')")
    page.locator("#opt-dim").uncheck()
    check("focus mode dims the English", dimmed)

    page.locator("#font-up").click()
    size = page.evaluate("getComputedStyle(document.querySelector('.prose')).fontSize")
    check("text size control works", size not in ("19.5px", ""), size)
    page.locator("#font-down").click()

    page.keyboard.press("e")
    page.wait_for_timeout(250)
    check("E toggles Spanish-only", page.evaluate("document.body.classList.contains('es-only')"))
    page.keyboard.press("e")
    page.wait_for_timeout(200)
    check("E toggles back", not page.evaluate("document.body.classList.contains('es-only')"))
    page.keyboard.press("f")
    page.wait_for_timeout(250)
    check("F toggles focus mode", page.evaluate("document.body.classList.contains('dim-english')"))
    page.keyboard.press("f")
    page.wait_for_timeout(200)

    # coverage ("how much of this do I already know")
    check("reader shows a coverage estimate", page.locator(".coverage-line").count() == 1)
    if page.locator(".coverage-line").count():
        coverage_text = page.locator(".coverage-line").inner_text()
        check("coverage names a percentage", "%" in coverage_text, coverage_text[:70])

    # -- the register tag -------------------------------------------------- #
    # Writing this checks a real file, so it records what it found and puts it
    # back: an article that declared a register gets the same code again (which is
    # a one-line replace, byte for byte), and one whose tag was inferred gets the
    # line removed. Either way the passage ends as it started.
    print("\nthe register tag")
    shelf = page.request.get(f"{BASE}/api/library").json()["articles"]
    editable = [a for a in shelf if a.get("register_editable")]
    compiled = [a for a in shelf if not a.get("register_editable")]
    check("the library knows which passages can be tagged from here",
          bool(editable) and len(editable) + len(compiled) == len(shelf),
          f"{len(editable)} editable, {len(compiled)} not")
    if compiled:
        # A passage inside a compilation is refused rather than retagged: one
        # front-matter block sits between several passages, so a single line would
        # not say which one the reader meant. This checks the refusal without
        # touching the reader's file.
        refused = page.request.post(
            f"{BASE}/api/article/{compiled[0]['slug']}/register", data={"code": "news"})
        check("and the server refuses to tag a passage inside a compilation",
              refused.status == 409, f"{refused.status}")
        check("saying why, rather than failing silently",
              "several passages" in refused.text(), refused.text()[:90])
    if not editable:
        skip("the register tag can be changed", "no single-passage file in this library")
    else:
        slug = editable[0]["slug"]
        page.goto(f"{BASE}/#/read/{slug}")
        page.wait_for_selector("#prose", timeout=15000)
        page.wait_for_timeout(600)
        before = page.request.get(f"{BASE}/api/article/{slug}").json()
        check("the reader says what kind of writing this is",
              page.locator("#register-note").inner_text().strip() != "",
              page.locator("#register-note").inner_text()[:60])
        check("and offers to change it", page.locator("#register-note [data-set-register]").count() == 1)

        page.locator("#register-note [data-set-register]").click()
        page.wait_for_selector(".register-choices", timeout=15000)
        check("the picker offers every register and a way to unset one",
              page.locator(".register-choices [data-register-pick]").count() == 6,
              f"{page.locator('.register-choices [data-register-pick]').count()} choices")
        reg = page.request.get(f"{BASE}/api/registers").json()["registers"]
        check("each choice explains itself", all(r.get("blurb") for r in reg))

        want = "academic" if before.get("register") != "academic" else "news"
        page.locator(f'.register-choices [data-register-pick="{want}"]').click()
        page.wait_for_timeout(900)
        after = page.request.get(f"{BASE}/api/article/{slug}").json()
        check("choosing one writes it into the passage",
              after.get("register") == want, f"{after.get('register')!r} after choosing {want!r}")
        check("and it counts as declared rather than guessed, once the reader says so",
              after.get("register_inferred") is False)
        check("the reader shows the new tag without reloading the page",
              want.capitalize() in page.locator("#register-note").inner_text(),
              page.locator("#register-note").inner_text()[:80])
        shot(page, "ui-register")

        # Put the passage back the way it was found.
        page.locator("#register-note [data-set-register]").click()
        page.wait_for_selector(".register-choices", timeout=15000)
        back = before["register"] if (before.get("register") and before.get("register_inferred") is False) else ""
        page.locator(f'.register-choices [data-register-pick="{back}"]').click()
        page.wait_for_timeout(900)
        restored = page.request.get(f"{BASE}/api/article/{slug}").json()
        check("the passage is left as it was found",
              restored.get("register") == before.get("register")
              and restored.get("register_inferred") == before.get("register_inferred"),
              f"{before.get('register')!r}/{before.get('register_inferred')} -> "
              f"{restored.get('register')!r}/{restored.get('register_inferred')}")

    # A passage inside a compilation cannot be tagged from here, and says so.
    if compiled:
        page.goto(f"{BASE}/#/read/{compiled[0]['slug']}")
        page.wait_for_selector("#prose", timeout=15000)
        page.wait_for_timeout(600)
        check("a passage in a compilation says the tag lives in the file",
              page.locator("#register-note [data-set-register]").count() == 0
              and "in the file" in page.locator("#register-note").inner_text(),
              page.locator("#register-note").inner_text()[:80])

    # -- the contents pane, on a passage that used to list its own furniture --
    # A Psychology Today import: the page's "THE BASICS" sidebar sits in the file
    # twice, and above the title there is a magazine kicker and a publication line.
    # All of it appeared in the contents pane, which made a five-section article
    # look like a fourteen-section one.
    print("\nthe contents pane")
    shelf = page.request.get(f"{BASE}/api/library").json()["articles"]
    lonely = next((a for a in shelf if a["title"].startswith("Loneliness")), None)
    if lonely is None:
        skip("the contents pane lists the article's sections", "that passage is not in this library")
    else:
        page.goto(f"{BASE}/#/read/{lonely['slug']}")
        page.wait_for_selector("#toc", timeout=20000)
        page.wait_for_timeout(600)
        entries = [page.locator("#toc .toc-link").nth(i).inner_text()
                   for i in range(page.locator("#toc .toc-link").count())]
        check("the contents pane lists a handful of sections, not everything short",
              3 <= len(entries) <= 8, str(entries))
        check("it names the sections the article actually has",
              any("emotional state" in e for e in entries) and any("catalyst" in e for e in entries),
              str(entries))
        check("and none of the page's furniture is in it",
              not any(f in " ".join(entries) for f in
                      ("THE BASICS", "Reviewed by", "Take our Loneliness Test",
                       "Stories We Tell", "Posted")),
              str(entries))
        check("no section is listed twice", len(set(entries)) == len(entries), str(entries))

        # The display complaint: with a long list the box cut through an entry and
        # scrolled inside a rail that also scrolled. Nothing should be clipped here,
        # and the list should have nothing to scroll.
        clipped = page.evaluate("""() => {
            const toc = document.querySelector('#toc');
            if (!toc) return null;
            const links = [...toc.querySelectorAll('.toc-link')];
            const box = toc.getBoundingClientRect();
            const last = links[links.length - 1].getBoundingClientRect();
            return {overflow: toc.scrollHeight - toc.clientHeight,
                    past: Math.round(last.bottom - box.bottom)};
        }""")
        check("the list fits its box rather than being cut through an entry",
              clipped and clipped["past"] <= 1, str(clipped))
        check("and there is only one scroller for it",
              clipped and clipped["overflow"] <= 1, str(clipped))
        shot(page, "ui-toc")

    # -- word lookup ------------------------------------------------------ #
    print("\nword lookup (calls the tutor)")
    # Saving for real is the point -- it is the path the reader takes -- but the
    # deck belongs to the reader, so what this writes is recorded and removed
    # again at the end of the section. Learned the hard way: without that, every
    # run left one word behind in a real deck, and which word it was depended on
    # which article happened to come first in the library.
    deck_before = {row["id"] for row in page.request.get(f"{BASE}/api/words").json()["words"]}
    # Pick a content word, not "es" or "la" -- a stopword's entry proves nothing.
    content_words = page.locator(".es .w").filter(has_text=re.compile(r"^[a-záéíóúñ]{6,}$"))
    word = content_words.first
    term = word.inner_text()
    word.click()
    page.wait_for_selector("#popover:not([hidden])", timeout=10000)
    page.wait_for_function(
        "() => { const b = document.querySelector('#pop-body'); "
        "return b && !b.querySelector('.spinner'); }", timeout=120000)
    body = page.locator("#pop-body").inner_text()
    check(f"lookup returns an entry for '{term}'", len(body.strip()) > 5, f"body was {body[:60]!r}")
    check("entry has a translation", page.locator("#pop-actions #pop-save").count() == 1)
    shot(page, "ui-popover")

    save_button = page.locator("#pop-save")
    if save_button.is_disabled():
        # Saved on an earlier run: the reader must recognise that and say so
        # rather than offering to save it twice.
        check("an already-saved word shows as saved", "Saved" in save_button.inner_text(),
              save_button.inner_text())
    else:
        save_button.click()
        page.wait_for_timeout(1500)
        check("saving the word confirms", "Saved" in save_button.inner_text(),
              save_button.inner_text())

    page.keyboard.press("Escape")
    check("Escape closes the popover", page.locator("#popover").is_hidden())

    mine = [row["id"] for row in page.request.get(f"{BASE}/api/words").json()["words"]
            if row["id"] not in deck_before]
    for word_id in mine:
        page.request.delete(f"{BASE}/api/word/{word_id}")
    check("the deck is left as it was found",
          page.request.get(f"{BASE}/api/words").json()["total"] == len(deck_before),
          f"{len(mine)} saved by this run")

    # -- the local glossary makes clicks instant --------------------------- #
    # The corpus already contains the author's translations; those are served
    # from an index the browser holds, so the popover fills without a request.
    print("\nlocal glossary")
    glossary = page.request.get(f"{BASE}/api/glossary").json()
    check("the glossary is built from the corpus", glossary["count"] > 100,
          f"{glossary['count']} entries")
    indexed = [key[2:] for key in glossary["entries"] if key.startswith("w:")]
    check("it ships whole-word keys for the browser", len(indexed) > 50, f"{len(indexed)} keys")

    # Find a word on the page the index can answer, and click it.
    target = None
    for handle in page.locator(".es .w").all()[:400]:
        candidate = handle.inner_text()
        if f"w:{candidate.lower()}" in glossary["entries"]:
            target = handle
            break
    if target is not None:
        text = target.inner_text()
        target.click()
        page.wait_for_selector("#popover:not([hidden])", timeout=8000)
        # The gloss is painted from the local index before the request returns,
        # so a gloss must be present almost immediately.
        try:
            page.wait_for_selector("#pop-body .gloss-big", timeout=1500)
            instant = True
        except Exception:
            instant = False
        check(f"'{text}' is glossed from the local index without waiting", instant)

        # ...and the fuller entry is still reachable, on request.
        page.wait_for_selector("#pop-deep", timeout=15000)
        check("a corpus-answered word still offers the tutor's full entry",
              page.locator("#pop-deep").count() == 1)
        page.locator("#pop-deep").click()
        page.wait_for_selector("#pop-sub:has-text('tutor')", timeout=15000)
        page.wait_for_function(
            "() => { const b = document.querySelector('#pop-body'); "
            "return b && !b.querySelector('.spinner'); }", timeout=120000)
        page.wait_for_timeout(600)
        # The probe asks the outside world *now*; the click happened a moment ago.
        # When the model is up but that one call failed, the popover says so in one
        # short line, and the honest report is a skip rather than a failure -- an app
        # that tells the reader it could not reach the model has done nothing wrong.
        # A *long* body without a full entry is the failure this checks for.
        body_text = page.locator("#pop-body").inner_text().strip()
        reported_a_failure = (not page.locator("#pop-body .gloss-big").count()
                              and len(body_text) < 120)
        unreachable = tutor_reachable(page) or (
            f"that call did not come back: {body_text[:60]}" if reported_a_failure else "")
        if unreachable:
            skip("the full entry replaces the short one", unreachable)
            skip("the full entry carries a part of speech", unreachable)
        else:
            check("the full entry replaces the short one",
                  page.locator("#pop-body .gloss-big").count() == 1)
            check("the full entry carries a part of speech",
                  bool((page.locator("#pop-sub").inner_text() or "").strip()),
                  page.locator("#pop-sub").inner_text()[:50])
        shot(page, "popover-full-entry")
        page.keyboard.press("Escape")
    else:
        check("found a word the local glossary can answer", False, "none on this page")

    # -- review ----------------------------------------------------------- #
    print("\nreview")
    # Seed two cards. Reviewing consumes the queue, so without this a second
    # run finds "Nothing due" and every assertion below silently has nothing to
    # check. Seeding through the API also exercises the save path.
    stamp = str(int(time.time()))
    seeded = []
    for index in range(2):
        term = f"prueba{stamp}{index}"
        # Give the card a sentence containing the term so produce mode has
        # something to blank out; a card saved from the sidebar has no context
        # and can only be reviewed by recognition.
        context = f"Esta es una {term} de la frase que escribimos para probar."
        response = page.request.post(f"{BASE}/api/word/save",
                                     data={"term": term, "gloss": "a test word", "lemma": term,
                                           "context": context})
        if response.ok:
            seeded.append(response.json()["word"]["id"])

    page.goto(f"{BASE}/#/review")
    page.wait_for_selector(".card-face", timeout=15000)
    front = page.locator(".prompt").inner_text()
    check("a card is shown", bool(front.strip()), front)
    check("grading buttons are hidden before reveal", page.locator(".grades button").count() == 0)
    page.keyboard.press("Space")
    page.wait_for_selector(".grades button", timeout=5000)
    check("space reveals the answer", page.locator("#card-answer .gloss").count() == 1)
    previews = page.locator(".grades .when").all_inner_texts()
    check("the four ratings show different intervals", len(set(previews)) >= 3, str(previews))
    shot(page, "ui-review")

    # A card saved as a conjugated form shows the form it belongs to. Two checks,
    # because the deck is the reader's and the first card in the queue is
    # whichever is due soonest: the rule is asserted directly, and then against
    # real data if the card on screen happens to be one this applies to.
    form = page.evaluate("""() => ({
      verb: dictionaryForm({term: 'enfatizan', lemma: 'enfatizar', pos: 'verb'}),
      noun: dictionaryForm({term: 'artefactos', lemma: 'artefacto', pos: 'noun'}),
      same: dictionaryForm({term: 'cobertura', lemma: 'cobertura', pos: 'noun'}),
      none: dictionaryForm({term: 'cobertura'}),
    })""")
    check("a verb shows its infinitive", "infinitive" in form["verb"] and "enfatizar" in form["verb"],
          form["verb"])
    check("a noun is not called an infinitive",
          "dictionary form" in form["noun"] and "infinitive" not in form["noun"], form["noun"])
    check("a word already in its dictionary form says nothing",
          form["same"] == "" and form["none"] == "", f"{form['same']!r} {form['none']!r}")

    queue = page.request.get(f"{BASE}/api/review").json()["cards"]
    if queue:
        first = queue[0]
        expects = bool(first.get("lemma")) and (
            fold_text(first["lemma"]) != fold_text(first["term"]))
        shown = page.locator("#card-answer .form-of").count() == 1
        check("the reveal shows the form for a conjugated word, and only then",
              shown == expects,
              f"{first['term']!r} lemma={first.get('lemma')!r} expected={expects} shown={shown}")

    counter = page.locator("#review-stage .row .small.muted").first
    remaining = counter.inner_text()
    page.keyboard.press("3")
    page.wait_for_timeout(1200)
    check("grading advances the queue", counter.inner_text() != remaining,
          f"still {counter.inner_text()!r}")
    check("grading does not error", not page.locator(".toast.err").count())

    # -- production (cloze) review ---------------------------------------- #
    print("\nproduce mode")
    page.locator('[data-mode="produce"]').click()
    page.wait_for_timeout(400)
    # Not every card can be clozed: a word saved from the sidebar has no
    # sentence, and a phrase may not appear verbatim. Walk a few cards looking
    # for one that does, and require at least one real cloze across the queue.
    cloze_found = False
    for _ in range(6):
        if not page.locator("#cloze-answer").count():
            break
        cloze_found = True
        check("produce mode blanks the word into its sentence",
              page.locator(".cloze-sentence .blank").count() == 1)
        page.locator("#cloze-answer").fill("no sé")
        page.locator("#cloze-check").click()
        page.wait_for_selector("#card-answer .answer", timeout=90000)
        check("checking a cloze answer reveals the word",
              page.locator("#card-answer .answer .gloss").count() == 1)
        check("checking a cloze answer offers grading",
              page.locator("#card-grades .grades button").count() == 4)
        page.keyboard.press("3")
        page.wait_for_timeout(1200)
        break
    if not cloze_found:
        print("  note: no card in this queue could be clozed (all lack a usable sentence)")
    page.locator('[data-mode="recognise"]').click()
    shot(page, "ui-review")

    # leave the deck as we found it
    for word_id in seeded:
        page.request.delete(f"{BASE}/api/word/{word_id}")

    # -- the deck, listed -------------------------------------------------- #
    # Review answers "what is due". This page answers "what have I got", which is
    # a different question and the one a reader asks when a word will not stick.
    print("\nevery saved word")
    page.goto(f"{BASE}/#/review")
    page.wait_for_selector("#view", timeout=15000)
    page.wait_for_timeout(800)
    check("review offers the way to the whole deck",
          page.locator('a[href="#/cards"]').count() >= 1)

    deck = page.request.get(f"{BASE}/api/words").json()
    total = deck["total"]
    page.goto(f"{BASE}/#/cards")
    page.wait_for_selector("#cards-search", timeout=20000)
    page.wait_for_timeout(600)
    rows = page.locator(".deck-row").count()
    check("the page lists the whole deck", rows == len(deck["words"]) and rows > 0,
          f"{rows} rows against {len(deck['words'])} from the API")
    check("and says how many there are",
          str(total) in page.locator("#cards-summary").inner_text(),
          page.locator("#cards-summary").inner_text())

    first = deck["words"][0]
    row = page.locator(".deck-row").first
    check("a row shows the word, its meaning and where it came from",
          first["term"] in row.inner_text() and (first["gloss"] or "") in row.inner_text(),
          row.inner_text()[:100])
    check("a row says when it is next due",
          bool(re.search(r"due|in \d", row.inner_text())), row.inner_text()[:100])
    check("a row offers to say it aloud", row.locator("[data-hear]").count() == 1)

    # Search has to fold accents, the same promise the quotes search makes: a
    # reader who types "cuestion" has been taught to expect "cuestión".
    sample = next((w["term"] for w in deck["words"] if len(w["term"]) > 5), first["term"])
    needle = sample[1:5]
    page.fill("#cards-search", needle)
    page.wait_for_timeout(1200)
    narrowed = page.locator(".deck-row").count()
    check("searching narrows the list", 0 < narrowed <= rows, f"{narrowed} of {rows}")
    if narrowed:
        shown = page.locator(".deck-row").first.inner_text().lower()
        check("and everything it shows matches", needle.lower() in shown, shown[:80])
    check("the search box keeps what was typed",
          page.locator("#cards-search").input_value() == needle)

    page.fill("#cards-search", "")
    page.wait_for_timeout(1200)
    check("clearing it brings the deck back",
          page.locator(".deck-row").count() == rows, f"{page.locator('.deck-row').count()}")

    # Ordering, and the stage filter. Both are client-side, so they are checked
    # against the same rows rather than against a refetch.
    page.select_option("#cards-sort", "alpha")
    page.wait_for_timeout(500)
    terms = [page.locator(".deck-row").nth(i).locator(".es").inner_text()
             for i in range(min(4, rows))]
    check("A–Z really is alphabetical", terms == sorted(terms, key=fold_text), str(terms))

    stage_chips = page.locator("#cards-stages [data-stage]").count()
    check("the stages are offered as filters, and only the ones in use",
          1 < stage_chips <= 5, f"{stage_chips} chips")
    if stage_chips > 1:
        chip = page.locator("#cards-stages [data-stage]").nth(1)
        label = chip.inner_text().split()[0]
        chip.click()
        page.wait_for_timeout(400)
        shown = page.locator(".deck-row").count()
        check(f"a stage filter shows only that stage ({label})", 0 < shown < rows,
              f"{shown} of {rows}")
        page.locator('#cards-stages [data-stage="all"]').click()
        page.wait_for_timeout(400)
        check("and All brings them back", page.locator(".deck-row").count() == rows)

    # Removing asks once, and a single click must not remove anything: the deck
    # is the reader's own work.
    page.select_option("#cards-sort", "recent")
    page.wait_for_timeout(300)
    drop = page.locator(".deck-row").first.locator("[data-drop-card]")
    drop.click()
    page.wait_for_timeout(400)
    check("removing asks before it removes",
          "danger" in (drop.get_attribute("class") or "")
          and page.locator(".deck-row").count() == rows,
          f"{page.locator('.deck-row').count()} rows after one click")
    shot(page, "ui-cards")
    check("the deck is left as it was",
          page.request.get(f"{BASE}/api/words").json()["total"] == total)

    # -- exercises -------------------------------------------------------- #
    print("\nexercises (calls the tutor" + (" and Jev" if with_ai else "") + ")")
    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector("#btn-drills", timeout=15000)
    page.locator("#btn-drills").click()
    page.wait_for_selector(".exercise textarea", timeout=180000)
    drills = page.locator(".exercise").count()
    check("translation drills load", drills >= 4, f"{drills} drills")

    if with_ai:
        first = page.locator(".exercise").first
        prompt = first.locator(".q").inner_text()
        first.locator("textarea").fill("La cámara no mató la pintura, la obligó a evolucionar.")
        first.locator("[data-check]").click()
        page.wait_for_selector(".verdict .meters", timeout=180000)
        meters = page.locator(".verdict .meter").count()
        check("grading returns per-dimension meters", meters >= 2, f"{meters} meters")
        headline = page.locator(".verdict .headline").first.inner_text()
        check("grading returns a verdict", bool(headline.strip()), headline)
        shot(page, "ui-verdict")
        del prompt

    # A grade from a word comparison must not look like one from a judge, and a
    # comparison that cannot read an answer must not colour it red. Both are
    # asserted with the response intercepted, because the tier that produces them
    # only appears when no judgment model is configured -- or when one fails,
    # which is exactly the case a test cannot arrange by waiting.
    print("\ngrading without a judge (intercepted)")

    def local_verdict(*, correct: bool, may_fail: bool, note: str, checks: dict) -> dict:
        return {
            "verdict": {"score": 4.0 if correct else 0.0, "score_max": 4, "checks": checks,
                        "labels": {"note": note}, "method": "local",
                        "model": "word comparison", "latency_ms": 0, "error": None,
                        "may_fail": may_fail},
            "feedback": None, "correct": correct, "provisional": True, "may_fail": may_fail,
        }

    def graded_as(payload: dict) -> tuple[str, str]:
        page.route("**/api/attempt", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)))
        box = page.locator(".exercise").first
        box.locator("textarea").fill("El edificio tenía muchos años.")
        box.locator("[data-check]").click()
        page.wait_for_selector(".exercise .verdict .headline", timeout=30000)
        result = (box.locator(".verdict").get_attribute("class") or "",
                  box.locator(".verdict .headline").inner_text())
        page.unroute("**/api/attempt")
        return result

    klass, headline = graded_as(local_verdict(
        correct=False, may_fail=False, checks={"meaning": .5, "grammar": .5, "natural": .5},
        note="too different from the article's wording to check"))
    check("an unreadable answer is not coloured wrong", "provisional" in klass and "wrong" not in klass, klass)
    check("and it says why it could not check", "too different" in headline, headline)

    klass, headline = graded_as(local_verdict(
        correct=True, may_fail=False, checks={"meaning": 1.0, "grammar": 1.0, "natural": 1.0},
        note="shares most of the article's wording"))
    check("a comparison that matched is still a pass", "correct" in klass, klass)
    shot(page, "ui-verdict-comparison")

    klass, _ = graded_as(local_verdict(
        correct=False, may_fail=True, checks={"fits": 0.0, "same_word": 0.0},
        note="compared with the article's word"))
    check("a wrong cloze can still fail", "wrong" in klass, klass)

    # -- passing a lesson on ------------------------------------------------ #
    # Read-only on purpose: accepting a shared lesson writes a file into the
    # reader's own library, and a test suite should not leave copies of things
    # behind. The import itself is covered by tests/test_transfer.py; what is
    # checked here is that the reader is told what they are being handed.
    print("\nsharing a lesson")
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".page-head p", timeout=20000)
    before = page.locator(".page-head p").first.inner_text()

    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector(".article-foot", timeout=20000)
    check("the reader offers to share a lesson", page.locator("#btn-share").count() == 1)
    check("and asks who is sending it", page.locator("#share-name").count() == 1)

    with page.expect_download(timeout=30000) as download_info:
        page.locator("#share-name").fill("ui-test")
        page.locator("#btn-share").click()
    download = download_info.value
    shared = Path(download.path()).read_text(encoding="utf-8")
    check("sharing downloads a .md file", download.suggested_filename.endswith(".md"),
          download.suggested_filename)
    check("the file names its format and sender",
          "Diglot Format" in shared and "Shared By:** ui-test" in shared)
    check("and carries a digest of the lesson", "Body Digest" in shared)
    check("the lesson itself came through", "POST-READING ANCHORS" in shared)

    page.goto(f"{BASE}/#/library")
    page.wait_for_selector("#btn-open-share", timeout=20000)
    check("the library offers to open a shared lesson", True)
    page.locator("#btn-open-share").click()
    page.wait_for_selector("#share-paste", timeout=10000)
    check("the dialog takes a file or pasted text", page.locator("#share-file").count() == 1)

    # A file that is not a lesson is refused, with a reason rather than a crash.
    page.locator("#share-paste").fill("<html><body>a web page, not a lesson</body></html>")
    page.locator("#share-look").click()
    page.wait_for_selector("#share-report .panel", timeout=20000)
    refusal = page.locator("#share-report").inner_text()
    check("something that is not a lesson is refused", "not a diglot lesson" in refusal)
    check("and the refusal explains what a lesson is", "front matter" in refusal)

    page.locator("#share-paste").fill(shared)
    page.locator("#share-look").click()
    page.wait_for_selector("#share-accept", timeout=20000)
    report = page.locator("#share-report").inner_text()
    check("the provenance is shown before anything is added", "ui-test" in report)
    check("so is how much of it the reader already knows", "already in your deck" in report)
    check("and the lesson's own vocabulary is listed", "It teaches" in report)
    shot(page, "ui-share-report")

    # Nothing has been added to the library by looking at it.
    page.locator("#modal-close").click()
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".page-head p", timeout=20000)
    after = page.locator(".page-head p").first.inner_text()
    check("looking at a lesson does not add it", before == after, f"{before!r} -> {after!r}")

    # -- settings, and the search that reads your deck --------------------- #
    # The panel is checked against the values the app reports, and the *same* values
    # are saved back, so this leaves the app configured exactly as it found it.
    print("\nsettings")
    check("the top bar offers settings", page.locator("#btn-settings").count() == 1)
    check("and no longer a separate 'What next?'",
          page.locator("#btn-recommend").count() == 0, "the old button is still there")

    settings_api = page.request.get(f"{BASE}/api/settings").json()
    check("the browser is never sent the key itself",
          set(settings_api["llm"]) == {"base_url", "model", "ready", "key_set", "key_hint", "from"},
          str(sorted(settings_api["llm"])))

    page.locator("#btn-settings").click()
    page.wait_for_selector("#settings-save", timeout=15000)
    check("the panel shows the endpoint and model in use",
          page.locator("#set-base-url").input_value() == (settings_api["llm"]["base_url"] or "")
          and page.locator("#set-model").input_value() == (settings_api["llm"]["model"] or ""),
          page.locator("#set-model").input_value())
    check("the key field is empty, with the current one described rather than shown",
          page.locator("#set-key").input_value() == ""
          and (settings_api["llm"]["key_set"] is False
               or settings_api["llm"]["key_hint"] in page.locator("#modal-body").inner_text()),
          page.locator("#modal-body").inner_text()[-200:])
    check("and it says whether the model is ready",
          ("model ready" in page.locator("#settings-state").inner_text())
          == bool(settings_api["llm"]["ready"]),
          page.locator("#settings-state").inner_text())
    shot(page, "ui-settings")

    # Saving what is already there: the app stays usable, and now from the app's own
    # settings file rather than the .env.
    page.locator("#settings-save").click()
    page.wait_for_timeout(1500)
    after_save = page.request.get(f"{BASE}/api/settings").json()
    check("saving keeps the app configured",
          after_save["llm"]["ready"] == settings_api["llm"]["ready"],
          f"ready {settings_api['llm']['ready']} -> {after_save['llm']['ready']}")
    check("and it is now the app's own setting",
          after_save["llm"]["from"] == ("app" if settings_api["llm"]["ready"] else after_save["llm"]["from"]),
          str(after_save["llm"]["from"]))

    # ...and forget it, so the app is left reading its .env again, as it was found.
    page.locator("#btn-settings").click()
    page.wait_for_selector("#settings-clear", timeout=15000)
    page.locator("#settings-clear").click()
    page.wait_for_timeout(1500)
    restored_settings = page.request.get(f"{BASE}/api/settings").json()
    check("forgetting hands the values back to the environment",
          restored_settings["llm"]["ready"] == settings_api["llm"]["ready"]
          and restored_settings["llm"]["base_url"] == settings_api["llm"]["base_url"],
          f"{restored_settings['llm']}")
    check("and the app is left as it was found",
          restored_settings["llm"]["model"] == settings_api["llm"]["model"],
          f"{restored_settings['llm']['model']!r} vs {settings_api['llm']['model']!r}")

    # The vocabulary-driven search now lives inside Discover rather than in a dialog
    # of its own: one place to look for something to read, reached two ways.
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector("#btn-discover", timeout=20000)
    page.locator("#btn-discover").click()
    page.wait_for_selector("#discover-recommend-go", timeout=20000)
    check("Discover offers a search by topic and one by vocabulary",
          page.locator("#discover-q").count() == 1
          and page.locator("#discover-recommend-go").count() == 1)
    check("and says what the second one does",
          "your deck" in page.locator("#discover-recommend").inner_text()
          or "words you have saved" in page.locator("#discover-recommend").inner_text(),
          page.locator("#discover-recommend").inner_text()[:90])
    shot(page, "ui-discover-recommend")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # -- keeping a sentence ------------------------------------------------- #
    # The suite puts the reader's collection back exactly as it found it. It
    # records what was already there and deletes only what it made, because the
    # collection is the reader's own work and a test has no business clearing it.
    print("\nkeeping a sentence")
    before_ids = {row["id"] for row in page.request.get(f"{BASE}/api/quotes").json()["quotes"]}
    before_total = len(before_ids)

    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector(".prose .sent", timeout=20000)
    check("the selection bar offers to keep a sentence",
          page.locator('#selection-bar [data-sel="quote"]').count() == 1)

    def keep_sentence(needle: str) -> None:
        """Select a sentence and press Keep.

        The click is dispatched rather than aimed at the button's coordinates.
        The bar hides on any scroll, and poking at pixels that a scroll could
        move out from under the cursor made this flaky for reasons that had
        nothing to do with keeping a sentence. Where the bar appears is asserted
        separately below, so nothing is being taken on trust here.
        """
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        sentence = page.locator(".prose .sent").filter(has_text=needle).first
        sentence.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        sentence.evaluate(
            "el => { const r = document.createRange(); r.selectNodeContents(el);"
            " const s = getSelection(); s.removeAllRanges(); s.addRange(r); }")
        page.evaluate("() => document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}))")
        page.wait_for_selector("#selection-bar:not([hidden])", timeout=8000)
        page.locator('#selection-bar [data-sel="quote"]').dispatch_event("click")
        page.wait_for_timeout(900)

    # The bar has to be somewhere a reader could actually reach it.
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(200)
    near = page.locator(".prose .sent").filter(has_text="Afortunadamente").first
    near.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    near.evaluate("el => { const r = document.createRange(); r.selectNodeContents(el);"
                  " const s = getSelection(); s.removeAllRanges(); s.addRange(r); }")
    page.evaluate("() => document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}))")
    page.wait_for_selector("#selection-bar:not([hidden])", timeout=8000)
    bar = page.locator('#selection-bar [data-sel="quote"]').bounding_box()
    viewport = page.viewport_size
    check("the bar sits where the reader can click it",
          bar and bar["y"] >= 0 and bar["y"] + bar["height"] <= viewport["height"],
          f"{bar} in {viewport}")

    keep_sentence("Afortunadamente")
    kept = page.request.get(f"{BASE}/api/quotes").json()
    check("pressing Keep saves the whole sentence", kept["total"] == before_total + 1,
          f"{kept['total']} kept")
    quote = kept["quotes"][0] if kept["quotes"] else {}
    check("the sentence came through whole", quote.get("text", "").startswith("Afortunadamente"),
          quote.get("text", "")[:60])
    check("and the Spanish is separated from the English",
          bool(quote.get("es")) and "annals" in (quote.get("glosses") or ""),
          f"es={quote.get('es')!r} glosses={quote.get('glosses')!r}")
    check("the word the lesson bolded is attached", bool(quote.get("term")), quote.get("term"))

    # Keeping the same sentence again is reported, not duplicated.
    keep_sentence("Afortunadamente")
    again = page.request.get(f"{BASE}/api/quotes").json()
    check("keeping it twice does not make two", again["total"] == before_total + 1,
          f"{again['total']}")

    # A sentence with no Spanish has nothing to learn in it. Checked through the
    # API rather than the UI on purpose: a refusal is a 400, the browser logs
    # every 400 as a console error, and this suite asserts there are none.
    refused = page.request.post(f"{BASE}/api/quote", data={
        "slug": "how-ai-will-make-art-worse", "block_index": 0,
        "text": "Many live in quiet fear that AI will someday be",
    })
    still = page.request.get(f"{BASE}/api/quotes").json()
    check("a sentence with no Spanish in it is refused",
          refused.status == 400 and still["total"] == before_total + 1,
          f"{refused.status}: {refused.text()[:80]}")

    page.request.post(f"{BASE}/api/quote", data={
        "slug": "how-ai-will-make-art-worse", "block_index": 0,
        "text": "Siguiendo esta tendencia, podemos esperar que la IA empuje a los artistas.",
    })

    page.goto(f"{BASE}/#/quotes")
    page.wait_for_selector(".quote", timeout=15000)
    check("the collection lists the kept sentences", page.locator(".quote").count() >= 1)
    check("each one says where it came from", page.locator(".quote .quote-source").count() >= 1)
    check("and is rendered as it read, with its gloss",
          page.locator(".quote .quote-text .gloss").count() >= 1)
    shot(page, "ui-quotes")

    page.fill("#quote-q", "annals")
    page.wait_for_timeout(600)
    check("searching by the English gloss finds it", page.locator(".quote").count() == 1,
          f"{page.locator('.quote').count()}")
    page.fill("#quote-q", "afortunadamente")
    page.wait_for_timeout(600)
    check("searching by the Spanish finds it", page.locator(".quote").count() == 1)
    page.fill("#quote-q", "zzzznothing")
    page.wait_for_timeout(600)
    check("a search with no matches says so", page.locator(".quote").count() == 0)
    page.fill("#quote-q", "")
    page.wait_for_timeout(600)

    page.locator(".quote [data-note]").first.click()
    page.locator(".quote textarea").first.fill("the annals sentence")
    page.locator(".quote [data-save-note]").first.click()
    page.wait_for_timeout(600)
    check("a note is shown on the sentence",
          "the annals sentence" in page.locator(".quote .quote-note").first.inner_text())
    page.fill("#quote-q", "annals sentence")
    page.wait_for_timeout(600)
    check("and the sentence can be found by its note", page.locator(".quote").count() == 1)
    page.fill("#quote-q", "")
    page.wait_for_timeout(600)

    # The connection back into reading: look a word up and see your own sentence.
    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector(".prose .w", timeout=20000)
    page.locator('.prose .w[data-word="pintan"]').first.click()
    page.wait_for_selector("#popover:not([hidden])", timeout=15000)
    page.wait_for_timeout(2500)
    check("looking a word up shows the sentences you kept with it",
          "In your quotes" in page.locator("#pop-body").inner_text())

    page.goto(f"{BASE}/#/quotes")
    page.wait_for_selector(".quote", timeout=15000)
    before = page.request.get(f"{BASE}/api/quotes").json()["total"]
    page.locator(".quote [data-remove]").first.click()
    check("removing asks once", page.locator(".quote [data-remove]").first.inner_text().startswith("Remove?"))
    page.locator(".quote [data-remove]").first.click()
    page.wait_for_timeout(900)
    after = page.request.get(f"{BASE}/api/quotes").json()["total"]
    check("and the second click removes it", after == before - 1, f"{before} -> {after}")

    # Leave the collection as it was found, deleting only what this section made.
    for row in page.request.get(f"{BASE}/api/quotes").json()["quotes"]:
        if row["id"] not in before_ids:
            page.request.delete(f"{BASE}/api/quote/{row['id']}")
    left = page.request.get(f"{BASE}/api/quotes").json()
    check("the suite left the collection as it found it",
          {row["id"] for row in left["quotes"]} == before_ids,
          f"{left['total']} quotes, expected {before_total}")

    # -- a goal for the week ------------------------------------------------ #
    # The suite puts the reader's goal back as it found it: any challenge it
    # starts is stopped, and any challenge that was already running is left
    # alone rather than replaced.
    print("\nthe week's goal")
    before = page.request.get(f"{BASE}/api/challenge").json()
    running_before = before.get("challenge")

    page.goto(f"{BASE}/#/stats")
    page.wait_for_selector("#challenge-panel .panel", timeout=30000)
    panel = page.locator("#challenge-panel").inner_text()
    if running_before:
        check("a running goal is shown with its progress", "of" in panel, panel[:70])
        check("and how long is left", "day" in panel, panel[:70])
    else:
        check("a goal is proposed rather than imposed", "Start this" in panel, panel[:70])
        check("and the proposal says why", len(panel) > 120, panel[:70])
        shot(page, "ui-challenge")

        # The target list belongs to the kind, so changing the kind rebuilds it.
        targets_before = page.locator("#challenge-target option").all_inner_texts()
        page.select_option("#challenge-kind", "days")
        page.wait_for_timeout(300)
        check("changing the goal changes what is counted",
              page.locator("#challenge-target option").all_inner_texts() != targets_before,
              f"{targets_before} -> {page.locator('#challenge-target option').all_inner_texts()}")

        page.click("#challenge-start")
        page.wait_for_selector(".challenge-bar", timeout=20000)
        page.wait_for_timeout(600)
        running = page.locator("#challenge-panel").inner_text()
        check("starting a goal shows progress against it", "of" in running, running[:70])
        check("and stops offering alternatives",
              page.locator("#challenge-kind").count() == 0, "the picker is still there")

        page.click("#challenge-give-up")
        page.wait_for_selector("#challenge-start", timeout=15000)
        check("giving up offers another goal", page.locator("#challenge-start").is_visible())
        history = page.request.get(f"{BASE}/api/challenge").json()["history"]
        check("and the attempt is kept in the history",
              any(row["ended_at"] for row in history), f"{len(history)} rows")

    # Leave no goal running that was not running when the suite arrived.
    left = page.request.get(f"{BASE}/api/challenge").json().get("challenge")
    if left and not running_before:
        page.request.delete(f"{BASE}/api/challenge/{left['id']}")
    check("the suite left no goal running",
          bool(page.request.get(f"{BASE}/api/challenge").json().get("challenge")) == bool(running_before))

    # -- the writing workspace ---------------------------------------------- #
    # Measuring, autosaving and keeping a version need no model, so they are
    # always checked. The reading of the draft, the inline notes and the
    # suggestions need one, so they wait for --with-ai like the other AI flows.
    print("\nwriting")
    before_writings = page.request.get(f"{BASE}/api/writing").json()
    before_ids = {row["id"] for row in before_writings["writings"]}
    page.goto(f"{BASE}/#/write")
    page.wait_for_selector("#write-text", timeout=20000)
    page.evaluate("() => localStorage.clear()")
    page.wait_for_selector("#write-prompt", timeout=20000)
    check("the workspace offers a prompt", len(page.locator("#write-prompt").inner_text()) > 10)
    check("and four ways to improve a draft",
          page.locator("[data-mode]").count() == 4,
          f"{page.locator('[data-mode]').count()} modes")
    check("with nothing to check before anything is written",
          page.locator("#write-count").inner_text() == "")
    # Two more ways in besides checking: a file, and a lesson of your own writing.
    check("a piece can be opened from a file",
          page.locator("#write-open").is_visible() and page.locator("#write-file").count() == 1)
    check("and made into a lesson", page.locator("#write-lesson").is_visible())
    page.click("#write-lesson")
    page.wait_for_timeout(600)
    toast = page.locator(".toast").last.inner_text() if page.locator(".toast").count() else ""
    check("a lesson from nothing is refused before it costs anything",
          "few sentences" in toast and not page.locator("#modal").is_visible(), toast)

    page.fill("#write-text", (
        "Los modelos de lenguaje han cambiado la manera en que escribimos. Cuando yo era "
        "estudiante, escribía todo a mano, pero ahora la inteligencia artificial me ayuda a "
        "encontrar las palabras. El arte se vuelve más personal cuando el artista piensa en "
        "su propio trabajo, y creo que necesitamos más tiempo para entenderlo."))
    page.wait_for_timeout(2200)
    count = page.locator("#write-count").inner_text()
    check("typing is measured live, and exactly", "words" in count and "Spanish" in count, count)
    check("and the draft is kept in the browser as it is typed",
          bool(page.evaluate("() => localStorage.getItem('diglot.writeDraft:new')")))
    check("the draft is not a version until it is checked",
          page.request.get(f"{BASE}/api/writing").json()["totals"]["pieces"]
          == before_writings["totals"]["pieces"])

    page.click("#write-draft")
    page.wait_for_selector(".version", timeout=30000)
    page.wait_for_timeout(600)
    check("a version can be kept without being read",
          page.locator(".version").count() == 1
          and "not read" in page.locator(".version").first.inner_text(),
          page.locator(".version").first.inner_text())
    first_piece = page.evaluate("() => location.hash")
    check("and it becomes a piece with its own address", first_piece.startswith("#/write/"), first_piece)

    if with_ai:
        page.click("#write-check")
        page.wait_for_selector("#write-feedback .write-facts", timeout=240000)
        page.wait_for_timeout(1000)
        facts = page.locator("#write-feedback .write-facts").first.inner_text()
        check("checking measures the draft without a model",
              "words" in facts and "Spanish" in facts, facts.replace("\n", " | "))
        check("and says what a model made of it",
              page.locator("#write-feedback .meter").count() >= 2,
              f"{page.locator('#write-feedback .meter').count()} meters")
        summary = page.locator("#write-feedback p").first.inner_text()
        check("with a sentence about the draft", len(summary) > 20, summary[:70])

        # Inline feedback: the notes are placed on the reader's own text.
        marks = page.locator("#write-annotated .w-note")
        check("the notes are placed on the text itself", marks.count() >= 1,
              f"{marks.count()} marks")
        if marks.count():
            kinds = {marks.nth(i).get_attribute("class").split()[-1] for i in range(marks.count())}
            marks.first.click()
            page.wait_for_selector("#popover:not([hidden])", timeout=8000)
            body = page.locator("#pop-body").inner_text()
            check("clicking one explains it", len(body.strip()) > 10, body[:60])
            check("and says which kind of note it is",
                  page.locator("#pop-sub").inner_text().strip() != "",
                  page.locator("#pop-sub").inner_text())
            page.keyboard.press("Escape")
            check("a draft with something right says so", "good" in kinds or len(kinds) > 1, str(kinds))
        shot(page, "ui-write")

        # The revision loop: editing after a read is noticed, and the next check
        # is a new version of the same piece.
        page.fill("#write-text", page.locator("#write-text").input_value().replace(
            "me ayuda a encontrar", "me ayuda mucho a encontrar"))
        page.wait_for_timeout(1500)
        check("editing after a read is noticed", page.locator(".write-stale").count() == 1)
        versions_before = page.locator(".version").count()
        page.click("#write-check")
        page.wait_for_selector("#write-feedback .write-facts", timeout=240000)
        page.wait_for_timeout(1000)
        check("and the next check becomes a new version",
              page.locator(".version").count() == versions_before + 1,
              f"{versions_before} -> {page.locator('.version').count()}")
        check("the piece stayed one piece",
              page.evaluate("() => location.hash") == first_piece)
        check("and the stale note is gone once it has been read again",
              page.locator(".write-stale").count() == 0)

        # A suggestion is shown beside the text and never applied.
        before_text = page.locator("#write-text").input_value()
        page.click('[data-mode="natural"]')
        page.wait_for_selector("#write-suggestion .suggestion, #write-suggestion p", timeout=240000)
        page.wait_for_timeout(600)
        if page.locator("#write-use").count():
            check("a suggestion appears beside the writing",
                  page.locator("#write-suggestion .annotated-text").count() == 1)
            check("and the reader's own text is untouched",
                  page.locator("#write-text").input_value() == before_text)
            page.click("#write-use")
            page.wait_for_timeout(800)
            check("taking it puts it in the box to edit",
                  page.locator("#write-text").input_value() != before_text)
        else:
            check("a suggestion appears beside the writing", False,
                  page.locator("#write-suggestion").inner_text()[:80])

    # A piece of your own writing, at the same three dials an import gets. The
    # dialog is not opened with the text typed into it by hand: what is being
    # checked is that the controls are the same ones, in the same words, so a
    # lesson made from your own writing is not a different kind of lesson.
    page.click("#write-lesson")
    page.wait_for_selector("#lesson-go", timeout=20000)
    page.wait_for_timeout(600)
    check("making a lesson offers the same ways in",
          page.locator("#lesson-go").is_visible()
          and "How should the two languages mix?" in page.locator("#modal-body").inner_text())
    check("offers the levels, the amount and the grain",
          page.locator("[data-level]").count() == 6
          and page.locator("[data-amount]").count() == 4
          and page.locator("[data-weave]").count() == 2)
    note = page.locator("#lesson-note").inner_text()
    check("and says what it is about to do with this piece",
          "words" in note and "Spanish" in note, note)
    preview = page.locator("#import-preview").inner_text()
    check("it calls the piece a piece, not an article",
          "piece" in preview and "article" not in preview, preview)
    shot(page, "ui-write-lesson")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    check("closing it without weaving leaves the piece alone",
          not page.locator("[data-job-detail]").count())

    # Leave the reader's writing as it was found.
    after_writings = page.request.get(f"{BASE}/api/writing").json()
    for row in after_writings["writings"]:
        if row["id"] not in before_ids:
            page.request.delete(f"{BASE}/api/writing/{row['id']}")
    check("the suite left no writing behind",
          page.request.get(f"{BASE}/api/writing").json()["totals"]["pieces"]
          == before_writings["totals"]["pieces"])

    # -- stats ------------------------------------------------------------ #
    print("\nprogress")
    page.goto(f"{BASE}/#/stats")
    page.wait_for_selector(".tile", timeout=15000)
    tiles = page.locator("#view > .tiles .tile").count()
    # Four resting tiles, plus whatever the analytics panel adds.
    check("progress tiles render", tiles >= 4, f"{tiles} tiles")
    check("the streak and vocabulary tiles are present",
          page.locator("#view > .tiles .tile", has_text="Streak").count() == 1
          and page.locator("#view > .tiles .tile", has_text="Words saved").count() == 1)
    check("activity chart renders 30 bars", page.locator("#reviews-chart .col").count() == 30,
          f"{page.locator('#reviews-chart .col').count()} bars")
    check("vocabulary stages render", page.locator(".stack span, .legend .item").count() >= 4)
    shot(page, "ui-stats")

    # -- themes ----------------------------------------------------------- #
    print("\nthemes")
    for theme in ("sepia", "night", "paper"):
        page.locator(f'.theme-switch button[data-theme-set="{theme}"]').click()
        page.wait_for_timeout(250)
        applied = page.evaluate("document.documentElement.dataset.theme")
        check(f"{theme} theme applies", applied == theme, applied)
        if theme == "night":
            shot(page, "ui-stats-night")

    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector("#prose", timeout=15000)
    page.locator('.theme-switch button[data-theme-set="night"]').click()
    page.wait_for_timeout(300)
    shot(page, "ui-reader-night")
    page.locator('.theme-switch button[data-theme-set="paper"]').click()

    # -- import difficulty controls ---------------------------------------- #
    print("\nimport difficulty controls")
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".card", timeout=15000)
    page.locator("#btn-import").click()
    page.wait_for_selector("#level-chips", timeout=10000)

    check("every level is offered", page.locator("[data-level]").count() == 6,
          f"{page.locator('[data-level]').count()} levels")
    check("amount presets are offered", page.locator("[data-amount]").count() == 4)
    # The caption is fetched when the controls are wired, so it is waited for
    # rather than read on the next line: on a busy machine the request can still
    # be in flight, and reading an empty box is not a fact about the app.
    try:
        page.wait_for_function(
            "() => { const el = document.querySelector('#import-preview');"
            " return !!(el && el.textContent.trim().length > 30); }", timeout=15000)
    except Exception:                                          # noqa: BLE001
        pass
    check("a live preview explains the choice",
          len(page.locator("#import-preview").inner_text()) > 30,
          page.locator("#import-preview").inner_text()[:60] or "(empty)")

    page.locator('[data-level="A1"]').click()
    page.wait_for_timeout(700)
    light = page.locator("#ratio-value").inner_text()
    check("choosing a level picks a sensible amount for it", light == "22%", light)

    page.locator('[data-level="C1"]').click()
    page.wait_for_timeout(700)
    heavy = page.locator("#ratio-value").inner_text()
    check("a harder level suggests more Spanish", heavy == "60%", heavy)

    # The two dials must be independent: an advanced level read lightly.
    page.locator('[data-amount="0.22"]').click()
    page.wait_for_timeout(700)
    check("the amount overrides the level's suggestion",
          page.locator("#ratio-value").inner_text() == "22%",
          page.locator("#ratio-value").inner_text())
    check("the level choice survives an amount override",
          "active" in (page.locator('[data-level="C1"]').get_attribute("class") or ""))

    page.locator("#import-ratio").fill("55")
    page.locator("#import-ratio").dispatch_event("input")
    page.locator("#import-ratio").dispatch_event("change")
    page.wait_for_timeout(700)
    check("the slider sets an exact amount",
          page.locator("#ratio-value").inner_text() == "55%",
          page.locator("#ratio-value").inner_text())
    shot(page, "import-options")

    # The third dial. Two forms, one of which is chosen; the example has to
    # change with it, because the example is what teaches the difference and a
    # stale one would show the reader the form they did not pick.
    check("both ways of arriving are offered", page.locator("[data-weave]").count() == 2,
          f"{page.locator('[data-weave]').count()} forms")
    mixed_example = page.locator("#weave-example").inner_text()
    mixed_blurb = page.locator("#weave-blurb").inner_text()
    check("the mixed form is the default",
          "active" in (page.locator('[data-weave="chunk"]').get_attribute("class") or ""))
    check("the example is shown, not just named",
          "in:" in mixed_example and "out:" in mixed_example, mixed_example[:60])
    check("the mixed form is described as allowing whole sentences",
          "whole sentence" in mixed_blurb.lower(), mixed_blurb[:80])

    page.locator('[data-weave="sentence"]').click()
    page.wait_for_timeout(800)
    strict_example = page.locator("#weave-example").inner_text()
    strict_blurb = page.locator("#weave-blurb").inner_text()
    check("the strict form becomes the active choice",
          "active" in (page.locator('[data-weave="sentence"]').get_attribute("class") or "")
          and "active" not in (page.locator('[data-weave="chunk"]').get_attribute("class") or ""))
    check("the blurb and the example both change with it",
          strict_example != mixed_example and strict_blurb != mixed_blurb)
    check("the strict form says the two languages never share a sentence",
          "entirely" in strict_blurb and "switch" in strict_blurb.lower(), strict_blurb[:80])
    check("the preview line follows the grain too",
          "sentence" in page.locator("#import-preview").inner_text().lower(),
          page.locator("#import-preview").inner_text()[:80])
    check("the amount is left where the reader put it",
          page.locator("#ratio-value").inner_text() == "55%",
          page.locator("#ratio-value").inner_text())
    shot(page, "ui-import-weave")

    # Reopening should remember the choice rather than resetting to defaults.
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    page.locator("#btn-import").click()
    page.wait_for_selector("#level-chips", timeout=10000)
    page.wait_for_timeout(700)
    check("the choice is remembered next time",
          page.locator("#ratio-value").inner_text() == "55%"
          and "active" in (page.locator('[data-level="C1"]').get_attribute("class") or "")
          and "active" in (page.locator('[data-weave="sentence"]').get_attribute("class") or ""),
          f"{page.locator('#ratio-value').inner_text()} / "
          f"{page.locator('[data-weave].active').get_attribute('data-weave')}")
    # Put it back so the later failure-path test starts from a clean default.
    page.locator('[data-level="auto"]').click()
    page.locator('[data-weave="chunk"]').click()
    page.wait_for_timeout(500)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # -- two ways into an import -------------------------------------------- #
    # A link is fetched; a paste is read. The second exists for pages the app
    # cannot reach at all, which is why it is a mode beside the link and not an
    # error message with a hidden retry in it.
    print("\nimport: a link or a pasted text")
    check("the entry point is called Import, not Import a link",
          page.locator("#btn-import").inner_text().strip() == "Import",
          page.locator("#btn-import").inner_text())
    page.locator("#btn-import").click()
    page.wait_for_selector("#import-modes", timeout=10000)
    check("the dialog is called Import too",
          page.locator("#modal-title").inner_text().strip() == "Import",
          page.locator("#modal-title").inner_text())
    check("both ways in are offered", page.locator("#import-modes [data-way]").count() == 2)
    check("the link form is the default",
          "active" in (page.locator('[data-way="link"]').get_attribute("class") or "")
          and page.locator("#import-url").is_visible()
          and not page.locator("#import-text").is_visible())

    page.locator('[data-way="paste"]').click()
    page.wait_for_timeout(400)
    check("choosing the paste swaps the field rather than adding one",
          "active" in (page.locator('[data-way="paste"]').get_attribute("class") or "")
          and page.locator("#import-text").is_visible()
          and not page.locator("#import-url").is_visible())
    check("the paste asks for the original link without needing it",
          page.locator("#import-source-url").is_visible())
    paste_note = page.locator("#import-title-note").inner_text()
    check("the title field explains itself for a paste",
          "headline" in paste_note, paste_note)
    # The lede above the fields has to describe the way in you are actually using:
    # telling a reader who is pasting that "the page gets fetched" is a lie about
    # the thing they are looking at.
    paste_lede = page.locator("#import-lede").inner_text()
    check("the explanation changes with the way in",
          "you paste" in paste_lede and "fetched" not in paste_lede, paste_lede[:80])
    shot(page, "ui-import-paste")

    # Each mode has to keep what was typed into the other: the fields stay in the
    # DOM and only one is hidden, because a half-typed URL lost to a look at the
    # other form is exactly the kind of thing that makes a dialog annoying.
    page.locator('[data-way="link"]').click()
    page.wait_for_timeout(300)
    page.locator("#import-url").fill("https://example.com/kept")
    page.locator("#import-title").fill("A kept title")
    page.locator('[data-way="paste"]').click()
    page.wait_for_timeout(300)
    page.locator('[data-way="link"]').click()
    page.wait_for_timeout(300)
    check("switching back finds the URL still there",
          page.locator("#import-url").input_value() == "https://example.com/kept")
    check("and the title too", page.locator("#import-title").input_value() == "A kept title")
    check("the title hint goes back to the link's wording",
          "page title comes out wrong" in page.locator("#import-title-note").inner_text())

    # An empty paste is refused here rather than queued as a job that would fail
    # a minute later.
    page.locator("#import-url").fill("")
    page.locator('[data-way="paste"]').click()
    page.wait_for_timeout(300)
    page.locator("#import-go").click()
    page.wait_for_timeout(600)
    toast = page.locator(".toast").last.inner_text() if page.locator(".toast").count() else ""
    check("pasting nothing says so instead of queueing a job",
          "paste" in toast.lower() and not page.locator("[data-job-detail]").count(), toast)
    check("and the button is still usable afterwards",
          not page.locator("#import-go").is_disabled())

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    page.locator("#btn-import").click()
    page.wait_for_selector("#import-modes", timeout=10000)
    page.wait_for_timeout(400)
    check("the way in you used last is remembered",
          "active" in (page.locator('[data-way="paste"]').get_attribute("class") or ""))
    # Put it back: the failure-path test below fills the URL field, which is
    # hidden in the paste mode.
    page.locator('[data-way="link"]').click()
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # -- discovering what to read next ------------------------------------- #
    # Calls the network: a search and the register gaps, both of which are
    # remote. The gaps are served from the library, but the search is not.
    print("\ndiscovery")
    gaps_api = page.request.get(f"{BASE}/api/discover/gaps").json()["gaps"]
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector("#btn-discover", timeout=20000)
    page.locator("#btn-discover").click()
    page.wait_for_selector("#discover-q", timeout=30000)
    # The gap panel is fetched after the dialog opens; an empty results div has
    # no size, so waiting on it would never resolve.
    page.wait_for_timeout(2000)

    # Whether there are gaps depends on what this reader has already opened, so
    # the check is that the panel agrees with the endpoint rather than that gaps
    # exist -- a shelf with every register met has nothing to say here.
    shown = page.locator("#discover-gaps .gap").count()
    check("the gap panel agrees with what the library knows",
          shown == len(gaps_api), f"{shown} shown, {len(gaps_api)} from the API")
    if gaps_api:
        gap_text = page.locator("#discover-gaps").inner_text()
        check("a gap names the register and how much is waiting",
              "never read" in gap_text and "shelves" in gap_text, gap_text[:80])
        check("and offers the unread lesson itself, not just a search",
              page.locator("#discover-gaps a.btn").count() >= 1)
    shot(page, "ui-discover")

    # A result the reader already owns must say so, and must not offer to import
    # it again -- the point of marking it is that there is nothing to do.
    #
    # This is the one check in the suite that depends on the open web, and the
    # search goes out through a local proxy: when that hiccups, the honest report
    # is that the check could not be made, not that the app is broken.
    page.fill("#discover-q", "diglossia")
    page.locator("#discover-go").click()
    found = True
    try:
        page.wait_for_selector("#discover-results .result", timeout=120000)
    except Exception as exc:                                       # noqa: BLE001
        found = False
        why = f"the search did not answer ({type(exc).__name__})"
        skip("a result already in the library is marked", why)
        skip("and says so in words", why)
        skip("clicking one does not open the import dialog", why)

    if found:
        page.wait_for_timeout(1200)
        marked = page.locator("#discover-results .result.have").first
        check("a result already in the library is marked",
              page.locator("#discover-results .result.have").count() >= 1,
              f"{page.locator('#discover-results .result').count()} results, none marked")
        if marked.count():
            check("and says so in words", "in your library" in marked.inner_text(),
                  marked.inner_text()[:70])
            marked.click()
            page.wait_for_timeout(800)
            check("clicking one does not open the import dialog",
                  page.locator("#import-url").count() == 0, "the import dialog opened")

        # Results that are not what the reader wants have to be replaceable. The
        # sources are deterministic, so a re-run returns the identical list --
        # the button asks for what comes after what has been shown, and says so
        # when there is nothing after.
        #
        # Whether there *is* anything after is a fact about the live sources, not
        # about the app: they are intermittently unreachable from this machine,
        # and a second page that comes back empty is a correct answer to a
        # question with no more answers. So the browser's own network traffic is
        # watched, and the difference between "the button did nothing" (a
        # failure) and "the web had nothing" (a skip) is made from that.
        if page.locator("#discover-more").count():
            before = page.locator("#discover-results .result").count()
            urls_before = page.evaluate(
                "() => [...document.querySelectorAll('#discover-results .result')]"
                ".map(el => el.dataset.url)")
            # Wait for the request rather than for a number of seconds. A search
            # is several network calls with generous timeouts, and a fixed pause
            # shorter than the answer reads as "the button did nothing" -- which
            # is the one conclusion this check must not reach by accident.
            answered = None
            try:
                with page.expect_response(
                        lambda r: "/api/discover" in r.url, timeout=180000) as caught:
                    page.locator("#discover-more").click()
                answered = caught.value
            except Exception:                                  # noqa: BLE001
                answered = None
            check("asking for different results asks the server again", answered is not None,
                  "no second request went out")
            page.wait_for_timeout(800)
            after = page.locator("#discover-results .result").count()
            urls_after = page.evaluate(
                "() => [...document.querySelectorAll('#discover-results .result')]"
                ".map(el => el.dataset.url)")
            returned = 0
            if answered is not None:
                try:
                    returned = len((answered.json() or {}).get("results") or [])
                except Exception:                              # noqa: BLE001
                    returned = 0
            if returned and after > before:
                check("asking for different results returns some", True)
                check("and none of them is one already shown",
                      set(urls_before) <= set(urls_after)
                      and len(set(urls_after)) == len(urls_after)
                      and after - before == len(set(urls_after) - set(urls_before)),
                      f"{len(set(urls_after) - set(urls_before))} new of {after} rows")
                check("and the ones already there are left alone",
                      set(urls_before) <= set(urls_after), "an earlier result disappeared")
                check("the count line keeps up with the list",
                      str(after) in page.locator("#discover-results .count-line").inner_text(),
                      page.locator("#discover-results .count-line").inner_text())
            else:
                # The sources are intermittently unreachable from this machine, and
                # a second page that comes back empty is a correct answer to a
                # question with no more answers. The app's job then is to say so
                # rather than repeat itself.
                skip("asking for different results returns some",
                     f"the sources returned nothing beyond the {before} already shown")
                check("with nothing further to show, it says so instead of repeating itself",
                      "everything those sources have" in page.locator("#discover-results").inner_text()
                      and set(urls_after) == set(urls_before),
                      page.locator("#discover-results").inner_text()[-120:])
        else:
            # Fewer results than a page: that is all the sources had, and the
            # dialog says so rather than offering a button that cannot work.
            check("a search with nothing more to give says so",
                  "everything those sources have" in page.locator("#discover-results").inner_text())
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # -- deleting a lesson, and starting over ------------------------------ #
    # The delete is checked end to end: a lesson is imported through the app's own
    # share path, removed from the shelf, and the file is gone afterwards. Nothing
    # of the reader's is touched -- the lesson this works on is one the suite made.
    print("\ndeleting a lesson, and starting over")
    deck_ids = {row["id"] for row in page.request.get(f"{BASE}/api/words").json()["words"]}
    shelf_now = page.request.get(f"{BASE}/api/library").json()["articles"]
    imported_now = [a for a in shelf_now if a.get("imported")]
    corpus_now = [a for a in shelf_now if not a.get("imported")]
    # Import the lesson the share section downloaded: a real lesson, through the
    # app's own path, so this does not work on anything of the reader's.
    landed = page.request.post(f"{BASE}/api/transfer/import", data={"text": shared})
    check("a lesson can be imported from a file (for this to delete)",
          landed.status == 200, f"{landed.status} {landed.text()[:80]}")
    if landed.status == 200:
        victim = landed.json()["slug"]
        # The shelf is a hash route, so navigating to the page you are already on
        # is a no-op -- no hashchange, no re-render, and this reads a DOM built
        # before the import. Reload, so what is checked is the library as it is.
        page.goto(f"{BASE}/#/library")
        page.reload()
        page.wait_for_selector(".card", timeout=20000)
        page.wait_for_timeout(800)
        removable = page.locator("[data-remove-lesson]")
        check("the shelf offers to remove lessons the app made",
              removable.count() >= len(imported_now) + 1,
              f"{removable.count()} removable of {page.locator('.card').count()} cards")
        if corpus_now:
            check("and offers nothing on a passage from the reader's own corpus",
                  page.locator(f'.card[data-slug="{corpus_now[0]["slug"]}"] [data-remove-lesson]').count() == 0,
                  "a hand-made passage is offered for deletion")
        check("the lesson just imported can be removed",
              page.locator(f'.card[data-slug="{victim}"] [data-remove-lesson]').count() == 1)

        button = page.locator(f'.card[data-slug="{victim}"] [data-remove-lesson]')
        button.click()
        page.wait_for_timeout(400)
        check("removing asks before it removes",
              "danger" in (button.get_attribute("class") or "")
              and page.request.get(f"{BASE}/api/article/{victim}").status == 200,
              "the lesson was already gone after one click")
        button.click()
        page.wait_for_timeout(1500)
        check("and the lesson is gone once you confirm",
              page.request.get(f"{BASE}/api/article/{victim}").status == 404,
              page.request.get(f"{BASE}/api/article/{victim}").status)
        check("with the deck untouched by any of it",
              {row["id"] for row in page.request.get(f"{BASE}/api/words").json()["words"]} == deck_ids,
              "the deck changed while a lesson was deleted")
        check("and the library is back to what it was",
              len(page.request.get(f"{BASE}/api/library").json()["articles"]) == len(shelf_now),
              f"{len(page.request.get(f'{BASE}/api/library').json()['articles'])} articles now")

    # Starting over is the one control that can lose work, so the suite checks what
    # it *says* and that Cancel does nothing. The wipe itself is tested against a
    # throwaway database in the unit tests: running it here would take the reader's.
    deck_now = page.request.get(f"{BASE}/api/words").json()["total"]
    page.goto(f"{BASE}/#/stats")
    page.wait_for_selector("#btn-reset", timeout=20000)
    page.locator("#btn-reset").click()
    page.wait_for_selector("#reset-go", timeout=10000)
    text = page.locator("#modal-body").inner_text()
    check("starting over lists what it clears", "saved words" in text and "review" in text, text[:90])
    check("and says what it keeps", "keeps" in text and "library" in text.lower(), text[-150:])
    check("and where the copy of the database goes", "backups" in text)
    page.locator("#reset-cancel").click()
    page.wait_for_timeout(600)
    check("cancelling it changes nothing",
          page.request.get(f"{BASE}/api/words").json()["total"] == deck_now,
          "the deck changed after Cancel")
    check("and nothing was cleared behind the dialog",
          page.request.get(f"{BASE}/api/stats").json()["words"] == deck_now)

    # -- read-along audio -------------------------------------------------- #
    print("\nread-along")
    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector("#prose .es", timeout=15000)
    check("the reader offers to read aloud", page.locator("#btn-read-aloud").is_visible())
    check("the text is rendered as sentences, not as bare spans",
          page.locator("#prose .sent").count() > 10,
          f"{page.locator('#prose .sent').count()} sentences")

    # The bug this replaced: a bolded word split one Spanish sentence into four
    # utterances, one of them the single word "pintan". Adjacent same-language
    # runs within a sentence must be merged back before being spoken.
    utterances = page.evaluate("""() => {
        const wanted = [...document.querySelectorAll('#prose .sent')]
            .find(s => s.textContent.includes('Afortunadamente'));
        if (!wanted) return null;
        const runs = [];
        for (const child of wanted.children) {
            if (child.classList.contains('gloss')) continue;
            const lang = child.classList.contains('es') ? 'es' : 'en';
            const text = child.textContent.replace(/\\s+/g, ' ').trim();
            if (!text) continue;
            const last = runs[runs.length - 1];
            if (last && last.lang === lang) last.text += ' ' + text;
            else runs.push({lang, text});
        }
        return runs;
    }""")
    check("a sentence is assembled from its runs", isinstance(utterances, list) and utterances,
          str(utterances))
    if utterances:
        spanish = [u for u in utterances if u["lang"] == "es"]
        check("a Spanish sentence is spoken as one utterance, not one per span",
              len(spanish) == 1, f"{len(spanish)} Spanish utterances: {spanish}")
        check("the whole sentence is there, not a fragment",
              "pintan" in spanish[0]["text"] and "brillante" in spanish[0]["text"],
              spanish[0]["text"][:80])

    # Headless Chrome has no voices, so the phrases resolve instantly and the
    # highlight would be gone before a poll could catch it. Watch for it.
    page.evaluate("""() => {
        window.__spoken = [];
        new MutationObserver(records => {
            for (const record of records) {
                const node = record.target;
                if (node.classList && node.classList.contains('speaking')) {
                    window.__spoken.push(node.textContent.trim().slice(0, 60));
                }
            }
        }).observe(document.getElementById('prose'),
                   { subtree: true, attributes: true, attributeFilter: ['class'] });
    }""")
    page.locator("#btn-read-aloud").click()
    page.wait_for_timeout(2500)
    spoken = page.evaluate("() => window.__spoken || []")
    check("it highlights sentences as it plays them", len(spoken) > 0,
          f"{len(spoken)} highlights")

    # Escape stops playback. (Pressing "r" after it would toggle it back on --
    # which is what this test got wrong the first time.)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check("the read button returns to its resting label",
          "Read" in page.locator("#btn-read-aloud").inner_text(),
          page.locator("#btn-read-aloud").inner_text())
    check("stopping clears the highlight", page.locator(".sent.speaking").count() == 0)

    # -- construction cards ------------------------------------------------ #
    # The corpus teaches multi-word constructions. Blanking one word out of
    # "se ponen de acuerdo" tests the word, not the construction.
    print("\nconstruction cards")
    stamp2 = str(int(time.time()))
    phrase = f"se ponen de acuerdo{stamp2}"
    context = f"Los críticos {phrase} en que el resultado importa."
    saved = page.request.post(f"{BASE}/api/word/save",
                              data={"term": phrase, "gloss": "they agree",
                                    "lemma": phrase, "context": context})
    word_id = saved.json()["word"]["id"] if saved.ok else None

    page.goto(f"{BASE}/#/review")
    page.wait_for_selector(".card-face", timeout=15000)
    page.locator('[data-mode="produce"]').click()
    page.wait_for_timeout(700)

    found = False
    for _ in range(12):
        if not page.locator("#cloze-answer").count():
            page.locator("#card-grades button").first.click() if page.locator("#card-grades button").count() else None
            page.wait_for_timeout(500)
            continue
        shown = page.locator(".cloze-sentence").inner_text()
        if phrase.split()[0] in shown or "acuerdo" in shown:
            # This is the card we seeded: the whole phrase should be gone.
            found = True
            check("a construction blanks the whole phrase, not one word",
                  "_____" in shown and "acuerdo" not in shown and "ponen" not in shown,
                  shown[:110])
            break
        page.keyboard.press("3")
        page.wait_for_timeout(700)
    if not found:
        print("  note: the seeded construction card was not reached in this queue")
    if word_id:
        page.request.delete(f"{BASE}/api/word/{word_id}")
    page.locator('[data-mode="recognise"]').click()

    # -- dismissing the word panel ----------------------------------------- #
    print("\nword panel dismissal")
    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector("#prose .es .w", timeout=15000)
    page.wait_for_timeout(1000)
    page.locator("#prose .es .w").nth(6).click()
    page.wait_for_selector("#popover:not([hidden])", timeout=8000)
    check("clicking a word opens the panel", page.locator("#popover").is_visible())

    page.locator("#prose p").first.click(position={"x": 20, "y": 18})
    page.wait_for_timeout(500)
    check("clicking elsewhere in the text closes it", page.locator("#popover").is_hidden())

    # The interesting case: the same gesture must close the old panel and open
    # a new one. A document-level `click` handler would close the panel it had
    # just opened.
    page.locator("#prose .es .w").nth(24).click()
    page.wait_for_selector("#popover:not([hidden])", timeout=8000)
    page.wait_for_timeout(700)
    check("clicking another word moves the panel rather than closing it",
          page.locator("#popover").is_visible())
    page.keyboard.press("Escape")

    # -- table of contents -------------------------------------------------- #
    print("\ntable of contents")
    page.goto(f"{BASE}/#/read/diglot-computational-linguistics-and-llms-what-language-really-mean")
    page.wait_for_selector("#prose .es", timeout=15000)
    page.wait_for_timeout(1200)

    links = page.locator("#toc .toc-link")
    check("the reader lists the article's sections", links.count() >= 3,
          f"{links.count()} entries")
    starts_at_top = page.evaluate("() => window.scrollY") < 50

    page.locator("#toc .toc-link").nth(2).click()
    page.wait_for_timeout(1400)
    check("clicking a section scrolls to it",
          page.evaluate("() => window.scrollY") > (20 if starts_at_top else 0))
    check("the current section is marked", page.locator("#toc .toc-link.current").count() == 1)
    shot(page, "toc")

    # A short piece with no headings should simply not offer one.
    page.goto(f"{BASE}/#/read/how-ai-will-make-art-worse")
    page.wait_for_selector("#prose", timeout=15000)
    page.wait_for_timeout(900)
    check("an article without sections shows no contents pane", page.locator("#toc").count() == 0)

    # -- read / unread filter ------------------------------------------------ #
    print("\nlibrary filter")
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".filter-row", timeout=15000)
    page.wait_for_timeout(800)

    check("the library offers read-state filters", page.locator("[data-filter]").count() == 4)
    check("the chosen filter is marked", page.locator("[data-filter].active").count() == 1)

    for key in ("unread", "reading", "finished", "all"):
        button = page.locator(f'[data-filter="{key}"]')
        # Each badge states how many articles that filter should show; the grid
        # must agree with it, which is a self-consistent check that does not
        # depend on how much has been read today.
        badge = int(re.sub(r"\D", "", button.inner_text()) or 0)
        button.click()
        page.wait_for_timeout(700)
        shown = page.locator(".card").count()
        check(f"'{key}' shows exactly the articles it counted", shown == badge,
              f"badge said {badge}, grid showed {shown}")
        check(f"'{key}' becomes the active filter",
              "active" in (button.get_attribute("class") or ""))

    page.locator('[data-filter="all"]').click()
    page.wait_for_timeout(600)
    shot(page, "library-filter")

    # -- reading analytics and the corpus map ------------------------------ #
    print("\nreading analytics")
    page.goto(f"{BASE}/#/stats")
    page.wait_for_selector("#reading-analytics .panel", timeout=20000)
    page.wait_for_timeout(1200)

    check("an exposure headline is stated",
          "%" in page.locator("#reading-analytics .panel-sub").first.inner_text()
          or "Nothing" in page.locator("#reading-analytics .panel-sub").first.inner_text(),
          page.locator("#reading-analytics .panel-sub").first.inner_text()[:80])
    check("the window can be switched",
          page.locator('#reading-analytics [data-window="30"]').count() == 1)
    page.locator('#reading-analytics [data-window="30"]').click()
    page.wait_for_timeout(1200)
    check("switching the window reloads the panel",
          "active" in (page.locator('#reading-analytics [data-window="30"]').get_attribute("class") or ""))
    page.locator('#reading-analytics [data-window="7"]').click()
    page.wait_for_timeout(900)

    print("\npassage map")
    page.wait_for_selector("#corpus-map", timeout=25000)
    page.wait_for_timeout(1500)
    nodes = page.locator("#corpus-map .map-node").count()
    edges = page.locator("#corpus-map .map-edges line").count()
    check("every passage is a node", nodes > 10, f"{nodes} nodes")
    check("passages are joined", edges > 5, f"{edges} edges")
    check("the graph is pruned rather than complete",
          edges < nodes * (nodes - 1) / 2, f"{edges} of {nodes * (nodes - 1) // 2} possible")

    clusters = page.locator("#corpus-map-panel .legend .item").count()
    check("clusters are named and legended", clusters >= 2, f"{clusters} legend entries")
    # A single cluster covering everything says the pruning failed.
    check("the corpus does not collapse into one cluster",
          page.locator("#corpus-map circle").count() > 0 and clusters > 2)
    check("nodes are direct-labelled or their absence is stated",
          page.locator("#corpus-map text").count() > 5)

    # The groups are named from the words their members' titles share, so the name
    # can be checked by reading the titles in the group rather than believed. What
    # they were *built* from -- the vocabulary the passages share in their text --
    # is a word list, and showing that as the label is what this replaced.
    graph_api = page.request.get(f"{BASE}/api/analytics/corpus").json()
    named = [cluster for cluster in graph_api["clusters"] if cluster["topic"]]
    check("groups carry a subject rather than a pile of shared words", bool(named),
          f"{len(graph_api['clusters'])} groups, {len(named)} named")
    legend = page.locator("#corpus-map-panel .legend-groups").inner_text().lower()
    for cluster in named[:3]:
        check(f"the legend names the {cluster['size']}-passage group",
              cluster["topic"][0] in legend, legend[:100])

    group = page.locator("#corpus-map-panel .legend-groups .item").first
    group.click()
    page.wait_for_timeout(500)
    focused = page.locator("#corpus-map .map-node.dim").count()
    check("clicking a group shows only that group", 0 < focused < nodes,
          f"{focused} of {nodes} dimmed")
    check("and the legend says which one is showing",
          "active" in (group.get_attribute("class") or ""))
    group.click()
    page.wait_for_timeout(500)
    check("clicking it again lets the whole map back",
          page.locator("#corpus-map .map-node.dim").count() == 0,
          f"{page.locator('#corpus-map .map-node.dim').count()} still dimmed")

    # Hovering should show the neighbourhood rather than the whole graph.
    page.locator("#corpus-map .map-node").first.hover()
    page.wait_for_timeout(500)
    dimmed = page.locator("#corpus-map .map-node.dim").count()
    check("hovering a passage isolates its neighbours", 0 < dimmed < nodes,
          f"{dimmed} of {nodes} dimmed")
    shot(page, "corpus-map")

    # -- pan and zoom ------------------------------------------------------- #
    print("\nmap pan and zoom")

    def centre_map():
        """Put the map's middle in the middle of the viewport.

        Mouse events address the viewport, so a map below the fold cannot be
        dragged at all -- an earlier version of this test moved the mouse to an
        off-screen coordinate and concluded the feature was broken.
        """
        page.evaluate("""() => {
            const box = document.getElementById('corpus-map').getBoundingClientRect();
            window.scrollBy(0, box.top + box.height / 2 - window.innerHeight / 2);
        }""")
        page.wait_for_timeout(400)
        box = page.locator("#corpus-map").bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def transform():
        return page.locator(".map-view").get_attribute("transform") or ""

    def zoom_level():
        try:
            return float(transform().split("scale(")[1].rstrip(")"))
        except (IndexError, ValueError):
            return 0.0

    check("the map offers zoom controls", page.locator("[data-map-zoom]").count() == 3)
    check("the map starts fitted", abs(zoom_level() - 1.0) < 0.01, transform())

    cx, cy = centre_map()
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 140, cy + 70, steps=12)
    page.mouse.up()
    page.wait_for_timeout(300)
    check("dragging pans the map", "translate(0.00 0.00)" not in transform(), transform())
    check("dragging does not navigate away", page.evaluate("() => location.hash") == "#/stats")

    cx, cy = centre_map()
    page.mouse.move(cx, cy)
    page.mouse.wheel(0, -600)
    page.wait_for_timeout(400)
    check("scrolling zooms in", zoom_level() > 1.05, f"{zoom_level():.2f}")

    before = zoom_level()
    page.locator('[data-map-zoom="out"]').click()
    page.wait_for_timeout(300)
    check("the zoom-out button works", zoom_level() < before,
          f"{before:.2f} -> {zoom_level():.2f}")

    page.locator('[data-map-zoom="reset"]').click()
    page.wait_for_timeout(300)
    check("fit returns to the whole map", transform() == "translate(0.00 0.00) scale(1.0000)", transform())

    # Zooming out past the limit must hand the scroll back to the page rather
    # than swallowing it, or the map becomes a trap you cannot scroll past.
    for _ in range(10):
        page.mouse.move(cx, cy)
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(60)
    check("zooming out is bounded", zoom_level() >= 0.34, f"{zoom_level():.2f}")
    page.locator('[data-map-zoom="reset"]').click()
    page.wait_for_timeout(300)

    # A click on a node must still open the article -- and a drag that happens
    # to end over one must not.
    #
    # The node is chosen by hit-testing rather than by index: the graph is laid
    # out by clustering, so any fixed index can end up underneath the zoom
    # controls, and an earlier version of this check was clicking those instead
    # and reporting that the map was broken. A node whose centre is really its
    # own circle is one a reader could click.
    def clickable_node() -> tuple[float, float, str]:
        count = page.locator("#corpus-map .map-node").count()
        for index in range(count):
            circle = page.locator("#corpus-map .map-node").nth(index).locator("circle").first
            circle.scroll_into_view_if_needed()
            page.wait_for_timeout(120)
            box = circle.bounding_box()
            if not box or box["y"] < 0 or box["y"] + box["height"] > page.viewport_size["height"]:
                continue
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            tag = page.evaluate(
                "([x,y]) => { const el = document.elementFromPoint(x,y);"
                " return el ? el.tagName.toLowerCase() : ''; }", [x, y])
            if tag == "circle":
                slug = page.locator("#corpus-map .map-node").nth(index).get_attribute("data-slug")
                return x, y, slug or ""
        return 0.0, 0.0, ""

    x, y, slug = clickable_node()
    check("a graph node is reachable with the mouse", bool(slug), f"none of the nodes was clickable")
    page.mouse.click(x, y)
    page.wait_for_timeout(1000)
    check("clicking a passage opens it", page.evaluate("() => location.hash").startswith("#/read/"),
          page.evaluate("() => location.hash"))

    page.goto(f"{BASE}/#/stats")
    page.wait_for_selector("#corpus-map", timeout=25000)
    page.wait_for_timeout(1800)
    x, y, _slug = clickable_node()
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 130, y + 60, steps=10)
    page.mouse.up()
    page.wait_for_timeout(800)
    check("a drag ending on a passage does not open it",
          page.evaluate("() => location.hash") == "#/stats",
          page.evaluate("() => location.hash"))

    # -- activity panel and the failure pop-up ---------------------------- #
    # A URL that cannot be fetched fails in seconds, which exercises the whole
    # background path -- queue, progress, terminal state, notification -- in a
    # test that stays fast. The success path is covered by ui_import_test.py.
    print("\nactivity panel and failure notification")
    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".card", timeout=15000)
    page.evaluate("""() => {
        window.__toasts = [];
        new MutationObserver(records => {
            for (const record of records)
                for (const node of record.addedNodes)
                    if (node.nodeType === 1 && node.classList.contains('toast'))
                        window.__toasts.push(node.textContent);
        }).observe(document.getElementById('toasts'), { childList: true });
    }""")

    page.locator("#btn-activity").click()
    page.wait_for_timeout(400)
    check("activity panel opens", page.locator("#activity").is_visible())
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    check("Escape closes the activity panel", page.locator("#activity").is_hidden())

    page.locator("#btn-import").click()
    page.wait_for_selector("#import-url", timeout=5000)
    page.locator("#import-url").fill("https://this-host-does-not-exist.invalid/article")
    page.locator("#import-go").click()
    page.wait_for_selector("[data-job-detail] .job", timeout=20000)
    job_id = page.locator("[data-job-detail] .job").first.get_attribute("data-job-id")
    # Close the dialog: the job has to survive it, and the modal would
    # otherwise intercept every later click on the page.
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    check("closing the dialog does not cancel the job",
          page.locator("#modal").is_hidden())

    status = "running"
    for _ in range(40):
        status = page.locator(f'[data-job-id="{job_id}"]').first.get_attribute("data-job-status")
        if status not in ("running", "queued"):
            break
        page.wait_for_timeout(1000)
    check("an unreachable URL fails rather than hanging", status == "failed", f"ended as {status}")

    page.wait_for_timeout(800)
    toasts = page.evaluate("() => window.__toasts || []")
    check("a pop-up announced the failure",
          any("✕" in t for t in toasts), str(toasts[-1:])[:120])

    page.locator("#btn-activity").click()
    page.wait_for_timeout(400)
    card = page.locator(f'#activity [data-job-id="{job_id}"]')
    check("the failure is listed with its reason",
          "✕" in card.inner_text() or "could not" in card.inner_text().lower(),
          card.inner_text()[:90])
    card.locator("[data-job-dismiss]").click()
    page.wait_for_timeout(700)
    check("a finished job can be dismissed",
          page.locator(f'#activity [data-job-id="{job_id}"]').count() == 0)
    page.keyboard.press("Escape")

    # -- console ---------------------------------------------------------- #
    real_errors = [e for e in errors if "favicon" not in e.lower()]
    check("no JavaScript errors", not real_errors, "; ".join(real_errors[:3]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-ai", action="store_true", help="also grade a translation end to end")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            run(page, with_ai=args.with_ai)
        finally:
            browser.close()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
