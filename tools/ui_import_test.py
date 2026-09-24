"""End-to-end test of the background import: queue it, walk away, get told.

The point of this test is the thing that is easy to get wrong and impossible to
see in a screenshot: that the work survives closing the dialog, that progress
keeps updating somewhere the user can find it later, and that a pop-up appears
wherever they are when it finishes.

It is slow (a real article, really woven, several minutes) and needs the
network, so it is kept out of the fast suite.

    python tools/ui_import_test.py
    python tools/ui_import_test.py --url https://example.com/article
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "shots"
BASE = "http://127.0.0.1:8787"

# Short enough to finish in a few minutes, long enough to have several chunks.
DEFAULT_URL = "https://en.wikipedia.org/wiki/Extensive_reading"

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name} — {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + ("" if condition else f"  {detail}"))


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def run(page: Page, url: str, timeout: float) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(f"{BASE}/#/library")
    page.wait_for_selector(".card", timeout=20000)
    articles_before = page.locator(".card").count()

    # Toasts live for a few seconds and the test polls every few seconds, so
    # watching for one by polling would race it. Record them as they appear.
    page.evaluate("""() => {
        window.__toasts = [];
        new MutationObserver(records => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node.nodeType === 1 && node.classList.contains('toast')) {
                        window.__toasts.push(node.textContent);
                    }
                }
            }
        }).observe(document.getElementById('toasts'), { childList: true });
    }""")

    # -- queue it --------------------------------------------------------- #
    print("\nqueue an import")
    page.locator("#btn-import").click()
    page.wait_for_selector("#import-url", timeout=5000)
    page.locator("#import-url").fill(url)
    page.locator("#import-go").click()

    page.wait_for_selector("[data-job-detail]", timeout=20000)
    page.wait_for_selector(".job.running, .job.queued", timeout=30000)
    print(f"  job detail rendered: {page.locator('[data-job-detail] .job-title').inner_text()[:60]!r}")
    check("dialog shows its own job", page.locator("[data-job-detail] .job").count() == 1)
    check("job shows a title, not a raw URL",
          not page.locator("[data-job-detail] .job-title").inner_text().startswith("http"),
          page.locator("[data-job-detail] .job-title").inner_text()[:60])
    check("job reports running or queued",
          page.locator("[data-job-detail] .job.running, [data-job-detail] .job.queued").count() == 1)

    job_id = page.locator("[data-job-detail] .job").first.get_attribute("data-job-id")
    check("the job has a stable id to follow", bool(job_id), str(job_id))
    shot(page, "import-job")

    # -- close the dialog: the work must continue -------------------------- #
    print("\nclose the dialog and check it is still running")
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    check("dialog closed", page.locator("#modal").is_hidden())

    page.locator("#btn-activity").click()
    page.wait_for_selector(f'#activity [data-job-id="{job_id}"]', timeout=10000)
    check("activity panel lists the job after the dialog is gone",
          page.locator(f'#activity [data-job-id="{job_id}"]').count() == 1)
    check("activity badge shows active work", page.locator("#job-pill").is_visible())

    card = page.locator(f'#activity [data-job-id="{job_id}"]')
    first = card.inner_text()
    page.wait_for_timeout(14000)
    check("progress keeps moving while the dialog is closed",
          card.inner_text() != first or card.get_attribute("data-job-status") == "done",
          f"still {card.inner_text()[:70]!r}")
    shot(page, "activity-running")

    # -- wait for it ------------------------------------------------------ #
    print(f"\nwaiting up to {timeout:.0f}s for the weave to finish")
    started = time.time()
    status = "running"
    while time.time() - started < timeout:
        status = card.get_attribute("data-job-status") or "gone"
        if status not in ("running", "queued"):
            break
        page.wait_for_timeout(3000)

    check("the job reached a terminal state", status not in ("running", "queued"), f"still {status}")
    if status in ("running", "queued"):
        return
    shot(page, "activity-finished")

    # -- told about it ---------------------------------------------------- #
    print("\nnotification")
    page.wait_for_timeout(1500)
    toasts = page.evaluate("() => window.__toasts || []")
    toast_text = toasts[-1] if toasts else ""
    check("a pop-up announced the result", bool(toast_text), "no toast was raised")
    if toast_text:
        print(f"  toast: {toast_text[:110]!r}")
    check("the pop-up says which way it went", ("✓" in toast_text) or ("✕" in toast_text),
          toast_text[:80])
    check("the pop-up agrees with the job's outcome",
          ("✓" in toast_text) == (status == "done"), f"job {status}, toast {toast_text[:50]!r}")

    succeeded = status == "done"
    if succeeded:
        check("a finished import offers to open the article",
              page.locator("[data-job-open]").count() >= 1)

        page.goto(f"{BASE}/#/library")
        page.wait_for_selector(".card", timeout=20000)
        page.wait_for_timeout(1200)
        slug = page.locator("[data-job-open]").first.get_attribute("data-job-open")
        articles_after = page.locator(".card").count()
        # Re-importing the same URL replaces the article rather than adding a
        # second copy, so the count may not change. What must be true is that
        # the article is on the shelf.
        on_shelf = page.locator(f'.card[data-slug="{slug}"]').count() == 1
        check("the article is on the shelf", on_shelf,
              f"{articles_before} -> {articles_after}, looking for {slug}")

        # Navigate straight to the slug rather than clicking the button: the
        # Activity panel closed when we changed views, so the button it holds is
        # real but not visible. Clicking it is covered by the fast suite.
        page.goto(f"{BASE}/#/read/{slug}")
        page.wait_for_selector("#prose .es", timeout=20000)
        check("the imported lesson is readable", page.locator("#prose .es").count() > 10)
        check("its Spanish is glossed", page.locator("#prose .gloss").count() > 0,
              f"{page.locator('#prose .gloss').count()} glosses")
        check("the lesson has post-reading anchors",
              page.locator(".rail .grammar-note").count() > 0)
        shot(page, "import-result")
    else:
        # The job appears twice now -- once in the dialog, once in the panel --
        # so scope the lookup to the panel rather than tripping strict mode.
        page.locator("#btn-activity").click() if page.locator("#activity").is_hidden() else None
        page.wait_for_timeout(300)
        step = page.locator(f'#activity [data-job-id="{job_id}"] .job-step').first
        print(f"  (the import ended as {status}: {step.inner_text()[:140]!r})")

    check("no JavaScript errors", not errors, "; ".join(errors[:2]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=1500)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            run(page, args.url, args.timeout)
        finally:
            browser.close()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
