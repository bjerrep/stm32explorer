#!/usr/bin/env python3
"""Batch-fill `price_usd` in stm32_data.json from the Mouser Search API,
falling back to Digi-Key for parts Mouser doesn't stock/price.

Walks every part in the dataset, resolves a best-effort orderable MPN
(mpn_resolve.resolve_mpn), asks Mouser for its cheapest listed unit price, and
writes it back into `stm32_data.json` in place. When Mouser returns no price,
Digi-Key is tried next (if its credentials are set); the winning distributor is
recorded in `price_source`.

Mouser has no batch endpoint and caps a free key at ~1000 calls/day, so the
full 1600-part catalogue takes a couple of days. Two things make that painless:

  * Resumability — already-priced parts are skipped, and so are parts a previous
    run tagged `price_status` (no-stock / no-price) so the daily quota isn't
    wasted re-querying dead parts. The file is checkpointed after every part; hit
    the daily cap (or Ctrl-C), then just re-run tomorrow to continue.
    `--refresh` clears those tags (and re-prices priced parts) to retry everything.
  * Rate limiting — `--sleep` between calls, and a hard `--max-requests`
    budget so one run can't burn through the daily quota.

Credentials:
    export MOUSER_API_KEY=...                      # required (primary source)
    export DIGIKEY_CLIENT_ID=...                   # optional (fallback)
    export DIGIKEY_CLIENT_SECRET=...               # optional (fallback)

Usage:
    python3 batch_prices.py --dry-run             # resolve only, no API calls
    python3 batch_prices.py --limit 40            # price the first 40 unpriced
    python3 batch_prices.py --start-at 500        # skip ahead, start at dataset index 500
    python3 batch_prices.py --max-requests 900    # stay under the daily cap
    python3 batch_prices.py --refresh             # clear no-price/no-stock tags + re-price all
    python3 batch_prices.py --refresh-incomplete  # retry only unpriced/tagged parts
    python3 batch_prices.py --no-digikey          # Mouser only, skip the fallback
"""
import argparse
import sys
import time
import urllib.error

from mpn_resolve import load_data, resolve_mpn, save_data
from mouser_lookup import price_for_part
import digikey_lookup


