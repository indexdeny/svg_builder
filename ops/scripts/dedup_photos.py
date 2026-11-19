# -*- coding: utf-8 -*-
"""Deduplicate a photo folder (CR v3.4 §1).

Groups images by perceptual difference-hash (dHash, 8x8 -> 64-bit) with a small
Hamming tolerance so that thumbnails and re-encoded copies of the same picture
fall into one group. Within each group the HEAVIEST file (largest byte size =
best quality) is kept; the lighter duplicates are deleted.

Dependency-free (PIL + numpy only). Dry-run by default; pass --apply to delete.

Usage:
    python ops/scripts/dedup_photos.py <folder> [--apply] [--hamming N]
"""
import os
import sys
import json

import numpy as np
from PIL import Image, ImageOps

HAMMING_MAX = 6          # <= this many differing bits => same picture
HASH_SIZE = 8            # 8x8 dHash needs a 9x8 resize


def dhash_and_area(path):
    """Returns (64-bit difference hash, pixel area). (None, 0) if unreadable."""
    try:
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        area = im.width * im.height
        g = im.convert('L').resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
        a = np.asarray(g, dtype=np.int16)
        diff = a[:, 1:] > a[:, :-1]
        bits = 0
        for b in diff.flatten():
            bits = (bits << 1) | int(b)
        return bits, area
    except Exception as e:
        print(f"[WARN] cannot hash {path}: {e}")
        return None, 0


def hamming(a, b):
    return bin(a ^ b).count('1')


def build_groups(items):
    """items: list of (path, size, area, hash). Returns list of groups (lists of
    items). Single-linkage clustering by Hamming distance <= HAMMING_MAX."""
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            if hamming(items[i][3], items[j][3]) <= HAMMING_MAX:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    return list(groups.values())


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    folder = sys.argv[1]
    apply = '--apply' in sys.argv
    global HAMMING_MAX
    if '--hamming' in sys.argv:
        HAMMING_MAX = int(sys.argv[sys.argv.index('--hamming') + 1])

    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
             and os.path.isfile(os.path.join(folder, f))]

    items = []
    for p in files:
        h, area = dhash_and_area(p)
        if h is not None:
            items.append((p, os.path.getsize(p), area, h))

    groups = build_groups(items)
    dups = [g for g in groups if len(g) > 1]

    to_delete = []
    report = []
    for g in groups:
        # Best version first: highest pixel resolution, byte size as tie-break.
        # (Bytes alone is misleading — a heavier thumbnail can be lower-res.)
        g_sorted = sorted(g, key=lambda it: (it[2], it[1]), reverse=True)
        keep = g_sorted[0]
        drop = g_sorted[1:]
        if drop:
            report.append({
                'keep': os.path.basename(keep[0]),
                'keep_dim_kb': f"{keep[2]}px {round(keep[1] / 1024, 1)}KB",
                'delete': [os.path.basename(d[0]) for d in drop],
                'delete_dim_kb': [f"{d[2]}px {round(d[1] / 1024, 1)}KB" for d in drop],
            })
            to_delete.extend(d[0] for d in drop)

    print(f"Scanned files:        {len(items)}")
    print(f"Distinct images:      {len(groups)}")
    print(f"Duplicate groups:     {len(dups)}")
    print(f"Files to delete:      {len(to_delete)}")
    print(f"Survivors:            {len(items) - len(to_delete)}")
    print()
    for r in report:
        print(f"  keep {r['keep']} ({r['keep_dim_kb']})  <-  "
              f"delete {list(zip(r['delete'], r['delete_dim_kb']))}")

    os.makedirs('ops/logs', exist_ok=True)
    with open('ops/logs/dedup_photos.json', 'w', encoding='utf-8') as f:
        json.dump({'scanned': len(items), 'distinct': len(groups),
                   'deleted': len(to_delete), 'groups': report}, f,
                  ensure_ascii=False, indent=2)

    if apply:
        from reference_whitelist import processing_directory
        processing_directory(folder)
        for p in to_delete:
            os.remove(p)
        print(f"\n[APPLIED] Deleted {len(to_delete)} lighter duplicate(s).")
    else:
        print("\n[DRY-RUN] No files deleted. Re-run with --apply to delete.")


if __name__ == '__main__':
    main()
