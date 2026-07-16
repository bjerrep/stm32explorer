#!/usr/bin/env python3
"""Resolve an orderable STM32 MPN from a dataset base part name.

The dataset (`stm32_data.json`) stores *base* part numbers such as
`STM32F407VG`, which are not directly orderable. An orderable MPN appends the
two fields the selector data omits:

    <base><package-code><temperature-range>
    e.g.  STM32F407VG + LQFP + (-40..85C)  ->  STM32F407VGT6

This is a best-effort guess. LQFP / QFN / WLCSP / TSSOP codes are reliable;
BGA codes vary between families, so those are the likeliest to miss.

Source-agnostic — used by both the single-part probe and the batch writer.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (scripts/ is one down)
DATA_FILE = os.path.join(ROOT, "stm32_data.json")
JS_FILE = os.path.join(ROOT, "stm32_data.js")                       # file:// sidecar

# ST package family -> the package-code letter in an orderable MPN.
# BGA families all collapse to 'H' (common case; exact code varies by line).
PKG_CODE = {
    "LQFP": "T",
    "UFQFPN": "U", "VFQFPN": "U",       # QFN family
    "WLCSP": "Y",
    "UFBGA": "H", "TFBGA": "H", "VFBGA": "H", "LFBGA": "H",  # BGA family
    "TSSOP": "P",
    "SO": "M",
    "LGA": "E",
}

# Temperature-range suffix. 6 = -40..+85C (industrial) is ST's standard default.
DEFAULT_TEMP = "6"


def pkg_family(package):
    """Leading alpha run of a package string: 'LQFP64' -> 'LQFP'."""
    m = re.match(r"[A-Za-z]+", package or "")
    return m.group(0).upper() if m else ""


def resolve_mpn(part, package, temp=DEFAULT_TEMP):
    """Base part + package + temp -> best-effort orderable MPN, or None."""
    code = PKG_CODE.get(pkg_family(package))
    if not code:
        return None
    return f"{part}{code}{temp}"


def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_data(data):
    """Atomic write of stm32_data.json, matching stm32_ripper.py's format
    (indent=1). Also refreshes the stm32_data.js sidecar so file:// autoload
    (and anything loading the sidecar) never lags behind freshly-written prices.
    """
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

    tmp = JS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.STM32_DATA = ")
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    os.replace(tmp, JS_FILE)


def find_part(parts, query):
    """Match the dataset by base part number (exact, then substring)."""
    q = query.strip().upper()
    for p in parts:
        if p["part"].upper() == q:
            return p
    for p in parts:
        if q in p["part"].upper():
            return p
    return None
