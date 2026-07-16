#!/usr/bin/env python3
"""
stm32_ripper.py  -  STM32 catalogue ripper for the Parametric Explorer
======================================================================

Run this manually (or from cron, ~weekly) to regenerate `stm32_data.json`,
the dataset consumed by stm32-explorer.html (Load dataset -> .json).

WHERE THE DATA COMES FROM
-------------------------
The authoritative parametric catalogue is ST's own MCU Product Selector:

    https://www.st.com/content/st_com/en/stm32-mcu-product-selector.html

It is the *only* single source that carries all six explorer axes for every
orderable part in one parametric table:

    Speed (max CPU MHz) · Power (typ. run current) · Price (budgetary USD)
    Flash · RAM · Max I/O      + package + core/generation.

The page is a JavaScript app: the table is filled from a JSON feed fetched in
the background. This script talks to that feed directly.

  !!! ONE-TIME SETUP: confirm the live endpoint !!!
  ST changes the feed URL periodically and fronts it with Akamai, so rather
  than hard-coding a URL that may rot, capture the current one yourself:
    1. Open the selector page above in Chrome/Firefox.
    2. Open DevTools -> Network -> filter "XHR/Fetch".
    3. Change any filter (e.g. tick a core) so the table reloads.
    4. Find the request returning JSON with the part rows (look for a large
       response containing "STM32..." part numbers). Right-click -> Copy URL.
    5. Paste it into SELECTOR_FEED_URL below, and copy the request headers
       (User-Agent, Accept, Referer, any cookies) into HEADERS.
  Then map the feed's column names to our schema in COLUMN_IDS.

FALLBACKS (documented, not wired in here)
-----------------------------------------
  * Specs only, official & offline:  STM32CubeMX local database
        <CubeMX install>/db/mcu/*.xml   ->  core, freq, flash, ram, io, package
    and the community mirror  https://github.com/whitequark/stm32-data
    (clean machine-readable RAM/flash/package). Neither carries price/power.
  * Price:  distributor APIs keyed by orderable part number -
        Octopart/Nexar, Digi-Key, or Mouser. Use to override price_usd.

OUTPUT SCHEMA  (stm32_data.json)
--------------------------------
  {
    "source": "st-mcu-product-selector",
    "generated": "2026-06-12",
    "parts": [
      { "part":"STM32F407VG", "series":"F4", "core":"Cortex-M4",
        "speed_mhz":168, "run_current_ma":87, "price_usd":8.1,
        "flash_kb":1024, "ram_kb":192, "io_max":82,
        "package":"LQFP100",
        "url":"https://www.st.com/en/.../stm32f407vg.html" },
      ...
    ]
  }
Only `part` and the numeric axes you graph are strictly required; the viewer
fills `url` automatically if omitted.

FETCHING PAST ST'S BOT PROTECTION
---------------------------------
ST fronts the feed with Akamai bot manager, which blocks non-browser TLS
fingerprints: a plain requests/curl call connects and then stalls until the
read timeout. So by default the ripper fetches through a headless browser
driving your stock Chrome (channel "chrome"), which clears Akamai where bundled
Chromium gets blocked. The exact downloaded payload is saved to
raw_selector_data.json for inspection. Flags:

    python3 stm32_ripper.py            # default: headless Chrome fetch
    python3 stm32_ripper.py --headful  # watch it in a visible window
    python3 stm32_ripper.py --channel '' # use bundled Chromium instead of Chrome
    python3 stm32_ripper.py --requests # use the plain-HTTP fetch instead
    python3 stm32_ripper.py -v         # verbose logging
    python3 stm32_ripper.py --dump     # print one raw row (to fill COLUMN_IDS)

Dependencies:  pip install requests playwright
               python -m playwright install chromium
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import datetime as dt

import requests

log = logging.getLogger("stm32_ripper")

# (connect, read) timeouts in seconds. Split so a slow/stalling server trips the
# read budget without inflating the time we wait just to establish a socket.
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
RETRIES = 3            # total attempts on transient network errors
BACKOFF = 5            # seconds, multiplied by attempt number

# ST fronts the feed with Akamai bot manager, which blocks non-browser TLS
# fingerprints: a plain requests/curl call connects then stalls. So the reliable
# fetch routes through a headless browser, which carries a real Chrome handshake
# plus the cookies/JS challenge the page establishes. Priming the page first is
# what makes the subsequent feed request go through.
SELECTOR_PAGE = ("https://www.st.com/content/st_com/en/"
                 "stm32-mcu-product-selector.html")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BROWSER_TIMEOUT = 45   # per-step browser timeout, seconds

# --------------------------------------------------------------------------
# CONFIG  -- edit these two after the DevTools capture described above.
# --------------------------------------------------------------------------
SELECTOR_FEED_URL = ""   # <-- paste the JSON feed URL captured from DevTools

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stm32-ripper/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.st.com/content/st_com/en/"
               "stm32-mcu-product-selector.html",
    # "Cookie": "...",  # paste if the feed needs the session cookie
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (scripts/ is one down)
OUTPUT = os.path.join(ROOT, "stm32_data.json")
JS_OUTPUT = os.path.join(ROOT, "stm32_data.js")             # window.STM32_DATA sidecar for file:// autoload
RAW_OUTPUT = os.path.join(ROOT, "raw_selector_data.json")   # exact feed payload, before normalizing
ATTR_OUTPUT = os.path.join(ROOT, "additional_attributes.json")  # overview of every feed column + its values

# The ST selector feed is column/row indirected: payload["columns"] defines
# each column's id+name, and every row carries cells = [{columnId, value}, ...].
# So we map our schema keys -> the feed's numeric column id(s) (first present
# wins). Run with --dump to see ids/names; EXPECTED_COLUMN names below are
# sanity-checked at runtime and warn if ST renumbers a column.
COLUMN_IDS = {
    "part":           ["1"],     # Part Number  (e.g. STM32F410R8)
    "core":           ["258"],   # Core
    "speed_mhz":      ["4256"],  # Operating Frequency (MHz)
    "flash_kb":       ["3144"],  # Flash Size (kB)
    "ram_kb":         ["1901"],  # RAM Size (kB)
    "io_max":         ["630"],   # I/Os (number of I/O ports)
    "run_current_ma": ["4409"],  # Supply Current (max) — rough run-current proxy
    "package":        ["4363"],  # Package
    # price_usd: this feed carries no price column — enrich from a distributor
    # API keyed by part number (see README). Left unmapped -> stays null.
    "price_usd":      [],
}

# id -> name we expect, to detect feed drift. Checked once per run.
EXPECTED_COLUMN = {
    "1": "Part Number", "258": "Core", "4256": "Operating Frequency",
    "3144": "Flash Size", "1901": "RAM Size", "630": "I/Os (High Current)",
    "4409": "Supply Current", "4363": "Package",
}

# --------------------------------------------------------------------------
# "Attributes - simplified": a hand-curated capability set written to each part
# as a flat `attrs` list the viewer shows as AND-filter chips. Two flavours:
#   SIMPLE_META   column id -> one chip = "the part has any value in this column"
#   SIMPLE_VALUES column id -> {raw feed value -> abbreviated chip}; each listed
#                 value (||-split) becomes its own chip. This is the attr_mapper.
# --------------------------------------------------------------------------
SIMPLE_META = {
    "5035": "USB",     "5535": "Security", "281":  "Crypto",  "5407": "Ethernet",
    "4359": "Display", "5301": "SMPS",     "5289": "COPROC",  "5047": "Graphics",
    "5606": "NPU",     "5600": "Motor",
}
SIMPLE_VALUES = {
    "5529": {  # FPU
        "Double-precision FPU": "FPU-DP", "Half-precision FPU": "FPU-HP",
        "Single-precision FPU": "FPU-SP",
    },
    "5439": {  # Additional Interfaces
        "ADF": "ADF", "Camera Interface": "Camera", "DFSDM": "DFSDM",
        "Ethernet": "ETH", "HDMI CEC": "HDMI", "MDF": "MDF",
        "MIPI CSI-2 camera interface": "MIPI", "PLAY": "PLAY",
        "Parallel camera interface": "ParCamera", "S/PDIF": "SPDIF",
        "SAI": "SAI", "SD/MMC": "SDMMC", "Vref Buffer": "VREF",
    },
    "5440": {  # External Memory Interfaces
        "Dual Octo SPI": "DualOctoSPI", "Dual Quad SPI": "DualQuadSPI",
        "FMC": "FMC", "FSMC": "FSMC", "Hexa SPI": "HexaSPI", "Octo SPI": "OctoSPI",
        "Quad SPI": "QuadSPI", "SPI": "SPI", "xSPI": "xSPI",
    },
}

# Chip display order for the simplified group (viewer reads this from the data).
SIMPLE_ORDER = (["USB", "Security", "FPU-DP", "FPU-HP", "FPU-SP"]
                + list(SIMPLE_VALUES["5439"].values())
                + ["Crypto"] + list(SIMPLE_VALUES["5440"].values())
                + ["Ethernet", "Display", "SMPS", "COPROC", "Graphics",
                   "NPU", "Motor"])

CORE_CANON = {
    "m0+": "Cortex-M0+", "m0plus": "Cortex-M0+", "cortex-m0+": "Cortex-M0+",
    "m0": "Cortex-M0", "cortex-m0": "Cortex-M0",
    "m3": "Cortex-M3", "m4": "Cortex-M4", "m7": "Cortex-M7",
    "m33": "Cortex-M33", "m55": "Cortex-M55", "m85": "Cortex-M85",
}


# --------------------------------------------------------------------------
def cells_of(row):
    """Flatten a feed row's cells into {columnId: value}."""
    return {c.get("columnId"): c.get("value") for c in row.get("cells", [])}


