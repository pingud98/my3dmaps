#!/usr/bin/env python3
"""Render overlay.svg on top of overlay_relief_topview.png (both written by
``my3dmaps overlay`` / ``build``) to a PNG, to check the film against the relief.

    python3 tools/compose_overlay.py out/liverpool --px-per-mm 4 [--crop x,y,w,h]

Uses a headless Chromium (``chromium`` on PATH, or $CHROMIUM).  ``--crop`` (mm,
from the window's top-left) renders a zoomed detail instead of the whole sheet.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--px-per-mm", type=float, default=4.0)
    ap.add_argument("--crop", help="x,y,w,h in mm")
    ap.add_argument("--out")
    ap.add_argument("--no-relief", action="store_true", help="overlay alone on white (what the film printer gets)")
    a = ap.parse_args()
    d = Path(a.dir).resolve()
    svg = d / "overlay.svg"
    txt = svg.read_text()
    import re
    m = re.search(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"', txt)
    W, H = float(m.group(1)), float(m.group(2))
    x, y, w, h = (0, 0, W, H) if not a.crop else (float(v) for v in a.crop.split(","))
    k = a.px_per_mm
    bg = "" if a.no_relief else (f'<img src="{(d / "overlay_relief_topview.png").as_uri()}" '
                                 f'style="position:absolute;left:0;top:0;width:{W * k}px;height:{H * k}px;'
                                 f'image-rendering:auto">')
    page = (f'<!doctype html><html><body style="margin:0;background:#fff;overflow:hidden">'
            f'<div style="position:relative;width:{w * k}px;height:{h * k}px;overflow:hidden">'
            f'<div style="position:absolute;left:{-x * k}px;top:{-y * k}px">{bg}'
            f'<img src="{svg.as_uri()}" style="position:absolute;left:0;top:0;width:{W * k}px;height:{H * k}px">'
            f'</div></div></body></html>')
    out = Path(a.out) if a.out else d / ("overlay_check.png" if not a.crop else "overlay_detail.png")
    exe = os.environ.get("CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser") or "google-chrome"
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "page.html"
        hp.write_text(page)
        subprocess.run([exe, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--allow-file-access-from-files", f"--window-size={int(w * k)},{int(h * k)}",
                        f"--screenshot={out.resolve()}", hp.as_uri()], check=True, capture_output=True, timeout=180)
    print(out)


if __name__ == "__main__":
    main()
