# -*- coding: utf-8 -*-
"""Render autocrop decisions as bbox overlays for visual QA (CR v3.4 §6).

For each image draws the detected crop rectangle (red) on a downscaled copy and
writes it under ops/qa_previews/autocrop/<status>/<file>.png, so the crop can be
judged at a glance (what is kept vs trimmed) before applying.

Usage: python ops/scripts/preview_autocrop.py <folder>
"""
import os
import sys

from PIL import Image, ImageOps, ImageDraw

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import autocrop

OUT = 'ops/qa_previews/autocrop'
MAXH = 380


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Pass an explicit approved source category folder')
    folder = sys.argv[1]
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')))
    for f in files:
        path = os.path.join(folder, f)
        from reference_whitelist import verify_path
        verify_path(path)
        try:
            d = autocrop.analyze_image(path)
        except Exception as e:
            print(f"[WARN] {f}: {e}")
            continue
        im = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
        dr = ImageDraw.Draw(im)
        if d.get('bbox'):
            x0, y0, x1, y1 = d['bbox']
            if x1 > x0 and y1 > y0:
                w = max(3, im.width // 200)
                dr.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(255, 0, 0), width=w)
        scale = MAXH / im.height if im.height > MAXH else 1.0
        if scale < 1.0:
            im = im.resize((max(1, int(im.width * scale)), MAXH), Image.LANCZOS)
        d_out = os.path.join(OUT, d['status'])
        os.makedirs(d_out, exist_ok=True)
        im.save(os.path.join(d_out, os.path.splitext(f)[0] + '.png'))
    print(f"Overlays written under {OUT}/<status>/")


if __name__ == '__main__':
    main()