def first_id(cells, ids):
    """Return the first present, non-empty cell value among column `ids`."""
    for i in ids:
        v = cells.get(i)
        if v not in (None, "", "-"):
            return v
    return None


def to_num(v):
    """Pull the first number out of strings like '1024 kB', '$8.10', '64/80'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"[-+]?\d*\.?\d+", str(v).replace(",", ""))
    return float(m.group()) if m else None


def canon_core(v):
    if not v:
        return None
    key = str(v).lower().replace("arm ", "").replace(" ", "").replace("®", "")
    # Longest fragment first so "m33"/"m0+" win over the "m3"/"m0" substrings.
    for frag in sorted(CORE_CANON, key=len, reverse=True):
        if frag in key:
            return CORE_CANON[frag]
    return str(v)


def clean_package(v):
    """'LQFP 64 10x10x1.4 mm' -> 'LQFP64'; 'TFBGA216' -> 'TFBGA216'."""
    if not v:
        return ""
    # search (not match) so 'SIP LGA 77 6.5x10x1.372 mm' -> 'LGA77', not the
    # whole string, when the leading token carries no pin count.
    m = re.search(r"([A-Za-z]+)[\s\-]*0*(\d+)", str(v))
    return (m.group(1).upper() + m.group(2)) if m else str(v).split(",")[0].strip()


def series_of(part):
    m = re.match(r"STM32([A-Z]\d|[A-Z]{1,2}\d?)", str(part).upper())
    return m.group(1) if m else "?"


def st_url(part):
    slug = re.sub(r"[^a-z0-9]", "", str(part).lower())
    return ("https://www.st.com/en/microcontrollers-microprocessors/"
            + slug + ".html")


def part_url(row, part):
    """Prefer the feed's own product-folder path; fall back to a slug guess."""
    p = row.get("productFolderUrl")
    if p:
        return "https://www.st.com" + p if p.startswith("/") else p
    return st_url(part)


