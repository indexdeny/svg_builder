# -*- coding: utf-8 -*-
"""A00 de-chrome: auto-crop phone/app UI off social-media screenshots (CR v3.4 §3-4).

Many input screenshots wrap the artwork in chrome — phone status bar, app
header, like/comment bars, bottom navigation. This stage finds where the
artwork begins and where the UI is, and crops to the artwork only, leaving a
clean image ready for recognition/render.

Method (deterministic, local, offline):
  The artwork is a large region with a light (white/cream/grey) background; the
  UI chrome is either dark solid bands (dark theme) or dense text/icon bands.
  We profile the fraction of "light" pixels per row and per column and take the
  largest contiguous light run (bridging small gaps) as the artwork extent,
  iterating rows<->columns to tighten the box. Covers both regimes:
    (A) a light artwork rectangle floating in dark UI  -> find the rectangle;
    (B) artwork filling the frame + a thin dark nav strip -> trim the strip.

Safety (never destroy the picture):
  - cropped side >= MIN_DIM_FRAC of the original, sane aspect, long side >= 256;
  - each trimmed margin must be clearly DARKER than the kept region (UI-like),
    else the trim is treated as low-confidence and skipped;
  - if the box is ~the whole frame the image is already clean (no crop);
  - originals that get cropped are copied to a backup dir first (revertable).

Usage:
    python ops/scripts/autocrop.py <folder> [--apply] [--backup DIR]
    (dry-run by default; writes ops/logs/autocrop_report.json either way)
API:
    process_folder(folder, apply=False, backup_dir=None) -> list[dict]
    analyze_image(path) -> dict   # decision for a single file, no side effects
"""
import os
import sys
import json
import shutil

import numpy as np
from PIL import Image, ImageOps

# --- Conservative de-chrome: only shave solid dark UI bands off the EDGES, and
# only when the kept region is a near-white sketch page. This structurally
# cannot cut into bright artwork (it stops at the first non-dark edge row/col),
# and the white-page gate keeps color/dark PAINTINGS untouched (they have no
# large near-white background, so they fall through to 'clean'/'skip').
DARK_PIX = 64            # pixel luma < this counts as "dark"
DARK_FRAC = 0.62         # an edge row/col is a UI band if >= this fraction is dark
WHITE_PIX = 200          # pixel luma > this counts as near-white "paper"
KEEP_WHITE_FRAC = 0.30   # kept region must be at least this much near-white paper
BOUNDARY_MIN_MEAN = 165  # artwork-side boundary of a trimmed band must be this bright
                         # (a clean white-paper margin). A darker boundary means the
                         # band is background around a subject, or art reaches the
                         # edge -> don't trim (leave whole; backups exist anyway)
BRIGHT_GAP = 35          # kept region mean must exceed trimmed-band mean by this
MIN_KEEP_DIM = 0.50      # each kept side must be >= 50% of the original side
MIN_KEEP_AREA = 0.45     # kept area must be >= 45% of the original area
MIN_LONG_SIDE = 256      # README minimum (256x256)
EDGE_TRIM_FRAC = 0.010   # a side counts as "trimmed" if cut by >= 1% of the dim


def _luma(path):
    im = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
    a = np.asarray(im, dtype=np.float32)
    g = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    return g, im


def _edge_run(is_band, from_end):
    """Length of the contiguous True run starting from one edge of a 1-D array."""
    n = len(is_band)
    rng = range(n - 1, -1, -1) if from_end else range(n)
    c = 0
    for i in rng:
        if is_band[i]:
            c += 1
        else:
            break
    return c


def _detect_bbox(gray):
    """Trim contiguous solid-dark bands off the TOP and BOTTOM edges (UI chrome).
    Left/right are never trimmed: phone/app chrome on portrait screenshots is
    horizontal, so a dark background beside a light subject can't clip artwork."""
    H, W = gray.shape
    dark = gray < DARK_PIX
    row_band = dark.mean(axis=1) >= DARK_FRAC   # full-width dark rows
    top = _edge_run(row_band, False)
    bot = _edge_run(row_band, True)
    return 0, top, W, H - bot


