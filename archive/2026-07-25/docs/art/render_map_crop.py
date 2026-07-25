#!/usr/bin/env python3
"""Render a true-to-game crop of the village tilemap (visual layers only).

Usage: python3 render_map_crop.py X0 Y0 X1 Y1 out.png  (tile coords, inclusive)
Run from frontend/public/assets/village/tilemap/. Used to produce the
post-office site survey (docs/art/post-office-site.png) — rerun after editing
tilemap.json to preview the result without booting the game.
"""
import json, os, sys
from PIL import Image, ImageDraw

X0, Y0, X1, Y1 = map(int, sys.argv[1:5])
OUT = sys.argv[5] if len(sys.argv) > 5 else "crop.png"
d = json.load(open("tilemap.json"))
TW, W = d["tilewidth"], d["width"]
tilesets = []
for t in d["tilesets"]:
    f = os.path.basename(t["image"])
    if t["name"] == "blocks": f = "blocks_1.png"
    if os.path.exists(f):
        im = Image.open(f).convert("RGBA")
        tilesets.append((t["firstgid"], im, im.width // TW))
tilesets.sort(key=lambda x: x[0])

def tile(gid):
    gid &= 0x0FFFFFFF
    if not gid: return None
    src = None
    for fg, im, cols in tilesets:
        if gid >= fg: src = (fg, im, cols)
        else: break
    if not src: return None
    fg, im, cols = src
    i = gid - fg
    return im.crop(((i % cols) * TW, (i // cols) * TW, (i % cols + 1) * TW, (i // cols + 1) * TW))

VISUAL = ["Bottom Ground", "Exterior Ground", "Exterior Decoration L1", "Exterior Decoration L2",
          "Interior Ground", "Wall", "Interior Furniture L1", "Interior Furniture L2 ",
          "Foreground L1", "Foreground L2"]
canvas = Image.new("RGBA", ((X1 - X0 + 1) * TW, (Y1 - Y0 + 1) * TW), (0, 0, 0, 255))
for name in VISUAL:
    layer = next((l for l in d["layers"] if l["name"] == name), None)
    if not layer: continue
    for ty in range(Y0, Y1 + 1):
        for tx in range(X0, X1 + 1):
            t = tile(layer["data"][ty * W + tx])
            if t: canvas.alpha_composite(t, ((tx - X0) * TW, (ty - Y0) * TW))
canvas.resize((canvas.width * 2, canvas.height * 2), Image.NEAREST).save(OUT)
print("saved", OUT)