def simple_attrs(cells):
    """Flat capability chips for the 'Attributes - simplified' selector: meta
    chips for column presence + abbreviated value chips (see SIMPLE_META /
    SIMPLE_VALUES). De-duped, ordered per SIMPLE_ORDER."""
    found = set()
    for cid, label in SIMPLE_META.items():
        if cells.get(cid) not in (None, "", "-"):
            found.add(label)
    for cid, amap in SIMPLE_VALUES.items():
        v = cells.get(cid)
        if v in (None, "", "-"):
            continue
        for tok in str(v).split("||"):
            tok = tok.strip()
            if tok:
                found.add(amap.get(tok, tok.replace(" ", "")))  # fallback: de-spaced
    rank = {a: i for i, a in enumerate(SIMPLE_ORDER)}
    return sorted(found, key=lambda a: (rank.get(a, len(rank)), a))


def normalize(row):
    cells = cells_of(row)
    part = first_id(cells, COLUMN_IDS["part"])
    if not part or not str(part).upper().startswith("STM32"):
        return None
    rec = {
        "part": str(part).strip(),
        "core": canon_core(first_id(cells, COLUMN_IDS["core"])),
        "speed_mhz": to_num(first_id(cells, COLUMN_IDS["speed_mhz"])),
        "flash_kb": to_num(first_id(cells, COLUMN_IDS["flash_kb"])),
        "ram_kb": to_num(first_id(cells, COLUMN_IDS["ram_kb"])),
        "io_max": to_num(first_id(cells, COLUMN_IDS["io_max"])),
        "price_usd": to_num(first_id(cells, COLUMN_IDS["price_usd"])),
        "run_current_ma": to_num(first_id(cells, COLUMN_IDS["run_current_ma"])),
        "package": clean_package(first_id(cells, COLUMN_IDS["package"])),
    }
    rec["series"] = series_of(part)
    rec["attrs"] = simple_attrs(cells)
    rec["url"] = part_url(row, part)
    return rec


