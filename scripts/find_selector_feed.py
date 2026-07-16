#!/usr/bin/env python3
"""
find_selector_feed.py  -  discover the ST MCU Product Selector data feed
=========================================================================

The selector page is a JavaScript app; the part table is filled by a JSON
request made in the background. This script loads the page in a headless
browser, watches every network response, scores the ones that look like the
parametric part feed, and writes the winner to:

    selector_feed.url            <- the bare URL (what stm32_ripper.py needs)
    selector_feed.headers.json   <- method + request headers (+ POST body),
                                     handy for reproducing the call in the ripper

Usage
-----
    pip install playwright
    python -m playwright install chromium      # one-time browser download

    python3 find_selector_feed.py              # headless, auto-detect
    python3 find_selector_feed.py --headful    # watch it work in a window
    python3 find_selector_feed.py --all        # list every JSON candidate, ranked
    python3 find_selector_feed.py --no-browser # best-effort static scan, no Playwright

Why a browser: the feed URL is built and called by ST's JavaScript at runtime
and ST rotates it, so the only reliable way to learn the *current* URL is to
observe the page actually fetching it.
"""

import argparse
import json
import logging
import os
import re
import sys

log = logging.getLogger("find_selector_feed")

PAGE = "https://www.st.com/content/st_com/en/stm32-mcu-product-selector.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (scripts/ is one down)
URL_OUT = os.path.join(ROOT, "selector_feed.url")
HDR_OUT = os.path.join(ROOT, "selector_feed.headers.json")

# headers worth carrying over to the ripper
KEEP_HEADERS = ("user-agent", "accept", "accept-language",
                "referer", "cookie", "content-type", "x-requested-with")

# A real parametric feed lists hundreds of orderable parts. Anything below this
# is almost certainly a nav menu / search box / suggestion endpoint that merely
# mentions a few part names, so we refuse to treat it as the feed.
MIN_PARTS = 20

# Match real orderable part numbers (STM32 + family letter(s) + digit + tail),
# e.g. STM32F407VG, STM32H743ZI, STM32WB55, STM32U585. Excludes bare "STM32MCU"
# / "STM32-something" slug noise that pads menu JSON.
PART_RE = re.compile(r"STM32[A-Z]{1,2}\d[0-9A-Z]*")

# Parametric column hints we expect in a genuine spec feed.
COL_HINTS = ("flash", "ram", "frequency", "package", "core", "price", "rpn",
             "current", "io")


