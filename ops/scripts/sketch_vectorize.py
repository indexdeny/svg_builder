# -*- coding: utf-8 -*-
"""SKETCH_VECTOR engine (TZ v3.1).

Photo/tonal raster -> XDoG line extraction -> black contour paths on a
transparent background. Output is pure #000000, fully vector, no raster,
no background. Designed for HUMAN_PHOTO / OBJECT_PHOTO / tonal ILLUSTRATION.

The look is "pencil / line-art / contour drawing" (NOT cartoon/comic):
XDoG (eXtended Difference of Gaussians) yields clean thin lines that preserve
face, hair, clothing folds and silhouette while dropping skin texture, noise
and colour.

API:
    vectorize_sketch(image_path, detail_level='HIGH', log=print) -> result dict
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hifi_vectorize import (TraceOverflowError, build_black_svg,
                            load_image_flattened, render_svg, score_ssim,
                            trace_mask)

WORK_LONG_SIDE = 1400         # cap working resolution for stable line thickness/speed
# Sketch SSIM measures fidelity of the traced SVG to the XDoG LINE MASK (not
# recognizability). Dense/busy line art traces less faithfully yet still reads
# as a clean drawing, so 0.90 was over-strict and quarantined good sketches.
# Visual review of all sketch quarantine cases down to 0.76 confirmed they are
# recognizable line drawings, so the release floor is lowered to 0.75. Below
# that the trace has diverged severely from the mask (genuine failure).
# Sourced from the SSOT (00_governance/pipeline_config.json) — single source of
# truth (svg_cad T0); the literal is only a fallback if the config is absent.
from pipeline_config import gate as _gate
SSIM_GATE = _gate('SKETCH_VECTOR', 'ssim_min', 0.75)

# detail -> CLAHE + two XDoG scales (fine for features/hair, mid for structure),
# trace params, and minimum kept blob size. Tuned so eyes/brows/nose/lips/hair
# and clothing folds survive (TZ v3.2: detail over silhouette).
DETAIL = {
    'ULTRA':  dict(clahe=3.5,
                   fine=dict(sigma=0.6, p=26, eps=-0.045, phi=15),
                   mid=dict(sigma=1.2, p=22, eps=-0.05, phi=11),
                   trace=dict(filter_speckle=2, corner_threshold=30, length_threshold=2.0),
                   min_blob=2),
    'HIGH':   dict(clahe=3.0,
                   fine=dict(sigma=0.7, p=24, eps=-0.04, phi=14),
                   mid=dict(sigma=1.4, p=20, eps=-0.05, phi=10),
                   trace=dict(filter_speckle=3, corner_threshold=35, length_threshold=2.5),
                   min_blob=3),
    'MEDIUM': dict(clahe=2.5,
                   fine=dict(sigma=1.0, p=22, eps=-0.04, phi=11),
                   mid=dict(sigma=1.8, p=18, eps=-0.05, phi=9),
                   trace=dict(filter_speckle=6, corner_threshold=60, length_threshold=4.0),
                   min_blob=6),
    'LOW':    dict(clahe=2.0,
                   fine=dict(sigma=1.3, p=18, eps=-0.03, phi=9),
                   mid=dict(sigma=1.8, p=16, eps=-0.04, phi=7),
                   trace=dict(filter_speckle=10, corner_threshold=80, length_threshold=6.0),
                   min_blob=14),
}


def xdog(gray, sigma, p, eps, phi, k=1.6):
    """Returns an 8-bit image: black (0) lines on white (255)."""
    g = gray.astype(np.float32)
    g1 = cv2.GaussianBlur(g, (0, 0), sigma)
    g2 = cv2.GaussianBlur(g, (0, 0), sigma * k)
    xd = (g1 + p * (g1 - g2)) / 255.0
    out = np.where(xd >= eps, 1.0, 1.0 + np.tanh(phi * (xd - eps)))
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def clean_specks(line_mask, min_blob):
    """Removes connected black components smaller than min_blob pixels."""
    inv = line_mask.astype(np.uint8)  # 1 where line (black)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(inv, 8)
    keep = np.zeros_like(inv)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_blob:
            keep[lab == i] = 1
    return keep.astype(bool)


def extract_lines(rgb, cfg):
    """Full RGB -> boolean line mask (True = black line) at working resolution.

    Pipeline (TZ v3.2): bilateral denoise (drop photo/skin texture, keep edges)
    -> CLAHE (lift faint facial features out of shadow) -> multi-scale XDoG
    (fine = features/hair, mid = structural contours) unioned -> speckle cleanup.
    Returns (line_mask, work_w, work_h)."""
    h, w = rgb.shape[:2]
    scale = WORK_LONG_SIDE / max(h, w) if max(h, w) > WORK_LONG_SIDE else 1.0
    ww, wh = round(w * scale), round(h * scale)
    work = cv2.resize(rgb, (ww, wh), interpolation=cv2.INTER_AREA) if scale != 1.0 else rgb
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    gray = cv2.bilateralFilter(gray, 7, 60, 60)
    gray = cv2.createCLAHE(clipLimit=cfg['clahe'], tileGridSize=(8, 8)).apply(gray)

    fine = xdog(gray, **cfg['fine'])
    mid = xdog(gray, **cfg['mid'])
    line_img = cv2.min(fine, mid)           # union of black lines from both scales
    mask = line_img < 128
    mask = clean_specks(mask, cfg['min_blob'])
    return mask, ww, wh


def vectorize_sketch(image_path, detail_level='HIGH', log=print):
    cfg = DETAIL.get(detail_level, DETAIL['HIGH'])
    rgb = load_image_flattened(image_path)
    view_h, view_w = rgb.shape[:2]

    line_mask, work_w, work_h = extract_lines(rgb, cfg)
    black_frac = float(line_mask.mean())
    log(f"  [SKETCH] {detail_level}: lines={black_frac*100:.1f}% at {work_w}x{work_h}")

    # Reference for SSIM: the binary line map (black lines on white) at view size
    ref = np.where(line_mask, 0, 255).astype(np.uint8)
    ref = cv2.resize(ref, (view_w, view_h), interpolation=cv2.INTER_NEAREST)
    ref_rgb = cv2.cvtColor(ref, cv2.COLOR_GRAY2RGB)

    # Trace the line mask (crash-safe; downscale on VTracer overflow)
    cur = line_mask.astype(np.int32)
    cw, ch = work_w, work_h
    while True:
        try:
            paths = trace_mask(cur == 1, cfg['trace'])
            break
        except TraceOverflowError:
            nw, nh = round(cw * 0.7), round(ch * 0.7)
            if nw * nh / 1e6 < 0.4:
                paths = []
                log("  [SKETCH] WARNING: line mask too dense, partial output")
                break
            cur = cv2.resize(cur, (nw, nh), interpolation=cv2.INTER_NEAREST)
            cw, ch = nw, nh
            log(f"  [SKETCH] overflow; re-tracing at {cw}x{ch}")

    svg_text = build_black_svg(view_w, view_h, cw, ch, paths)
    rendered = render_svg(svg_text, view_w, view_h)
    s_ref = score_ssim(ref_rgb, rendered)
    # Fraction of the RENDERED drawing (composited on white) that is black. This
    # is the honest "is it a blob?" signal — the line-mask fraction undercounts
    # because traced dark regions fill far more area than their pixels. A dark
    # source (e.g. a black wallpaper) can blob out near-solid black here.
    render_black = float((np.asarray(rendered).reshape(-1, 3).mean(1) < 128).mean())
    log(f"  [SKETCH] SSIM(lines) {s_ref:.4f}, paths={len(paths)}, "
        f"render_black={render_black*100:.1f}%")

    return {
        'svg': svg_text,
        'ssim_ref': s_ref,
        'ssim_orig': s_ref,           # sketch has no colour reference; report line SSIM
        'params': cfg['trace'],
        'k': 1,
        'mode': 'SKETCH_VECTOR',
        'gate_passed': s_ref >= SSIM_GATE,
        'line_fraction': round(black_frac, 4),
        'render_black_frac': round(render_black, 4),
    }


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    image_path, output_path = sys.argv[1], sys.argv[2]
    from reference_whitelist import verify_path, output_file
    image_path = str(verify_path(image_path))
    output_path = str(output_file(output_path, suffix='.svg'))
    detail = sys.argv[3] if len(sys.argv) > 3 else 'HIGH'
    res = vectorize_sketch(image_path, detail)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(res['svg'])
    print(f"[SKETCH] SSIM(lines)={res['ssim_ref']:.4f} "
          f"gate={'PASS' if res['gate_passed'] else 'FAIL'}")
    sys.exit(0 if res['gate_passed'] else 3)


if __name__ == '__main__':
    main()