def analyze_image(path):
    """Decide what to do with one file. No side effects."""
    gray, im = _luma(path)
    H, W = gray.shape
    x0, y0, x1, y1 = _detect_bbox(gray)

    # Validate each trimmed side at the artwork-side boundary: it must be a
    # near-white paper edge. This is what separates a true white sketch PAGE
    # (UI band, then white margin) from a pale SUBJECT on a dark background
    # (where the "dark band" is just the background continuing). Any trim whose
    # inner boundary is not predominantly white is reverted to no-trim.
    sv = max(4, int(0.012 * H))
    sh = max(4, int(0.012 * W))
    if y0 > 0 and float(gray[y0:y0 + sv, :].mean()) < BOUNDARY_MIN_MEAN:
        y0 = 0
    if y1 < H and float(gray[y1 - sv:y1, :].mean()) < BOUNDARY_MIN_MEAN:
        y1 = H
    if x0 > 0 and float(gray[:, x0:x0 + sh].mean()) < BOUNDARY_MIN_MEAN:
        x0 = 0
    if x1 < W and float(gray[:, x1 - sh:x1].mean()) < BOUNDARY_MIN_MEAN:
        x1 = W
    cw, ch = x1 - x0, y1 - y0

    res = {'file': os.path.basename(path), 'src_w': W, 'src_h': H,
           'bbox': [int(x0), int(y0), int(x1), int(y1)],
           'status': 'clean', 'reasons': []}

    trimmed = {'top': int(y0), 'bottom': int(H - y1),
               'left': int(x0), 'right': int(W - x1)}
    trimmed_any = (y0 >= EDGE_TRIM_FRAC * H or (H - y1) >= EDGE_TRIM_FRAC * H or
                   x0 >= EDGE_TRIM_FRAC * W or (W - x1) >= EDGE_TRIM_FRAC * W)
    if not trimmed_any or cw <= 0 or ch <= 0:
        return res

    # safety: dark bands must not consume most of the image (else it is probably
    # a dark artwork, not chrome) — keep it whole instead of gutting it.
    if cw < MIN_KEEP_DIM * W or ch < MIN_KEEP_DIM * H or cw * ch < MIN_KEEP_AREA * W * H:
        res['status'] = 'skip_safety'
        res['reasons'].append('dark bands too large (likely dark artwork)')
        return res
    if max(cw, ch) < MIN_LONG_SIDE:
        res['status'] = 'skip_safety'; res['reasons'].append('below min long side')
        return res

    kept = gray[y0:y1, x0:x1]
    kept_mean = float(kept.mean())
    white_frac = float((kept > WHITE_PIX).mean())

    # gate 1: the kept region must contain a real white sketch page. Combined
    # with the bright-boundary check above (which already rejected trims around
    # subjects-on-dark like a pale figure), this keeps colour/dark paintings whole.
    if white_frac < KEEP_WHITE_FRAC:
        res['status'] = 'skip_lowconf'
        res['reasons'].append(f'kept region not white-page (white_frac={white_frac:.2f})')
        return res

    # gate 2: every meaningfully trimmed band must be clearly darker than the page.
    bands = {'top': gray[:y0, :], 'bottom': gray[y1:, :],
             'left': gray[:, :x0], 'right': gray[:, x1:]}
    thr = {'top': EDGE_TRIM_FRAC * H, 'bottom': EDGE_TRIM_FRAC * H,
           'left': EDGE_TRIM_FRAC * W, 'right': EDGE_TRIM_FRAC * W}
    for side, band in bands.items():
        if trimmed[side] >= thr[side] and band.size:
            if float(band.mean()) > kept_mean - BRIGHT_GAP:
                res['status'] = 'skip_lowconf'
                res['reasons'].append(f'{side} band not dark enough')
                return res

    res['status'] = 'crop'
    res['trimmed'] = trimmed
    res['kept_frac'] = round(cw * ch / (W * H), 3)
    res['white_frac'] = round(white_frac, 3)
    return res


def process_folder(folder, apply=False, backup_dir=None):
    if apply:
        from reference_whitelist import processing_directory
        processing_directory(folder)
    if backup_dir is None:
        backup_dir = os.path.join(folder, '_precrop_originals')
    if apply:
        processing_directory(backup_dir, allow_missing=True)
        if os.path.abspath(backup_dir) == os.path.abspath(folder):
            raise ValueError('Backup requires a separate processing output directory')
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                   and os.path.isfile(os.path.join(folder, f)))
    report = []
    for f in files:
        path = os.path.join(folder, f)
        try:
            d = analyze_image(path)
        except Exception as e:
            d = {'file': f, 'status': 'error', 'reasons': [str(e)]}
        report.append(d)
        if apply and d['status'] == 'crop':
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(backup_dir, f))
            im = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
            x0, y0, x1, y1 = d['bbox']
            im.crop((x0, y0, x1, y1)).save(path, quality=95)
    counts = {}
    for d in report:
        counts[d['status']] = counts.get(d['status'], 0) + 1
    os.makedirs('ops/logs', exist_ok=True)
    with open('ops/logs/autocrop_report.json', 'w', encoding='utf-8') as fp:
        json.dump({'folder': folder, 'apply': apply, 'backup_dir': backup_dir,
                   'counts': counts, 'items': report}, fp,
                  ensure_ascii=False, indent=2)
    return report, counts


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    folder = sys.argv[1]
    apply = '--apply' in sys.argv
    backup_dir = None
    if '--backup' in sys.argv:
        backup_dir = sys.argv[sys.argv.index('--backup') + 1]

    report, counts = process_folder(folder, apply=apply, backup_dir=backup_dir)
    for d in report:
        line = f"  {d['file']:16s} {d['status']:13s}"
        if d.get('bbox'):
            line += f" bbox={d['bbox']}"
        if d.get('reasons'):
            line += f"  ({'; '.join(d['reasons'])})"
        print(line)
    print(f"\nCounts: {counts}")
    print("[APPLIED]" if apply else "[DRY-RUN] re-run with --apply to crop "
          "(originals are backed up first).")


if __name__ == '__main__':
    main()