# --------------------------------------------------------------------------
def score_body(text):
    """How much does this response body look like the STM32 part catalogue?
    Returns (score, distinct_part_count, column_hint_count)."""
    if not text:
        return 0, 0, 0
    # Distinct part numbers, not raw hits: a menu repeats "STM32" in slugs and
    # would otherwise inflate the count without being a real catalogue.
    n_parts = len(set(PART_RE.findall(text)))
    s = n_parts                                   # part numbers are the strongest signal
    if n_parts >= 5:
        s += 50
    col_hits = sum(1 for kw in COL_HINTS if kw in text.lower())
    s += col_hits * 3
    s += min(len(text) // 5000, 20)               # bigger payloads, mild bonus
    return s, n_parts, col_hits


def score_url(url):
    s = 0
    u = url.lower()
    for kw in ("selector", "parametric", "cpn", "product", "mcu", "stm32"):
        if kw in u:
            s += 4
    if u.endswith(".json") or "json" in u:
        s += 4
    # Static-asset extensions: match at the path end so ".js" doesn't fire on
    # ".json". Strip query/fragment before testing.
    path = u.split("?", 1)[0].split("#", 1)[0]
    for ext in (".js", ".css", ".svg", ".png", ".woff", ".woff2", ".gif", ".jpg"):
        if path.endswith(ext):
            s -= 30
    # de-prioritise obvious non-feeds: trackers, and — crucially — the site
    # navigation / search / suggestion endpoints that merely name a few parts.
    for bad in ("analytics", "gtm", "google", "cookie", "consent", "font",
                "image", "menu", "top_menu", "navigation", "/nav", "search",
                "suggest", "autocomplete", "header", "footer", "breadcrumb"):
        if bad in u:
            s -= 30
    return s


def qualifies(cand):
    """A candidate is the real feed only if it lists a catalogue's worth of
    distinct parts *and* carries parametric columns. This is the gate that
    keeps the nav-menu JSON (few parts, no spec columns) from being chosen."""
    return cand["n_parts"] >= MIN_PARTS and cand["col_hits"] >= 2


def write_results(best, candidates):
    url = best["url"]
    with open(URL_OUT, "w", encoding="utf-8") as f:
        f.write(url + "\n")
    sidecar = {
        "url": url,
        "method": best.get("method", "GET"),
        "headers": best.get("headers", {}),
    }
    if best.get("post_data"):
        sidecar["post_data"] = best["post_data"]
    with open(HDR_OUT, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2, ensure_ascii=False)

    log.info("Wrote %s and %s.", URL_OUT, HDR_OUT)
    print(f"\n  Best feed found ({best['n_parts']} distinct STM32 part numbers, "
          f"{best['col_hits']} parametric columns):")
    print(f"    {best.get('method','GET')}  {url}")
    print(f"\n  Wrote {URL_OUT} and {HDR_OUT}.")
    if best.get("method") == "POST":
        print("  NOTE: this is a POST request - copy 'post_data' from "
              f"{HDR_OUT} into the ripper and use requests.post().")


# --------------------------------------------------------------------------
def via_browser(args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright not installed.  Run:\n"
                 "    pip install playwright && python -m playwright install chromium\n"
                 "(or use --no-browser for a best-effort static scan).")

    captured = []   # list of dicts: url, method, headers, post_data, score, n_parts

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful)
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()

        def on_response(resp):
            try:
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" not in ctype and "javascript" not in ctype:
                    # still allow no-content-type feeds, but skip obvious assets
                    if any(resp.url.lower().endswith(e) for e in
                           (".js", ".css", ".png", ".svg", ".woff", ".woff2", ".gif", ".jpg")):
                        return
                try:
                    body = resp.text()
                except Exception:
                    return
                bscore, nparts, col_hits = score_body(body)
                if nparts == 0:
                    return
                uscore = score_url(resp.url)
                req = resp.request
                cand = {
                    "url": resp.url,
                    "method": req.method,
                    "headers": {k: v for k, v in req.headers.items()
                                if k.lower() in KEEP_HEADERS},
                    "post_data": req.post_data,
                    "score": bscore + uscore,
                    "n_parts": nparts,
                    "col_hits": col_hits,
                }
                captured.append(cand)
                log.debug("Candidate: score=%d parts=%d cols=%d urlscore=%d "
                          "qualifies=%s %s %s", cand["score"], nparts, col_hits,
                          uscore, qualifies(cand), req.method, resp.url)
            except Exception:
                pass

        page.on("response", on_response)

        log.info("Loading %s ...", PAGE)
        print(f"  Loading {PAGE} ...")
        page.goto(PAGE, wait_until="domcontentloaded", timeout=args.timeout * 1000)

        # dismiss the OneTrust cookie banner if present (it can block the fetch)
        for sel in ("#onetrust-accept-btn-handler",
                    "button:has-text('Accept')",
                    "button:has-text('I ACCEPT')"):
            try:
                page.click(sel, timeout=2500)
                log.info("Dismissed cookie banner (%s).", sel)
                break
            except Exception:
                pass

        # let the table's data request fire; nudge a filter to force a (re)load
        try:
            page.wait_for_load_state("networkidle", timeout=args.timeout * 1000)
        except Exception:
            log.debug("networkidle not reached within timeout; continuing.")
        try:
            box = page.query_selector("input[type=checkbox]")
            if box:
                box.click(timeout=2000)
                log.info("Toggled a filter checkbox to force a table reload.")
                page.wait_for_timeout(3000)
            else:
                log.debug("No filter checkbox found to nudge.")
        except Exception:
            log.debug("Filter nudge failed; continuing.")
        page.wait_for_timeout(args.wait * 1000)

        browser.close()

    log.info("Captured %d JSON response(s) mentioning STM32 parts.", len(captured))
    if not captured:
        sys.exit("\n  No JSON response containing STM32 part numbers was seen.\n"
                 "  Re-run with --headful to watch, raise --wait, or check that the\n"
                 "  page region you're in actually serves the selector.")

    # collapse duplicate URLs, keep highest score
    best_by_url = {}
    for c in captured:
        cur = best_by_url.get(c["url"])
        if cur is None or c["score"] > cur["score"]:
            best_by_url[c["url"]] = c
    ranked = sorted(best_by_url.values(), key=lambda c: c["score"], reverse=True)

    if args.all:
        print(f"\n  {len(ranked)} JSON candidate(s), ranked:")
        for c in ranked:
            print(f"    {'OK ' if qualifies(c) else '   '}score {c['score']:>4}  "
                  f"parts {c['n_parts']:>4}  cols {c['col_hits']}  "
                  f"{c['method']:4} {c['url']}")

    # Only accept a candidate that clears the catalogue bar. Picking ranked[0]
    # unconditionally is exactly what wrote the nav-menu URL last time.
    good = [c for c in ranked if qualifies(c)]
    if not good:
        top = ranked[0]
        log.error("No candidate qualified as the parametric feed "
                  "(need >=%d distinct parts and >=2 spec columns).", MIN_PARTS)
        sys.exit(
            f"\n  Best candidate was weak ({top['n_parts']} distinct parts, "
            f"{top['col_hits']} spec columns):\n    {top['method']} {top['url']}\n"
            "  That looks like a menu/search endpoint, not the catalogue. "
            "Refusing to overwrite\n  the feed files. Re-run with --headful "
            "--all to watch and inspect every candidate,\n  or raise --wait to "
            "give the table's request more time to fire.")

    if len(good) > 1:
        log.warning("%d candidates qualified; choosing the highest-scoring one.",
                    len(good))
    write_results(good[0], good)