def validate_columns(payload):
    """Warn if ST has renumbered a column we depend on (feed drift)."""
    colidx = {c.get("id"): c.get("name") for c in payload.get("columns", [])}
    for key, ids in COLUMN_IDS.items():
        present = [i for i in ids if i in colidx]
        if ids and not present:
            log.warning("Column id(s) %s for '%s' not in feed — value will be "
                        "blank. Re-check ids with --dump.", ids, key)
        for i in present:
            exp = EXPECTED_COLUMN.get(i)
            if exp and colidx[i] != exp:
                log.warning("Column %s is now '%s' (expected '%s') — mapping "
                            "for '%s' may be stale.", i, colidx[i], exp, key)
            else:
                log.debug("%-15s <- col %s '%s'", key, i, colidx[i])


_NUM_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def harvest_attributes(payload, rows):
    """Survey every column in the feed so we can decide which attributes are
    worth surfacing as filters. For each column records its name, fill-rate, a
    guessed kind, and — for the categorical/multi-valued ones — the distinct
    values (||-split into the tokens that would become chips). Writes a
    human-scannable overview; nothing here feeds the dataset itself."""
    import collections
    colidx = {c.get("id"): c.get("name") for c in payload.get("columns", [])}
    mapped = {i: key for key, ids in COLUMN_IDS.items() for i in ids}

    non_empty = collections.Counter()
    tokens = collections.defaultdict(set)   # ||-split distinct values (chip candidates)
    raws = collections.defaultdict(set)     # unsplit distinct, for kind detection
    multi = collections.defaultdict(bool)
    for row in rows:
        for c in row.get("cells", []):
            cid, v = c.get("columnId"), c.get("value")
            if v in (None, "", "-"):
                continue
            s = str(v).strip()
            non_empty[cid] += 1
            raws[cid].add(s)
            if "||" in s:
                multi[cid] = True
                tokens[cid].update(t.strip() for t in s.split("||") if t.strip())
            else:
                tokens[cid].add(s)

    def kind_of(cid):
        rs = raws[cid]
        if multi[cid]:                                       return "multi"
        if rs and rs <= {"true", "false"}:                   return "boolean"
        if rs and all(_NUM_RE.match(x) for x in rs):         return "numeric"
        return "enum" if len(tokens[cid]) <= 40 else "text"

    cols = []
    for cid, name in colidx.items():
        k = kind_of(cid)
        e = {"id": cid, "name": name, "non_empty": non_empty[cid],
             "kind": k, "distinct": len(tokens[cid]), "mapped": mapped.get(cid)}
        if k in ("multi", "enum", "boolean"):
            e["values"] = sorted(tokens[cid])[:120]
            if len(tokens[cid]) > 120:
                e["values_truncated"] = True
        elif k == "numeric":
            nums = [float(x) for x in raws[cid]]
            e["min"], e["max"] = min(nums), max(nums)
        else:
            e["sample"] = sorted(tokens[cid])[:3]
        cols.append(e)

    # capability columns (the ||/enum ones we'd flatten) drive the chip count
    rank = {"multi": 0, "enum": 1, "boolean": 2, "numeric": 3, "text": 4}
    cols.sort(key=lambda c: (rank[c["kind"]], -c["non_empty"]))
    cap = set()
    for c in cols:
        if c["kind"] in ("multi", "enum") and c["mapped"] is None:
            cap.update(tokens[c["id"]])

    out = {
        "generated": dt.date.today().isoformat(),
        "rows": len(rows),
        "projected_capability_chips": len(cap),
        "columns": cols,
    }
    with open(ATTR_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    log.info("Wrote %s: %d columns, ~%d distinct capability values.",
             ATTR_OUTPUT, len(cols), len(cap))


def extract_rows(payload):
    """The feed nests the row list differently across ST revisions; probe the
    common shapes and return a flat list of dict rows."""
    if isinstance(payload, list):
        log.debug("Payload is a top-level list of %d items", len(payload))
        return payload
    for key in ("data", "rows", "results", "products", "items"):
        v = payload.get(key)
        if isinstance(v, list):
            log.debug("Found rows at payload['%s'] (%d items)", key, len(v))
            return v
        if isinstance(v, dict):
            for k2 in ("data", "rows", "results"):
                if isinstance(v.get(k2), list):
                    log.debug("Found rows at payload['%s']['%s'] (%d items)",
                              key, k2, len(v[k2]))
                    return v[k2]
    # last resort: first list value found anywhere one level down
    for k, v in payload.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            log.debug("Falling back to first list-of-dicts at payload['%s'] "
                      "(%d items)", k, len(v))
            return v
    log.error("Could not locate a row list in the payload. Top-level keys: %s",
              list(payload.keys()) if isinstance(payload, dict) else type(payload))
    return []


def load_discovered():
    """If find_selector_feed.py has been run, reuse its output instead of the
    hand-edited constants above. Returns (url, headers, method, post_data)."""
    url, headers, method, post = SELECTOR_FEED_URL, dict(HEADERS), "GET", None
    url_file = os.path.join(ROOT, "selector_feed.url")
    hdr_file = os.path.join(ROOT, "selector_feed.headers.json")
    if os.path.exists(url_file):
        with open(url_file, encoding="utf-8") as f:
            url = f.read().strip() or url
    if os.path.exists(hdr_file):
        with open(hdr_file, encoding="utf-8") as f:
            sc = json.load(f)
        url = sc.get("url", url)
        method = sc.get("method", "GET")
        post = sc.get("post_data")
        # merge discovered headers over the defaults
        headers.update({k: v for k, v in (sc.get("headers") or {}).items() if v})
    return url, headers, method, post


def _fetch_requests(url, headers, method, post, *, attempts, read_timeout):
    """Plain HTTP fetch via requests. Fast when the feed is directly reachable,
    but ST's Akamai bot manager blocks non-browser TLS fingerprints, so against
    st.com this typically connects and then stalls until the read timeout."""
    timeout = (CONNECT_TIMEOUT, read_timeout)
    last_err = None
    for attempt in range(1, attempts + 1):
        t0 = time.monotonic()
        try:
            log.info("[requests] attempt %d/%d (connect=%ss read=%ss)...",
                     attempt, attempts, CONNECT_TIMEOUT, read_timeout)
            if method == "POST":
                r = requests.post(url, headers=headers, data=post, timeout=timeout)
            else:
                r = requests.get(url, headers=headers, timeout=timeout)
            dt_ms = (time.monotonic() - t0) * 1000
            log.info("[requests] HTTP %s in %.0f ms, %s bytes, content-type=%s",
                     r.status_code, dt_ms, len(r.content),
                     r.headers.get("content-type", "?"))
            r.raise_for_status()
            payload = _loads_json(r.text)
            if payload is None:
                log.error("[requests] response was not JSON (first 200 chars): "
                          "%.200s", r.text)
                raise RuntimeError("feed response was not JSON")
            _save_raw(r.text)
            return payload
        except requests.exceptions.ReadTimeout as e:
            last_err = e
            log.warning("[requests] connection accepted but no response within "
                        "%ss (attempt %d/%d) — likely Akamai blocking the "
                        "non-browser TLS fingerprint.", read_timeout,
                        attempt, attempts)
        except requests.exceptions.RequestException as e:
            last_err = e
            log.warning("[requests] failed (attempt %d/%d): %s",
                        attempt, attempts, e)
        if attempt < attempts:
            wait = BACKOFF * attempt
            log.info("[requests] retrying in %ds...", wait)
            time.sleep(wait)
    raise last_err


def _loads_json(body):
    """Parse JSON, returning None instead of raising. Used to tell a real feed
    response apart from an Akamai HTML challenge page."""
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _save_raw(text):
    """Persist the exact downloaded feed payload for inspection / debugging the
    COLUMN_IDS. Best-effort: a write failure shouldn't abort the rip."""
    try:
        with open(RAW_OUTPUT, "w", encoding="utf-8") as f:
            f.write(text)
        log.info("Saved raw feed payload to %s (%d bytes).", RAW_OUTPUT, len(text))
    except OSError as e:
        log.warning("Could not write %s: %s", RAW_OUTPUT, e)


def _fetch_browser(url, headers, method, post, *, headful=False,
                   channel=None, pause=False, disable_http2=True):
    """Fetch through Chromium so the request carries a real browser TLS
    fingerprint plus the cookies/JS challenge ST's page establishes — the path
    that actually clears Akamai. Tries the feed URL directly, then falls back to
    priming the selector page and fetching from inside that page context.

    Debugging knobs (see --headful/--channel/--pause/--http2):
      headful       show the browser window so you can watch and use DevTools
      channel       use a stock browser ("chrome"/"msedge") vs bundled Chromium
      pause         open the Playwright Inspector after the first navigation
      disable_http2 force HTTP/1.1 (default) vs let Chromium negotiate HTTP/2
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is needed to fetch past ST's bot protection. Install:\n"
            "    pip install playwright && python -m playwright install chromium")

    # --disable-http2 dodges Akamai's HTTP/2 stream resets (ERR_HTTP2_PROTOCOL_
    # ERROR) against headless clients; the automation flag trims an obvious
    # bot tell. If a run still stalls, retry on a non-datacenter IP.
    launch_args = ["--disable-blink-features=AutomationControlled"]
    if disable_http2:
        launch_args.append("--disable-http2")
    launch_kwargs = {"headless": not headful, "args": launch_args}
    if channel:
        launch_kwargs["channel"] = channel
    if headful:
        launch_kwargs["slow_mo"] = 300   # ease watching the steps
    log.info("[browser] launching %s%s (args: %s)...",
             "headful " if headful else "headless ",
             channel or "chromium", " ".join(launch_args))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(user_agent=headers.get("user-agent") or UA,
                                  locale="en-US")
        page = ctx.new_page()

        def maybe_pause(where):
            if pause:
                log.info("[browser] paused at %s — inspect the page/Network "
                         "tab, then press Resume in the Playwright Inspector "
                         "(or close it) to continue.", where)
                page.pause()

        # Strategy 1 (GET only): navigate straight to the feed URL. The feed
        # returns raw JSON in a browser, so this is the cheapest path and avoids
        # the heavy selector app page (which often never reaches a load state
        # under headless and just times out).
        if method == "GET":
            log.info("[browser] navigating directly to feed: %s", url)
            try:
                resp = page.goto(url, wait_until="commit",
                                 timeout=BROWSER_TIMEOUT * 1000)
                body = resp.text() if resp else ""
                log.info("[browser] direct nav HTTP %s, %s bytes, content-type=%s",
                         resp.status if resp else "?", len(body),
                         resp.headers.get("content-type", "?") if resp else "?")
                maybe_pause("the feed URL")
                payload = _loads_json(body)
                if payload is not None:
                    _save_raw(body)
                    browser.close()
                    return payload
                log.warning("[browser] direct navigation didn't return JSON "
                            "(likely a bot challenge); priming the selector "
                            "page and retrying via in-page fetch.")
            except Exception as e:
                log.warning("[browser] direct navigation failed (%s); priming "
                            "the selector page instead.", e.__class__.__name__)

        # Strategy 2: prime the selector page to establish Akamai cookies / JS
        # challenge, then issue the feed request from inside that page context.
        # goto here is best-effort — the app page may never fully settle.
        log.info("[browser] priming session at %s", SELECTOR_PAGE)
        try:
            page.goto(SELECTOR_PAGE, wait_until="commit",
                      timeout=BROWSER_TIMEOUT * 1000)
        except Exception as e:
            log.warning("[browser] priming goto did not complete (%s); "
                        "continuing anyway.", e.__class__.__name__)
        for sel in ("#onetrust-accept-btn-handler",
                    "button:has-text('Accept')",
                    "button:has-text('I ACCEPT')"):
            try:
                page.click(sel, timeout=2500)
                log.info("[browser] dismissed cookie banner.")
                break
            except Exception:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT * 1000)
        except Exception:
            log.debug("[browser] networkidle not reached; continuing.")

        maybe_pause("the primed selector page")

        # In-page fetch: browsers silently drop forbidden headers (Referer,
        # User-Agent) — fine, the page context supplies correct ones.
        log.info("[browser] requesting feed via in-page %s fetch...", method)
        res = page.evaluate(
            """async ([url, method, post, headers]) => {
                const opts = {method, credentials: 'include', headers};
                if (post) opts.body = post;
                const r = await fetch(url, opts);
                return {status: r.status, ok: r.ok, body: await r.text(),
                        ctype: r.headers.get('content-type')};
            }""",
            [url, method, post, headers])
        browser.close()

    log.info("[browser] in-page fetch HTTP %s, %s bytes, content-type=%s",
             res["status"], len(res["body"] or ""), res.get("ctype"))
    if not res["ok"]:
        raise RuntimeError(f"feed returned HTTP {res['status']}")
    payload = _loads_json(res["body"])
    if payload is None:
        log.error("[browser] response was not JSON (first 200 chars): %.200s",
                  res["body"])
        raise RuntimeError("feed response was not JSON")
    _save_raw(res["body"])
    return payload


def fetch(args):
    url, headers, method, post = load_discovered()
    if not url:
        sys.exit("No feed URL. Run find_selector_feed.py first (writes "
                 "selector_feed.url), or set SELECTOR_FEED_URL above.")

    log.info("Feed source: %s %s", method, url)
    log.debug("Request headers: %s", json.dumps(headers, indent=2))
    if post:
        log.debug("POST body: %s", post)
    if "top_menu" in url or "menu" in url.rsplit("/", 1)[-1]:
        log.warning("Feed URL looks like a site-navigation menu, not the MCU "
                    "parametric feed (%s). Re-run find_selector_feed.py and "
                    "pick the large XHR full of 'STM32...' rows.", url)

    try:
        if args.requests:
            return _fetch_requests(url, headers, method, post,
                                   attempts=RETRIES, read_timeout=READ_TIMEOUT)
        return _fetch_browser(url, headers, method, post,
                              headful=args.headful, channel=args.channel,
                              pause=args.pause, disable_http2=not args.http2)
    except SystemExit:
        raise
    except Exception as e:
        log.error("Could not fetch the feed: %s", e)
        sys.exit(f"Could not fetch the feed: {e}")


def parse_args():
    ap = argparse.ArgumentParser(
        description="Rip the ST MCU Product Selector feed into stm32_data.json.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true",
                      help="fetch through a headless browser (default; clears "
                           "ST's Akamai bot protection)")
    mode.add_argument("--requests", action="store_true",
                      help="fetch with plain HTTP requests instead of the "
                           "browser (fast, but ST's Akamai usually stalls it)")
    ap.add_argument("--dump", action="store_true",
                    help="print one raw feed row and exit (to fill COLUMN_IDS)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="verbose (DEBUG) logging")
    # Browser debugging knobs (ignored with --requests).
    ap.add_argument("--headful", action="store_true",
                    help="show the browser window (needs a display) so you can "
                         "watch the navigation and use DevTools")
    ap.add_argument("--channel", metavar="NAME", default="chrome",
                    help="stock browser channel to drive (default 'chrome', "
                         "which clears Akamai where bundled Chromium can't); "
                         "e.g. 'msedge', or '' to use bundled Chromium")
    ap.add_argument("--pause", action="store_true",
                    help="open the Playwright Inspector after navigating so you "
                         "can inspect the page (implies --headful)")
    ap.add_argument("--http2", action="store_true",
                    help="let Chromium negotiate HTTP/2 (default forces HTTP/1.1 "
                         "to dodge Akamai's HTTP/2 stream resets)")
    args = ap.parse_args()
    if args.pause:
        args.headful = True   # the Inspector needs a headed browser
    return args


def main(args):
    payload = fetch(args)

    if args.dump:
        # Inspect one raw row so you can fill COLUMN_IDS correctly.
        validate_columns(payload)
        rows = extract_rows(payload)
        if not rows:
            sys.exit("No rows found in feed — nothing to dump.")
        log.info("%d rows; dumping first raw row.", len(rows))
        print(f"{len(rows)} rows. First raw row keys/values:\n")
        print(json.dumps(rows[0], indent=2, ensure_ascii=False))
        return

    validate_columns(payload)
    rows = extract_rows(payload)
    harvest_attributes(payload, rows)
    log.info("Extracted %d raw rows; normalizing...", len(rows))
    parts, skipped = [], 0
    for row in rows:
        rec = normalize(row)
        if rec and rec["speed_mhz"]:        # require at least a usable speed
            parts.append(rec)
        else:
            skipped += 1
    log.info("Normalized %d parts, skipped %d (no STM32 part# or no speed).",
             len(parts), skipped)

    # de-dup by part number, keep the richest record
    by_part = {}
    for p in parts:
        cur = by_part.get(p["part"])
        if cur is None or sum(v is not None for v in p.values()) > \
                          sum(v is not None for v in cur.values()):
            by_part[p["part"]] = p
    parts = sorted(by_part.values(), key=lambda p: p["part"])

    out = {
        "source": "st-mcu-product-selector",
        "generated": dt.date.today().isoformat(),
        "attr_order": SIMPLE_ORDER,
        "parts": parts,
    }
    log.info("De-duped to %d unique parts.", len(parts))
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    # JS sidecar so the viewer can autoload on double-click (file://), where the
    # browser blocks fetch() of a local .json. Sets a global the page reads.
    with open(JS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("window.STM32_DATA = ")
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    miss = lambda k: sum(1 for p in parts if p.get(k) is None)
    print(f"Wrote {OUTPUT} and {JS_OUTPUT}: {len(parts)} parts ({skipped} skipped).")
    for k in ("price_usd", "run_current_ma", "io_max"):
        n = miss(k)
        print(f"  missing {k:16s}: {n}")
        if n == len(parts) and parts:
            log.warning("Every part is missing '%s' — check its COLUMN_IDS "
                        "entry against the --dump output.", k)


def setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.verbose)
    main(args)