def main(argv):
    ap = argparse.ArgumentParser(description="Batch-fill price_usd from the Mouser API.")
    ap.add_argument("--limit", type=int, help="only process the first N eligible parts")
    ap.add_argument("--start-at", type=int, metavar="N",
                    help="start at dataset part index N (0-based); skip earlier parts")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--max-requests", type=int, help="hard cap on API requests this run")
    ap.add_argument("--refresh", action="store_true",
                    help="clear no-price/no-stock tags and re-price every part")
    ap.add_argument("--refresh-incomplete", action="store_true",
                    help="retry only unpriced parts (incl. tagged no-price/no-stock); leave priced parts alone")
    ap.add_argument("--dry-run", action="store_true", help="resolve MPNs only; make no API calls")
    ap.add_argument("--no-digikey", action="store_true", help="Mouser only; skip the Digi-Key fallback")
    args = ap.parse_args(argv[1:])

    data = load_data()
    parts = data["parts"]

    # Eligible = needs a price and resolves to an MPN. Normal runs also skip
    # parts tagged price_status (a prior no-stock/no-price result) so the quota
    # isn't wasted re-querying dead parts.
    #   --refresh              re-price everything, priced parts included
    #   --refresh-incomplete   retry unpriced parts only (incl. tagged), clearing
    #                          their tags; leave already-priced parts alone
    todo, unresolved = [], 0                          # each entry: (dataset index, part, mpn)
    for idx, p in enumerate(parts):
        if p.get("price_usd") is not None and not args.refresh:
            continue                                 # keep priced parts (unless full --refresh)
        if args.refresh or args.refresh_incomplete:
            p.pop("price_status", None)              # clear tag so it's retried
        elif p.get("price_status"):
            continue                                 # normal run skips known-missing
        mpn = resolve_mpn(p["part"], p.get("package"))
        if not mpn:
            unresolved += 1
            continue
        todo.append((idx, p, mpn))
    if args.start_at is not None:
        todo = [t for t in todo if t[0] >= args.start_at]   # keep eligible parts at/after index N
    if args.limit:
        todo = todo[:args.limit]

    priced_already = sum(1 for p in parts if p.get("price_usd") is not None)
    tagged = sum(1 for p in parts if p.get("price_status"))
    print(f"parts: {len(parts)}   already priced: {priced_already}   "
          f"tagged no-price/no-stock: {tagged}   "
          f"unresolved package: {unresolved}   to process: {len(todo)}")

    if args.dry_run:
        for idx, p, mpn in todo[:15]:
            print(f"  {idx:4} {p['part']:16} {p.get('package',''):10} -> {mpn}")
        if len(todo) > 15:
            print(f"  ... +{len(todo) - 15} more")
        return 0
    if not todo:
        print("nothing to do.")
        return 0

    import os
    if not os.environ.get("MOUSER_API_KEY"):
        print("MOUSER_API_KEY not set.", file=sys.stderr)
        return 1
    key = os.environ["MOUSER_API_KEY"]

    dk_enabled = not args.no_digikey and digikey_lookup.have_credentials()
    if not args.no_digikey and not dk_enabled:
        print("(Digi-Key fallback off — DIGIKEY_CLIENT_ID/DIGIKEY_CLIENT_SECRET not set)")

    requests_made = hits = misses = 0
    dk_requests = dk_hits = 0
    try:
        for i, (idx, p, mpn) in enumerate(todo):
            if args.max_requests and requests_made >= args.max_requests:
                print(f"reached --max-requests={args.max_requests}; stopping.")
                break
            if i and args.sleep:
                time.sleep(args.sleep)
            try:
                price, matched, how = price_for_part(p["part"], p.get("package"), key)
            except urllib.error.HTTPError as e:
                print(f"  HTTP {e.code} on {mpn}: {e.read().decode()[:200]} — stopping.", file=sys.stderr)
                break
            except RuntimeError as e:
                # Mouser reports the daily-cap / key errors in the Errors block.
                print(f"  API error on {mpn}: {e} — stopping.", file=sys.stderr)
                break
            # an exact hit is 1 call; any fallback also searched the base = 2 calls
            requests_made += 1 if how == "exact" else 2
            source, miss_how = "mouser", how        # miss_how: reason it stayed unpriced

            # Mouser had nothing priced — try Digi-Key for this die.
            if price is None and dk_enabled:
                try:
                    token = digikey_lookup.get_token(*digikey_lookup.credentials())
                    dprice, dmatched, dhow = digikey_lookup.price_for_part(
                        p["part"], p.get("package"), token, digikey_lookup.credentials()[0])
                    dk_requests += 1 if dhow == "exact" else 2
                    if dprice is not None:
                        price, matched, how, source = dprice, dmatched, dhow, "digikey"
                        dk_hits += 1
                    else:
                        miss_how = dhow              # Digi-Key was the last word
                except urllib.error.HTTPError as e:
                    print(f"  Digi-Key HTTP {e.code} on {mpn}: {e.read().decode()[:200]}", file=sys.stderr)
                except (RuntimeError, urllib.error.URLError) as e:
                    print(f"  Digi-Key error on {mpn}: {e}", file=sys.stderr)

            if price is not None:
                p["price_usd"] = price
                p["price_source"] = source
                p.pop("price_status", None)          # clear any stale tag
                hits += 1
                tag, label = f"${price:.3f}", f"{source}:{how}"
            else:
                # tag so normal runs skip it; --refresh clears it to retry
                p["price_status"] = miss_how         # "no-stock" / "no-price"
                misses += 1
                tag, label = "no price", miss_how
            save_data(data)                          # checkpoint after every part
            note = matched if (matched and matched != mpn) else ""
            # first column = the part's index in the dataset (model)
            print(f"  {idx:4}: {p['part']:16} {tag:>9}  [{label}] {note}")
    except KeyboardInterrupt:
        print("\ninterrupted — progress saved.")

    save_data(data)
    print(f"done. mouser calls: {requests_made}  digikey calls: {dk_requests}  "
          f"priced: {hits} (digikey: {dk_hits})  tagged no-price/no-stock: {misses}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
