# Scripts

Helper scripts for building and pricing the dataset the viewer
(`stm32-explorer.html`) reads. They live in `scripts/` but **read and write the
data files in the repo root** (`stm32_data.json`, `stm32_data.js`, …) regardless
of where you invoke them, so the conventional call is from the repo root:

```bash
python3 scripts/<name>.py [args]
```

> This file is maintained automatically — it documents each script's purpose and
> arguments. If you change a script's CLI, update this file.

## Pipeline

```
find_selector_feed.py ─▶ selector_feed.url + selector_feed.headers.json
                             │
stm32_ripper.py  ◀───────────┘   ─▶ stm32_data.json + stm32_data.js  (+ raw_selector_data.json, additional_attributes.json)
                                          │
batch_prices.py  ─▶ fills price_usd  ◀────┘   (uses mouser_lookup, digikey_lookup, mpn_resolve)

probe_part.py     — inspect one part in the raw feed dump
mouser_lookup.py  — probe one part's price on Mouser
digikey_lookup.py — probe one part's price on Digi-Key
mpn_resolve.py    — shared library (orderable-MPN resolver + data load/save)
```

`raw_selector_data.json` and `additional_attributes.json` are regenerable ripper
outputs and are git-ignored.

## Credentials (environment variables)

| Variable | Used by | Notes |
|---|---|---|
| `MOUSER_API_KEY` | `batch_prices.py`, `mouser_lookup.py` | Free Mouser Search API key. Required for pricing. |
| `DIGIKEY_CLIENT_ID` | `batch_prices.py`, `digikey_lookup.py` | Digi-Key Product Information V4 OAuth client id. Optional (fallback). |
| `DIGIKEY_CLIENT_SECRET` | `batch_prices.py`, `digikey_lookup.py` | Digi-Key OAuth client secret. Optional (fallback). |

---

## stm32_ripper.py

Rips the ST MCU Product Selector feed into the dataset. Writes `stm32_data.json`,
the `stm32_data.js` sidecar (a `window.STM32_DATA` global for `file://` autoload),
`raw_selector_data.json` (the exact feed payload), and `additional_attributes.json`
(an overview of every feed column). Reuses `selector_feed.url` /
`selector_feed.headers.json` from `find_selector_feed.py` if present.

Requires `pip install requests playwright` and `python -m playwright install chromium`.

| Argument | Purpose |
|---|---|
| `--browser` | Fetch through a headless browser (**default**; clears ST's Akamai bot protection). |
| `--requests` | Fetch with plain HTTP instead of the browser (fast, but Akamai usually stalls it). |
| `--dump` | Print one raw feed row and exit (to fill `COLUMN_IDS`). |
| `--headful` | Show the browser window (needs a display) to watch navigation / use DevTools. |
| `--channel NAME` | Stock browser channel to drive (default `chrome`; e.g. `msedge`, or `''` for bundled Chromium). |
| `--pause` | Open the Playwright Inspector after navigating (implies `--headful`). |
| `--http2` | Let Chromium negotiate HTTP/2 (default forces HTTP/1.1 to dodge Akamai resets). |
| `-v`, `--verbose` | Verbose (DEBUG) logging. |

## find_selector_feed.py

Discovers the live ST MCU Product Selector data-feed URL and the request headers
needed to fetch it, writing `selector_feed.url` and `selector_feed.headers.json`
for the ripper to reuse. Requires Playwright (unless `--no-browser`).

| Argument | Purpose |
|---|---|
| `--headful` | Show the browser window. |
| `--all` | Print all ranked JSON candidates. |
| `--no-browser` | Static-scan fallback (no Playwright). |
| `--timeout N` | Per-step timeout in seconds (default 45). |
| `--wait N` | Extra settle time after page load, seconds (default 5). |
| `-v`, `--verbose` | Verbose (DEBUG) logging, incl. every scored candidate. |

## batch_prices.py

Batch-fills `price_usd` in `stm32_data.json` from the Mouser Search API, falling
back to Digi-Key for parts Mouser doesn't stock/price. Resumable (checkpoints
after every part) and rate-limited. Records the winning distributor in
`price_source`, and tags parts with no price found via `price_status`
(`no-price` / `no-stock`) so normal runs skip them. Requires `MOUSER_API_KEY`
(and optionally the Digi-Key credentials).

| Argument | Purpose |
|---|---|
| `--limit N` | Only process the first N eligible parts. |
| `--start-at N` | Start at dataset part index N (0-based); skip earlier parts. Applied before `--limit`. |
| `--sleep S` | Seconds between requests (default 1.0). |
| `--max-requests N` | Hard cap on API requests this run (stay under the daily quota). |
| `--refresh` | Clear `no-price`/`no-stock` tags and re-price **every** part. |
| `--refresh-incomplete` | Retry only unpriced parts (incl. tagged); leave priced parts alone. |
| `--dry-run` | Resolve MPNs only; make no API calls. |
| `--no-digikey` | Mouser only; skip the Digi-Key fallback. |

## mouser_lookup.py

Probe a single part's Mouser price (with the same base-part fallback the batch
writer uses). Requires `MOUSER_API_KEY`.

```bash
python3 scripts/mouser_lookup.py [PART]
```

`PART` is a base name (`STM32F398VE`) or an orderable MPN (`STM32F398VET6`). With
no argument it picks a demo part from the dataset.

## digikey_lookup.py

Probe a single part's Digi-Key price. Same interface as `mouser_lookup.py`.
Requires `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`.

```bash
python3 scripts/digikey_lookup.py [PART]
```

## probe_part.py

Inspect one part in a raw feed dump — prints the matched row's raw JSON plus a
digest resolving every populated cell to its column name. Useful when mapping
`COLUMN_IDS` in the ripper.

```bash
python3 scripts/probe_part.py <raw_data_file> <part_number>
```

`<raw_data_file>` is the `raw_selector_data.json` shape (a
`{"columns":[…], "rows":[…]}` table). Regenerate it with `stm32_ripper.py` if
it's absent (it's git-ignored).

## mpn_resolve.py

Shared library (no CLI). Resolves a base part + package into a best-effort
orderable MPN (`resolve_mpn`), and provides `load_data` / `save_data` — the
latter writes both `stm32_data.json` and the `stm32_data.js` sidecar so they stay
in sync. Imported by the pricing scripts.
