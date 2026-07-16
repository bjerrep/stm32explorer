#!/usr/bin/env python3
"""Look up an STM32 part's price on the Digi-Key Product Information API (v4).

Digi-Key is the fallback for parts Mouser doesn't stock/price (see
mouser_lookup.py). Unlike Mouser's bare API key, Digi-Key uses OAuth2
client-credentials: you register an app at

    https://developer.digikey.com/   ->  create an app, enable "Product
    Information V4"

which gives a client id + secret. Then:

    export DIGIKEY_CLIENT_ID=...
    export DIGIKEY_CLIENT_SECRET=...

The interface mirrors mouser_lookup.price_for_part so batch_prices.py can call
either distributor the same way.

Usage:
    python3 digikey_lookup.py                 # demo: pick an MCU from the DB
    python3 digikey_lookup.py STM32F398VE     # base name -> resolved MPN
    python3 digikey_lookup.py STM32F398VET6   # already-orderable MPN
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

from mpn_resolve import (
    DEFAULT_TEMP, PKG_CODE, find_part, load_data, pkg_family, resolve_mpn,
)

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

# OAuth tokens live ~10 min; cache one so a whole batch reuses a single grant.
_token_cache = {"token": None, "expires": 0.0}


def credentials():
    """(client_id, client_secret) from the environment, either may be None."""
    return os.environ.get("DIGIKEY_CLIENT_ID"), os.environ.get("DIGIKEY_CLIENT_SECRET")


def have_credentials():
    cid, secret = credentials()
    return bool(cid and secret)


def get_token(client_id, client_secret, timeout=30):
    """Fetch (and cache) an OAuth2 client-credentials access token."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"] - 30:
        return _token_cache["token"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    token = resp.get("access_token")
    if not token:
        raise RuntimeError(f"no access_token in token response: {resp}")
    _token_cache["token"] = token
    _token_cache["expires"] = now + float(resp.get("expires_in", 600))
    return token


def digikey_search(keyword, token, client_id, limit=10, timeout=30):
    """Return the Digi-Key Products list for a keyword/MPN (may be empty)."""
    body = json.dumps({"Keywords": keyword, "Limit": limit, "Offset": 0}).encode()
    req = urllib.request.Request(SEARCH_URL, data=body, headers={
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": client_id,
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Currency": "USD",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return resp.get("Products") or []


def unit_price(product):
    """Cheapest listed price break (any quantity) for a Digi-Key product, or None.

    v4 hangs price breaks off each ProductVariations[].StandardPricing[]; some
    responses also carry a flat top-level UnitPrice. Take the min across all.
    """
    prices = []
    for v in (product.get("ProductVariations") or []):
        for b in (v.get("StandardPricing") or []):
            up = b.get("UnitPrice")
            if isinstance(up, (int, float)) and up > 0:
                prices.append(float(up))
    up = product.get("UnitPrice")
    if isinstance(up, (int, float)) and up > 0:
        prices.append(float(up))
    return round(min(prices), 4) if prices else None


def _mpn(product):
    return product.get("ManufacturerProductNumber") or ""


def _cheapest(products):
    """(price, matchedMPN) for the cheapest priced product in a list."""
    return min(
        ((unit_price(p), _mpn(p)) for p in products),
        key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0),
    )


def price_for_mpn(mpn, token, client_id):
    """Best unit price for an MPN, preferring an exact manufacturer-part match,
    else the cheapest of any returned product. Returns (price, matchedMPN)."""
    products = digikey_search(mpn, token, client_id)
    if not products:
        return None, None
    exact = [p for p in products if _mpn(p).upper() == mpn.upper()]
    cand = exact or products
    return _cheapest(cand)


def price_for_part(base, package, token, client_id):
    """Price a dataset base part with fallback. Returns (price, matchedMPN, how).

    Mirrors mouser_lookup.price_for_part:
    1. exact resolved MPN (base + package-code + temp '6');
    2. else the bare base part, preferring the same package family, then cheapest.
    """
    code = PKG_CODE.get(pkg_family(package))
    if code:
        price, matched = price_for_mpn(f"{base}{code}{DEFAULT_TEMP}", token, client_id)
        if price is not None:
            return price, matched, "exact"

    products = digikey_search(base, token, client_id)   # one call, all variants
    if not products:
        return None, None, "no-stock"
    same = [p for p in products if code and
            _mpn(p).upper().startswith(f"{base}{code}".upper())]
    if same:
        price, matched = _cheapest(same)
        if price is not None:
            return price, matched, "alt-temp"
    price, matched = _cheapest(products)
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

    cid, secret = credentials()
    if not (cid and secret):
        print("\nDIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not set — skipping live lookup.")
        print("Register an app at https://developer.digikey.com/ then:")
        print("  export DIGIKEY_CLIENT_ID=...  DIGIKEY_CLIENT_SECRET=...")
        return 0

    print("\nDigi-Key:")
    try:
        token = get_token(cid, secret)
        if explicit:
            price, matched = price_for_mpn(query, token, cid)
            how = "exact" if price is not None else "no-price"
        else:
            price, matched, how = price_for_part(base, package, token, cid)

        if price is None:
            print(f"  no price found  [{how}]")
        else:
            print(f"  ${price:.4f}  {matched}   [{how}]")

        if how != "exact":
            variants = digikey_search(base, token, cid)
            if variants:
                print(f"\n  variants Digi-Key stocks for {base}:")
                for v in sorted(variants, key=_mpn):
                    up = unit_price(v)
                    tag = f"${up:.4f}" if up is not None else "no price"
                    print(f"    {_mpn(v):22} {tag:>10}")
            else:
                print(f"  (Digi-Key returns nothing for base {base})")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"  API error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