# --------------------------------------------------------------------------
def via_static():
    """Browserless fallback: fetch the page + its scripts and grep for URLs
    that reference part data. Unreliable (the real URL is often assembled at
    runtime) - use --headful browser mode if this finds nothing useful."""
    import requests
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    log.info("Static scan: fetching %s", PAGE)
    print(f"  Fetching {PAGE} (static scan) ...")
    html = sess.get(PAGE, timeout=(15, 60)).text

    blobs = [html]
    scripts = re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', html)
    log.info("Found %d <script src> references; fetching them.", len(scripts))
    for m in scripts:
        u = m if m.startswith("http") else ("https://www.st.com" + m if m.startswith("/") else None)
        if not u:
            continue
        try:
            blobs.append(sess.get(u, timeout=(15, 30)).text)
            log.debug("Fetched script %s", u)
        except Exception as e:
            log.debug("Failed to fetch script %s: %s", u, e)

    found = set()
    for b in blobs:
        for m in re.findall(r'https?://[^\s"\'<>]+', b):
            if re.search(r'(selector|parametric|cpn|product).*?(json|data)', m, re.I):
                found.add(m)
        for m in re.findall(r'["\'](/[^"\']*?(?:selector|parametric|cpn)[^"\']*?)["\']', b, re.I):
            found.add("https://www.st.com" + m)
    if not found:
        sys.exit("  Static scan found no candidate URLs. Use the browser mode "
                 "(drop --no-browser).")
    log.info("Static scan found %d candidate URL(s).", len(found))
    ranked = sorted(found, key=score_url, reverse=True)
    print("\n  Candidate URLs (static scan, verify before trusting):")
    for u in ranked:
        print("    " + u)
    with open(URL_OUT, "w", encoding="utf-8") as f:
        f.write(ranked[0] + "\n")
    print(f"\n  Wrote best guess to {URL_OUT} - confirm it returns part JSON.")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Discover the ST MCU selector data feed URL.")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    ap.add_argument("--all", action="store_true", help="print all ranked JSON candidates")
    ap.add_argument("--no-browser", action="store_true", help="static scan fallback (no Playwright)")
    ap.add_argument("--timeout", type=int, default=45, help="per-step timeout, seconds (default 45)")
    ap.add_argument("--wait", type=int, default=5, help="extra settle time after load, seconds (default 5)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="verbose (DEBUG) logging, incl. every scored candidate")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    if args.no_browser:
        via_static()
    else:
        via_browser(args)


if __name__ == "__main__":
    main()
