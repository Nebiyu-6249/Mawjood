#!/usr/bin/env python
"""Generate tagged QR codes for source attribution.

A QR encodes a click-to-chat link whose prefill is the source code::

    https://wa.me/9715XXXXXXXX?text=SRC12

Scanning it opens WhatsApp with ``SRC12`` already typed. The consumer sends it,
Mawjood parses the code, records where they came from, strips it from the text,
and gets on with the conversation.

    uv run python tools/make_qr.py --source SRC12
    uv run python tools/make_qr.py --source SRC12 --phone +97141234567 --out marina.svg
    uv run python tools/make_qr.py --source SRC12 --terminal

SVG by default: a QR on a flyer wants to be printed at whatever size the printer
feels like, and vector survives that.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import segno

from mawjood.core.attribution import SOURCE_PATTERN, build_wa_link, parse_source_code

DEFAULT_OUTPUT_DIR = Path("qr")


def normalise_source(raw: str) -> str:
    """Accept ``12``, ``SRC12`` or ``src12`` and return the canonical form."""
    candidate = raw if raw.upper().startswith("SRC") else f"SRC{raw}"
    parsed = parse_source_code(candidate)
    if parsed is None:
        raise SystemExit(
            f"invalid source code {raw!r}: expected SRC followed by 2-16 letters or "
            f"digits, matching {SOURCE_PATTERN.pattern}"
        )
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an attribution QR code.")
    parser.add_argument("--source", required=True, help="source code, e.g. SRC12")
    parser.add_argument(
        "--phone",
        default="+971500000000",
        help="the WhatsApp number the QR opens (E.164). Defaults to a placeholder "
        "because no number is provisioned yet.",
    )
    parser.add_argument("--out", help="output path (.svg or .png). Defaults to qr/<SOURCE>.svg")
    parser.add_argument("--terminal", action="store_true", help="also print it to the terminal")
    parser.add_argument("--scale", type=int, default=8, help="module scale for raster output")
    args = parser.parse_args()

    source = normalise_source(args.source)
    link = build_wa_link(args.phone, source)
    qr = segno.make(link, error="h")

    if args.terminal:
        qr.terminal(compact=True)

    out = Path(args.out) if args.out else DEFAULT_OUTPUT_DIR / f"{source}.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    # High error correction so it still scans off a scuffed table card.
    qr.save(str(out), scale=args.scale)

    print(f"source : {source}")
    print(f"link   : {link}")
    print(f"written: {out}")

    if args.phone == "+971500000000":
        print(
            "\nnote: no WhatsApp number is provisioned yet, so this points at a "
            "placeholder.\n      Re-run with --phone once the BSP number exists."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
