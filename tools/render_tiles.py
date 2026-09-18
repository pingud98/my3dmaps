#!/usr/bin/env python3
"""Render PNG views of built tiles (and the assembled tile set) from a project's
``preview.glb`` files, using headless Chromium + Three.js.

    python3 tools/render_tiles.py out/liverpool --out docs/images --prefix liverpool

Needs ``playwright`` with Chromium installed (``pip install playwright &&
playwright install chromium``); it is a dev/documentation tool, not part of
the pipeline.  Three.js is loaded from a CDN, so it needs network access.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;background:#1b1f27}canvas{display:block}</style>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}</script></head><body>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
const W = __W__, H = __H__;
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setSize(W, H); renderer.setPixelRatio(1); document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x1b1f27);
scene.add(new THREE.HemisphereLight(0xffffff, 0x666677, 1.1));
const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(-0.6, 1.0, 0.8); scene.add(sun);
const fill = new THREE.DirectionalLight(0xffffff, 0.35); fill.position.set(0.8, 0.5, -0.6); scene.add(fill);
const loader = new GLTFLoader();
const items = __ITEMS__;   // [{url, dx, dy}] in mm; model X east, Y north, Z up -> three: x, z=-y, y
const group = new THREE.Group(); scene.add(group);
for (const it of items) {
  const g = await new Promise((res, rej) => loader.load(it.url, res, undefined, rej));
  g.scene.traverse(o => { if (o.isMesh) { o.material.side = THREE.DoubleSide; o.material.flatShading = true; o.material.roughness = 0.85; o.geometry.computeVertexNormals(); } });
  g.scene.rotation.x = -Math.PI / 2;               // Z-up -> Y-up
  g.scene.position.set(it.dx, 0, -it.dy);
  group.add(g.scene);
}
const box = new THREE.Box3().setFromObject(group);
const size = new THREE.Vector3(); box.getSize(size); const c = new THREE.Vector3(); box.getCenter(c);
const span = Math.max(size.x, size.z);
const camera = new THREE.PerspectiveCamera(32, W / H, 1, 20000);
const dist = span * __DIST__;
camera.position.set(c.x - dist * 0.55, c.y + dist * 0.62, c.z + dist * 0.75);   // from the south-west, elevated
camera.lookAt(c.x, c.y + size.y * 0.15, c.z);
renderer.render(scene, camera);
window.__done = { triangles: renderer.info.render.triangles };
</script></body></html>
"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # noqa: D102
        pass


def serve(root: Path) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(_Quiet, directory=str(root))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def render(page, out_png: Path, items: list[dict], w: int, h: int, dist: float, root: Path, port: int) -> dict:
    html = PAGE.replace("__ITEMS__", json.dumps(items)).replace("__W__", str(w)).replace("__H__", str(h)) \
        .replace("__DIST__", str(dist))
    (root / "_render.html").write_text(html)
    await page.goto(f"http://127.0.0.1:{port}/_render.html")
    await page.wait_for_function("window.__done !== undefined", timeout=600000)
    await page.screenshot(path=str(out_png))
    return await page.evaluate("window.__done")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project_dir", help="a my3dmaps build directory (contains summary.json)")
    ap.add_argument("--out", default="docs/images")
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=950)
    ap.add_argument("--gap-mm", type=float, default=0.0, help="gap between tiles in the assembly view")
    args = ap.parse_args()
    root = Path(args.project_dir).resolve()
    summary = json.loads((root / "summary.json").read_text())
    prefix = args.prefix or summary["name"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tile_mm = float(json.loads((root / "project.json").read_text())["tile_mm"])
    bed_off = (256.0 - tile_mm) / 2.0

    from playwright.async_api import async_playwright

    srv, port = serve(root)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = await browser.new_page(viewport={"width": args.width, "height": args.height})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            for t in summary["tiles"]:
                png = out / f"{prefix}_{t['name']}.png"
                info = await render(page, png, [{"url": f"{t['name']}/preview.glb", "dx": 0, "dy": 0}],
                                    args.width, args.height, 2.05, root, port)
                print(f"{png}  ({info['triangles']} triangles)")
            items = [{"url": f"{t['name']}/preview.glb", "dx": t["tx"] * (tile_mm + args.gap_mm) - bed_off,
                      "dy": t["ty"] * (tile_mm + args.gap_mm) - bed_off} for t in summary["tiles"]]
            png = out / f"{prefix}_assembly.png"
            info = await render(page, png, items, args.width, args.height, 1.9, root, port)
            print(f"{png}  ({info['triangles']} triangles)")
            if errors:
                print("page errors:", errors)
            await browser.close()
    finally:
        srv.shutdown()
        (root / "_render.html").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
