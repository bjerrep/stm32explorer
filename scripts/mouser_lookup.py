#!/usr/bin/env python3
"""Look up an STM32 part's price on the Mouser Search API.

Mouser gives real distributor pricing (per-quantity price breaks) for free —
you just need an API key (no OAuth). Register at:
    https://www.mouser.com/api-hub/   ->  "Search API"
then:
    export MOUSER_API_KEY=...

Note: Mouser has no batch endpoint (one MPN per request) and caps the free
key at ~1000 calls/day, so pricing the whole catalogue takes a few runs —
that's what batch_prices.py's resumable checkpointing is for.

Usage:
    python3 mouser_lookup.py                 # demo: pick an MCU from the DB
    python3 mouser_lookup.py STM32F407VG     # base name -> resolved MPN
    python3 mouser_lookup.py STM32F407VGT6   # already-orderable MPN
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

from mpn_resolve import (
    DEFAULT_TEMP, PKG_CODE, find_part, load_data, pkg_family, resolve_mpn,
)

SEARCH_URL = "https://api.mouser.com/api/v1/search/partnumber"


def parse_price(s):
    """Mouser price strings like '$12.34', '1,234.56', '12,34 €' -> float."""
    if not s:
        return None
    t = re.sub(r"[^\d,.\-]", "", s)          # keep digits and separators
    if "," in t and "." in t:                # '1,234.56' -> thousands + decimal
        t = t.replace(",", "")
    elif "," in t:                           # '12,34' (EU decimal) -> '12.34'
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def mouser_search(mpn, key, timeout=30):
    """Return the Mouser Parts list for an MPN (may be empty)."""
    body = json.dumps({
        "SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": ""}
    }).encode()
    url = f"{SEARCH_URL}?apiKey={key}"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    errs = resp.get("Errors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("Message", str(e)) for e in errs))
    return (resp.get("SearchResults") or {}).get("Parts") or []


def unit_price(part):
    """Cheapest listed price break (any quantity) for a Mouser part, or None."""
    prices = [parse_price(b.get("Price")) for b in (part.get("PriceBreaks") or [])]
    prices = [p for p in prices if p is not None]
    return round(min(prices), 4) if prices else None


def price_for_mpn(mpn, key):
    """Best unit price for an MPN, preferring an exact ManufacturerPartNumber
    match, else the cheapest of any returned part. Returns (price, matchedMPN)."""
    parts = mouser_search(mpn, key)
    if not parts:
        return None, None
    exact = [p for p in parts if (p.get("ManufacturerPartNumber") or "").upper() == mpn.upper()]
    cand = exact or parts
    return _cheapest(cand)


def _cheapest(parts):
    """(price, matchedMPN) for the cheapest priced part in a list."""
    return min(
        ((unit_price(p), p.get("ManufacturerPartNumber")) for p in parts),
        key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0),
    )


def price_for_part(base, package, key):
    """Price a dataset base part with fallback. Returns (price, matchedMPN, how).

    1. exact resolved MPN (base + package-code + temp '6');
    2. if that has no price, search the BARE base part — this returns whatever
       temperature grades / packages / options Mouser actually stocks (so a
       missing '-40..85C' variant or an obsolete-in-this-temp part is caught),
       preferring the same package family, then taking the cheapest.
    """
    code = PKG_CODE.get(pkg_family(package))
    if code:
        price, matched = price_for_mpn(f"{base}{code}{DEFAULT_TEMP}", key)
        if price is not None:
            return price, matched, "exact"

    parts = mouser_search(base, key)              # one call, all variants of this die
    if not parts:
        return None, None, "no-stock"
    same = [p for p in parts if code and
            (p.get("ManufacturerPartNumber") or "").upper().startswith(f"{base}{code}".upper())]
    if same:
        price, matched = _cheapest(same)
        if price is not None:
            return price, matched, "alt-temp"      # same package, different temp/option
    price, matched = _cheapest(parts)
    return price, matched, ("alt-pkg" if price is not None else "no-price")


def main(argv):
    parts = load_data()["parts"]
    query = argv[1] if len(argv) > 1 else None

    explicit = bool(query and re.search(r"[A-Z]\d$", query.upper()) and len(query) >= 12)
    if explicit:
        base, package = query, None
        print(f"mpn       {query}   (explicit — no fallback)")
    else:
        p = find_part(parts, query) if query else parts[0]
        if not p:
            print(f"No dataset part matching '{query}'", file=sys.stderr)
            return 1
        base, package = p["part"], p.get("package")
        print(f"part      {base}   ({p['core']}, {p['series']}, {p['package']})")
        print(f"resolved  {resolve_mpn(base, package)}   (temp '{DEFAULT_TEMP}' = -40..85C)")

    key = os.environ.get("MOUSER_API_KEY")
    if not key:
        print("\nMOUSER_API_KEY not set — skipping live lookup.")
        print("Get a free key at https://www.mouser.com/api-hub/ then:  export MOUSER_API_KEY=...")
        return 0

    print("\nMouser:")
    try:
        if explicit:
            price, matched = price_for_mpn(query, key)
            how = "exact" if price is not None else "no-price"
        else:
            price, matched, how = price_for_part(base, package, key)

        if price is None:
            print(f"  no price found  [{how}]")
        else:
            print(f"  ${price:.4f}  {matched}   [{how}]")

        # when the exact -40..85C MPN missed, show what Mouser actually stocks
        # for this die — the alternative temp grades / packages / options.
        if how != "exact":
            variants = mouser_search(base, key)
            if variants:
                print(f"\n  variants Mouser stocks for {base}:")
                for v in sorted(variants, key=lambda v: v.get("ManufacturerPartNumber") or ""):
                    up = unit_price(v)
                    tag = f"${up:.4f}" if up is not None else "no price"
                    print(f"    {v.get('ManufacturerPartNumber',''):22} {tag:>10}")
            else:
                print(f"  (Mouser returns nothing for base {base} — likely obsolete / not stocked)")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"  API error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
