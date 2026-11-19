# -*- coding: utf-8 -*-
"""Preflight check for a single raster (TZ v3.1 Monochrome Standard).

Classifies the image, runs the matching engine (sketch or monochrome fill),
normalizes to the black-on-transparent standard, validates RULE 1-5, and
writes a side-by-side preview. Does NOT touch the knowledge graph — dry run.

Usage: python ops/scripts/preflight.py 01_input_raster/<allowed-category>/<image>
Output: ops/qa_previews/preflight/_pre_<stem>.svg   candidate SVG (release standard)
        ops/qa_previews/preflight/_pre_<stem>.png   original | rendered (on white)
        ops/qa_previews/preflight/_pre_<stem>.json  metrics + classification + rules
"""
import json
import os
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as classifier
import hifi_vectorize as hv
import sketch_vectorize as sk
from compile_svg import normalize_monochrome, validate_monochrome

os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


def main():
    from reference_whitelist import verify_path
    image_path = str(verify_path(sys.argv[1]))
    stem = os.path.splitext(os.path.basename(image_path))[0]
    out_dir = 'ops/qa_previews/preflight'
    os.makedirs(out_dir, exist_ok=True)

    cls = classifier.classify(image_path)
    info = {'image': os.path.basename(image_path), **{k: cls[k] for k in (
        'input_type', 'render_mode', 'vector_detail_level', 'is_tonal',
        'source_width', 'source_height', 'aspect_ratio')}}

    t0 = time.time()
    if cls['render_mode'] == 'SKETCH_VECTOR':
        res = sk.vectorize_sketch(image_path, cls['vector_detail_level'])
    else:
        res = hv.vectorize_monochrome(image_path, cls['vector_detail_level'])
    svg = normalize_monochrome(res['svg'])
    rule_errors = validate_monochrome(svg)

    info.update({
        'seconds': round(time.time() - t0, 1),
        'ssim': round(res['ssim_ref'], 4),
        'gate_passed': res['gate_passed'],
        'svg_kb': len(svg.encode('utf-8')) // 1024,
        'rule_violations': rule_errors,
        'standard_ok': not rule_errors,
    })

    with open(f'{out_dir}/_pre_{stem}.svg', 'w', encoding='utf-8') as f:
        f.write(svg)

    rgb = hv.load_image_flattened(image_path)
    h, w = rgb.shape[:2]
    rendered = hv.render_svg(svg, w, h)            # composited on white for viewing
    sep = np.full((h, 8, 3), 255, np.uint8)
    side = Image.fromarray(np.concatenate([rgb, sep, rendered], axis=1))
    if side.width > 1600:
        side = side.resize((1600, int(side.height * 1600 / side.width)), Image.LANCZOS)
    side.save(f'{out_dir}/_pre_{stem}.png')

    with open(f'{out_dir}/_pre_{stem}.json', 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print(json.dumps(info, ensure_ascii=False))


if __name__ == '__main__':
    main()
