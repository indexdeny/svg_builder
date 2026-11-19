# -*- coding: utf-8 -*-
"""Rename surviving images to <prefix>_NNN.<ext> (CR v3.4 §2).

Order = natural by the numeric id parsed from '<prefix>_<N>...'; if no such id
(arbitrary names like IMG_8198), natural sort by filename. The ORIGINAL
extension is preserved (lowercased; .jpeg -> .jpg) so PNG/WEBP stay intact.
Two-pass (temp -> final) so no rename collides with an existing source name.

Usage: python ops/scripts/rename_photos.py <folder> [--prefix phone] [--apply]
"""
import os
import re
import sys
import json

EXTS = ('.jpg', '.jpeg', '.png', '.webp')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    folder = sys.argv[1]
    apply = '--apply' in sys.argv
    if apply:
        from reference_whitelist import processing_directory
        processing_directory(folder)
    prefix = sys.argv[sys.argv.index('--prefix') + 1] if '--prefix' in sys.argv else 'photo'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', prefix):
        raise ValueError('Prefix must contain only ASCII letters, digits, underscores and hyphens')

    files = [f for f in os.listdir(folder)
             if f.lower().endswith(EXTS) and os.path.isfile(os.path.join(folder, f))]

    def nat_key(name):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r'(\d+)', name)]

    files.sort(key=nat_key)

    def ext_of(name):
        e = os.path.splitext(name)[1].lower()
        return '.jpg' if e == '.jpeg' else e

    plan = [(old, f"{prefix}_{i:03d}{ext_of(old)}") for i, old in enumerate(files, 1)]

    for old, new in plan:
        print(f"  {old:50s} -> {new}")
    print(f"\nTotal: {len(plan)} file(s).")

    os.makedirs('ops/logs', exist_ok=True)
    with open('ops/logs/rename_photos.json', 'w', encoding='utf-8') as f:
        json.dump([{'old': o, 'new': n} for o, n in plan], f,
                  ensure_ascii=False, indent=2)

    if not apply:
        print("\n[DRY-RUN] No files renamed. Re-run with --apply.")
        return

    # Pass 1: to unique temp names. Pass 2: temp -> final.
    tmp = []
    for i, (old, new) in enumerate(plan):
        t = f"__tmp_{i:03d}__.jpg"
        os.rename(os.path.join(folder, old), os.path.join(folder, t))
        tmp.append((t, new))
    for t, new in tmp:
        os.rename(os.path.join(folder, t), os.path.join(folder, new))
    print(f"\n[APPLIED] Renamed {len(plan)} file(s) to {prefix}_NNN.<ext>.")


if __name__ == '__main__':
    main()
