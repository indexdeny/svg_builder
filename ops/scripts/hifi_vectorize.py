# -*- coding: utf-8 -*-
"""
A06: HiFi Deterministic Vectorizer (TZ v3.0).

PNG -> alpha flatten -> K-means color quantization (Lab) -> per-cluster binary
masks -> VTracer (binary mode) per mask -> composite SVG -> resvg render ->
SSIM vs reference -> VTracer parameter auto-tuning -> best candidate.

Usage:
    python ops/scripts/hifi_vectorize.py <image_path> <output_svg_path> [--report <json_path>]
Importable API:
    vectorize(image_path, design_tokens) -> dict(svg, ssim_ref, ssim_orig, params, layers)
"""
import io
import json
import os
import re
import sys
import tempfile

import cv2
import numpy as np
import resvg_py
import vtracer
from PIL import Image
from skimage.metrics import structural_similarity as ssim

# Quantization / palette policy
MAX_K = 6
DELTA_E_TARGET = 6.0          # mean Lab quantization error threshold for auto-K
TOKEN_SNAP_DIST = 24.0        # max RGB euclidean distance to snap cluster -> base token
UPSCALE_BELOW = 600           # upscale x2 when max(w, h) below this
DOWNSCALE_ABOVE_MP = 2.2      # downscale tracing for images above this many megapixels
SSIM_EARLY_STOP = 0.985
# Release gate for the STANDARD_VECTOR / colour paths, sourced from the SSOT
# (00_governance/pipeline_config.json) so there is one source of truth (svg_cad
# T0). The literal is only a fallback if the config file is absent.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import gate as _gate
SSIM_RELEASE_GATE = _gate('STANDARD_VECTOR', 'ssim_min', 0.93)

# VTracer parameter grid for the perceptual tuning loop, ordered by expected quality
PARAM_GRID = [
    dict(filter_speckle=4, corner_threshold=60, length_threshold=4.0),
    dict(filter_speckle=2, corner_threshold=30, length_threshold=2.0),
    dict(filter_speckle=4, corner_threshold=30, length_threshold=3.5),
    dict(filter_speckle=8, corner_threshold=60, length_threshold=4.0),
    dict(filter_speckle=2, corner_threshold=90, length_threshold=2.0),
    dict(filter_speckle=4, corner_threshold=90, length_threshold=6.0),
]


def load_image_flattened(image_path):
    """Loads the raster, flattens alpha over white, returns RGB uint8 array."""
    img = Image.open(image_path)
    if img.mode in ('RGBA', 'LA', 'PA'):
        bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
        bg.alpha_composite(img.convert('RGBA'))
        img = bg.convert('RGB')
    else:
        img = img.convert('RGB')
    return np.array(img)


def quantize_colors(rgb):
    """K-means in Lab space with auto-K. Returns (labels HxW, centers_rgb Kx3)."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    pixels = lab.reshape(-1, 3)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)

    best = None
    for k in range(2, MAX_K + 1):
        compactness, labels, centers = cv2.kmeans(
            pixels, k, None, criteria, 5, cv2.KMEANS_PP_CENTERS)
        mean_err = np.sqrt(compactness / len(pixels))
        best = (labels, centers, k, mean_err)
        if mean_err < DELTA_E_TARGET:
            break

    labels, centers, k, mean_err = best
    centers_rgb = cv2.cvtColor(
        centers.reshape(1, -1, 3).astype(np.uint8), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    return labels.reshape(rgb.shape[:2]), centers_rgb, k, float(mean_err)


def pick_background_cluster(labels):
    """Background = the largest-area cluster that touches the image border.

    Sampling a thick border band (not a 1px edge) and ranking border-touching
    clusters by total image area makes this robust to thin decorative frames:
    a slim light frame touches the border but has tiny area, so a large panel
    behind it still wins (fixes light-on-dark emblems being inverted)."""
    h, w = labels.shape
    b = max(1, int(min(h, w) * 0.03))
    border = np.concatenate([
        labels[:b, :].ravel(), labels[-b:, :].ravel(),
        labels[:, :b].ravel(), labels[:, -b:].ravel()])
    border_clusters = np.unique(border)
    areas = np.bincount(labels.ravel(), minlength=int(labels.max()) + 1)
    return int(max(border_clusters, key=lambda c: areas[c]))


def rgb_to_hex(rgb):
    return '#{:02X}{:02X}{:02X}'.format(*[int(c) for c in rgb])


def map_cluster_colors(centers_rgb, design_tokens):
    """Maps each cluster to a base design token or an auxiliary token.

    Returns list of dicts: {token, hex} where token is a CSS var name and hex
    is the value rendered for it. Pure black/white always snap to base tokens
    (Zero Black Policy)."""
    base = {t: tuple(int(h.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4))
            for t, h in design_tokens.get('colors', {}).items()}
    out = []
    aux_n = 1
    for c in centers_rgb:
        c = tuple(int(x) for x in c)
        dists = {t: sum((a - b) ** 2 for a, b in zip(c, rgb)) ** 0.5
                 for t, rgb in base.items()}
        token, dist = min(dists.items(), key=lambda kv: kv[1])
        luma = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
        if dist <= TOKEN_SNAP_DIST or luma <= 16 or luma >= 250:
            if luma <= 16:
                token = '--dark'
            elif luma >= 250:
                token = '--white'
            out.append({'token': token, 'hex': design_tokens['colors'][token]})
        else:
            out.append({'token': f'--aux-{aux_n}', 'hex': rgb_to_hex(c)})
            aux_n += 1
    return out


MIN_TRACE_MP = 0.4            # floor for reactive downscaling on VTracer overflow


class TraceOverflowError(Exception):
    """VTracer's Rust clustering overflowed on a too-dense mask.

    Driven by image resolution/density, NOT by filter_speckle (the overflow
    happens during initial clustering, before speckle filtering). The caller
    recovers by re-tracing the whole image at a lower resolution."""


def trace_mask(mask, params):
    """Traces a binary mask (True = shape) with VTracer.

    Returns list of (d_string, transform_or_None). VTracer emits a per-path
    transform="translate(x,y)" which must be preserved.
    Raises TraceOverflowError if VTracer panics (mask too dense to cluster)."""
    h, w = mask.shape
    img = np.full((h, w), 255, np.uint8)
    img[mask] = 0
    with tempfile.TemporaryDirectory() as td:
        png_path = os.path.join(td, 'mask.png')
        svg_path = os.path.join(td, 'mask.svg')
        Image.fromarray(img).save(png_path)
        try:
            vtracer.convert_image_to_svg_py(
                image_path=png_path,
                out_path=svg_path,
                colormode='binary',
                mode='spline',
                filter_speckle=int(params['filter_speckle']),
                corner_threshold=int(params['corner_threshold']),
                length_threshold=float(params['length_threshold']),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:  # incl. pyo3_runtime.PanicException
            raise TraceOverflowError(str(e))
        with open(svg_path, 'r', encoding='utf-8') as f:
            svg_text = f.read()
    paths = []
    for m in re.finditer(r'<path\b[^>]*>', svg_text):
        tag = m.group(0)
        d = re.search(r'\sd="([^"]+)"', tag)
        tr = re.search(r'\stransform="([^"]+)"', tag)
        if d:
            paths.append((d.group(1), tr.group(1) if tr else None))
    return paths


def trace_all_layers(labels, fg_order, colors, params, view_w, view_h, log):
    """Traces every foreground mask, reactively downscaling the whole label map
    if VTracer overflows. Returns (fg_layers, trace_w, trace_h, labels)."""
    cur = labels
    cur_h, cur_w = cur.shape
    while True:
        try:
            fg_layers = [(colors[ci], trace_mask(cur == ci, params))
                         for ci in fg_order]
            return fg_layers, cur_w, cur_h, cur
        except TraceOverflowError:
            nw, nh = round(cur_w * 0.7), round(cur_h * 0.7)
            if nw * nh / 1e6 < MIN_TRACE_MP:
                log(f"    [HIFI] WARNING: image too dense; vectorized at floor "
                    f"{cur_w}x{cur_h} with some detail dropped")
                # final best-effort pass, skipping any layers that still overflow
                fg_layers = []
                for ci in fg_order:
                    try:
                        fg_layers.append((colors[ci], trace_mask(cur == ci, params)))
                    except TraceOverflowError:
                        fg_layers.append((colors[ci], []))
                return fg_layers, cur_w, cur_h, cur
            cur = cv2.resize(cur.astype(np.int32), (nw, nh),
                             interpolation=cv2.INTER_NEAREST)
            cur_w, cur_h = nw, nh
            log(f"    [HIFI] VTracer overflow; re-tracing at {cur_w}x{cur_h} "
                f"({cur_w * cur_h / 1e6:.2f} MP)")


def build_svg(view_w, view_h, trace_w, trace_h, bg_color, fg_layers, design_tokens):
    """Assembles the composite SVG. Paths are in trace coordinates; a group
    transform maps them back into the viewBox when trace size differs."""
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_w} {view_h}" '
        f'width="{view_w}" height="{view_h}">'
    ]
    style = ['  <style>', '  :root {']
    for section in ('colors', 'typography', 'strokes'):
        for k, v in design_tokens.get(section, {}).items():
            style.append(f'    {k}: {v};')
    aux_seen = set()
    for color, _ in fg_layers:
        if color['token'].startswith('--aux-') and color['token'] not in aux_seen:
            style.append(f"    {color['token']}: {color['hex']};")
            aux_seen.add(color['token'])
    if bg_color['token'].startswith('--aux-') and bg_color['token'] not in aux_seen:
        style.append(f"    {bg_color['token']}: {bg_color['hex']};")
    style += ['  }', '  </style>']
    lines += style
    lines.append(f'  <rect width="{view_w}" height="{view_h}" '
                 f'fill="var({bg_color["token"]})" />')
    sx, sy = view_w / trace_w, view_h / trace_h
    transform = (f' transform="scale({sx:.8f}, {sy:.8f})"'
                 if abs(sx - 1) > 1e-9 or abs(sy - 1) > 1e-9 else '')
    for color, paths in fg_layers:
        lines.append(f'  <g fill="var({color["token"]})"{transform}>')
        for d, tr in paths:
            tr_attr = f' transform="{tr}"' if tr else ''
            lines.append(f'    <path d="{d}"{tr_attr} />')
        lines.append('  </g>')
    lines.append('</svg>')
    return '\n'.join(lines)


def build_black_svg(view_w, view_h, trace_w, trace_h, paths):
    """Monochrome release SVG (TZ v3.1): pure-black fill, transparent background,
    no <style>, no background rect. `paths` is a list of (d, transform_or_None).
    A single group transform maps trace coords back into the viewBox."""
    sx, sy = view_w / trace_w, view_h / trace_h
    transform = (f' transform="scale({sx:.8f}, {sy:.8f})"'
                 if abs(sx - 1) > 1e-9 or abs(sy - 1) > 1e-9 else '')
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'viewBox="0 0 {view_w} {view_h}" '
             f'width="{view_w}" height="{view_h}">',
             f'  <g fill="#000000"{transform}>']
    for d, tr in paths:
        tr_attr = f' transform="{tr}"' if tr else ''
        lines.append(f'    <path d="{d}"{tr_attr} />')
    lines += ['  </g>', '</svg>']
    return '\n'.join(lines)


def substitute_css_vars(svg_text):
    """Inlines var(--x) with hex values from the <style> block (resvg lacks var())."""
    tokens = dict(re.findall(r'(--[a-zA-Z0-9-]+):\s*([^;]+);', svg_text))
    return re.sub(r'var\((--[a-zA-Z0-9-]+)\)',
                  lambda m: tokens.get(m.group(1), '#000000').strip(),
                  svg_text)


def render_svg(svg_text, width, height):
    """Rasterizes SVG to an RGB numpy array on white background."""
    png = resvg_py.svg_to_bytes(svg_string=substitute_css_vars(svg_text),
                                width=int(width), height=int(height))
    img = Image.open(io.BytesIO(bytes(png))).convert('RGBA')
    bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
    bg.alpha_composite(img)
    return np.array(bg.convert('RGB'))


def score_ssim(a, b):
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    return float(ssim(a, b, channel_axis=2))


def vectorize(image_path, design_tokens, log=print):
    """Full HiFi pipeline for one raster. Returns result dict."""
    rgb_orig = load_image_flattened(image_path)
    view_h, view_w = rgb_orig.shape[:2]

    mp = view_w * view_h / 1e6
    if max(view_w, view_h) < UPSCALE_BELOW:
        factor = 2.0
    elif mp > DOWNSCALE_ABOVE_MP:
        factor = (DOWNSCALE_ABOVE_MP / mp) ** 0.5
        log(f"  [HIFI] {mp:.1f} MP source: tracing downscaled x{factor:.2f} "
            f"(viewBox stays {view_w}x{view_h})")
    else:
        factor = 1.0
    trace_w, trace_h = round(view_w * factor), round(view_h * factor)
    rgb = (cv2.resize(rgb_orig, (trace_w, trace_h),
                      interpolation=cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA)
           if factor != 1.0 else rgb_orig)

    labels, centers_rgb, k, qerr = quantize_colors(rgb)
    bg_idx = pick_background_cluster(labels)
    colors = map_cluster_colors(centers_rgb, design_tokens)
    log(f"  [HIFI] K={k} clusters (quant err {qerr:.2f}), background cluster {bg_idx} "
        f"-> {colors[bg_idx]['token']}")

    # Reference raster: quantized image painted with final output colors
    palette = np.array(
        [tuple(int(c['hex'].lstrip('#')[i:i + 2], 16) for i in (0, 2, 4))
         for c in colors], np.uint8)
    reference = palette[labels]
    if factor != 1.0:
        reference = cv2.resize(reference, (view_w, view_h),
                               interpolation=cv2.INTER_AREA)

    # Foreground clusters, big areas first (painted bottom-up)
    areas = np.bincount(labels.ravel(), minlength=k)
    fg_order = sorted([i for i in range(k) if i != bg_idx],
                      key=lambda i: -int(areas[i]))

    best = None
    cur_labels = labels
    for params in PARAM_GRID:
        fg_layers, trace_w, trace_h, cur_labels = trace_all_layers(
            cur_labels, fg_order, colors, params, view_w, view_h, log)
        svg_text = build_svg(view_w, view_h, trace_w, trace_h, colors[bg_idx],
                             fg_layers, design_tokens)
        rendered = render_svg(svg_text, view_w, view_h)
        s_ref = score_ssim(reference, rendered)
        log(f"  [HIFI] params {params} -> SSIM(ref) {s_ref:.4f}")
        if best is None or s_ref > best['ssim_ref']:
            best = dict(svg=svg_text, ssim_ref=s_ref, params=params,
                        rendered=rendered,
                        layers=[(colors[ci]['token'], len(fl[1]))
                                for ci, fl in zip(fg_order, fg_layers)])
        if s_ref >= SSIM_EARLY_STOP:
            break

    best['ssim_orig'] = score_ssim(rgb_orig, best['rendered'])
    best['k'] = k
    best['gate_passed'] = best['ssim_ref'] >= SSIM_RELEASE_GATE
    del best['rendered']
    return best


# detail level -> ordered VTracer param sets for monochrome tracing (finest first)
MONO_DETAIL_GRID = {
    'ULTRA':  [dict(filter_speckle=2, corner_threshold=30, length_threshold=2.0),
               dict(filter_speckle=2, corner_threshold=60, length_threshold=2.0),
               dict(filter_speckle=4, corner_threshold=30, length_threshold=3.5)],
    'HIGH':   [dict(filter_speckle=3, corner_threshold=40, length_threshold=3.0),
               dict(filter_speckle=2, corner_threshold=30, length_threshold=2.0),
               dict(filter_speckle=4, corner_threshold=60, length_threshold=4.0)],
    'MEDIUM': [dict(filter_speckle=6, corner_threshold=60, length_threshold=4.0),
               dict(filter_speckle=4, corner_threshold=60, length_threshold=4.0)],
    'LOW':    [dict(filter_speckle=10, corner_threshold=80, length_threshold=6.0)],
}


def vectorize_monochrome(image_path, detail_level='ULTRA', log=print):
    """STANDARD_VECTOR monochrome path (TZ v3.1).

    Segments foreground vs background (background = border-dominant cluster),
    traces the foreground union mask as pure-black fill with even-odd holes
    (interior background regions stay transparent). No background rect, no
    colour: pure #000000 on a transparent canvas."""
    rgb_orig = load_image_flattened(image_path)
    view_h, view_w = rgb_orig.shape[:2]

    mp = view_w * view_h / 1e6
    if max(view_w, view_h) < UPSCALE_BELOW:
        factor = 2.0
    elif mp > DOWNSCALE_ABOVE_MP:
        factor = (DOWNSCALE_ABOVE_MP / mp) ** 0.5
    else:
        factor = 1.0
    trace_w, trace_h = round(view_w * factor), round(view_h * factor)
    rgb = (cv2.resize(rgb_orig, (trace_w, trace_h),
                      interpolation=cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA)
           if factor != 1.0 else rgb_orig)

    labels, centers_rgb, k, qerr = quantize_colors(rgb)
    bg_idx = pick_background_cluster(labels)
    # Background = clusters reachable from the border cluster by single-linkage
    # colour steps (<=40 RGB). A vignette/gradient canvas is a CHAIN of near-
    # identical shades, so the whole gradient collapses into one transparent
    # background, while isolated ink (a big colour jump away) stays foreground.
    cen = centers_rgb.astype(np.int32)
    bg_set = {bg_idx}
    changed = True
    while changed:
        changed = False
        for i in range(k):
            if i in bg_set:
                continue
            if any(np.linalg.norm(cen[i] - cen[j]) <= 40 for j in bg_set):
                bg_set.add(i)
                changed = True
    # T3 invariant (R-03): the single-linkage merge must never swallow EVERY
    # cluster. A smooth grayscale chains all shades into the background, leaving
    # an empty foreground -> a blank SVG that scores SSIM~1.0 against a blank
    # reference and would silently 'pass'. Keep the cluster most distant from the
    # background seed as foreground so the layer always retains >=1 path.
    if len(bg_set) >= k:
        seed = cen[bg_idx]
        farthest = max(range(k), key=lambda i: float(np.linalg.norm(cen[i] - seed)))
        bg_set.discard(farthest)
        log(f"  [MONO] T3: all {k} clusters merged into background; retaining "
            f"cluster {farthest} as foreground (>=1-path invariant)")

    # Ink-vs-fill: for a dark-on-light design (light background), the meaningful
    # marks are the DARK clusters; a light interior FILL colour (e.g. the yellow
    # disk behind a black silhouette) is paper, not ink. Filling the whole
    # foreground there collapses the design to a solid blob (logo_pee -> black
    # circle). So when the background is light AND real dark ink coexists with a
    # lighter fill, keep only the dark clusters as black and let the fill show
    # through transparent. White-on-dark and solid single-colour shapes are
    # unaffected (they fall through to the plain foreground = everything non-bg).
    def _luma(c):
        return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    INK_MAX = 160
    nonbg = [i for i in range(k) if i not in bg_set]
    dark = [i for i in nonbg if _luma(cen[i]) < INK_MAX]
    if _luma(cen[bg_idx]) > 160 and dark and len(dark) < len(nonbg):
        fg_mask = np.isin(labels, dark)
        light = sorted(set(nonbg) - set(dark))
        log(f"  [MONO] ink-on-light: ink clusters {sorted(dark)} -> black, "
            f"light fill clusters {light} -> transparent")
    else:
        fg_mask = ~np.isin(labels, list(bg_set))
    fg_frac = float(fg_mask.mean())
    log(f"  [MONO] {detail_level}: K={k}, bg clusters {sorted(bg_set)} "
        f"(fg clusters retained {k - len(bg_set)}), foreground {fg_frac*100:.1f}%")

    reference = np.where(fg_mask[..., None], 0, 255).astype(np.uint8)
    reference = np.repeat(reference, 3, axis=2)
    if factor != 1.0:
        reference = cv2.resize(reference, (view_w, view_h),
                               interpolation=cv2.INTER_AREA)

    best = None
    cur = fg_mask.astype(np.int32)
    cw, ch = trace_w, trace_h
    for params in MONO_DETAIL_GRID.get(detail_level, MONO_DETAIL_GRID['HIGH']):
        # crash-safe trace with reactive downscale
        while True:
            try:
                paths = trace_mask(cur == 1, params)
                break
            except TraceOverflowError:
                nw, nh = round(cw * 0.7), round(ch * 0.7)
                if nw * nh / 1e6 < MIN_TRACE_MP:
                    paths = []
                    log("  [MONO] WARNING: foreground too dense, partial output")
                    break
                cur = cv2.resize(cur, (nw, nh), interpolation=cv2.INTER_NEAREST)
                cw, ch = nw, nh
                log(f"  [MONO] overflow; re-tracing at {cw}x{ch}")
        svg_text = build_black_svg(view_w, view_h, cw, ch, paths)
        rendered = render_svg(svg_text, view_w, view_h)
        s_ref = score_ssim(reference, rendered)
        log(f"  [MONO] params {params} -> SSIM(fg) {s_ref:.4f}, paths={len(paths)}")
        if best is None or s_ref > best['ssim_ref']:
            best = dict(svg=svg_text, ssim_ref=s_ref, params=params,
                        n_paths=len(paths))
        if s_ref >= SSIM_EARLY_STOP:
            break

    best['ssim_orig'] = best['ssim_ref']
    best['k'] = k
    best['mode'] = 'STANDARD_VECTOR'
    best['gate_passed'] = best['ssim_ref'] >= SSIM_RELEASE_GATE
    # T3 (R-03): an empty foreground (no traced paths) is never a release, even
    # though a blank-vs-blank SSIM is high. Flag it so the compiler quarantines
    # with diagnostics instead of emitting a silent empty SVG.
    best['n_paths'] = best.get('n_paths', 0)
    if best['n_paths'] == 0 or fg_frac <= 0.0:
        best['gate_passed'] = False
        best['invariant_violation'] = (
            f"empty foreground after filtering (fg_frac={fg_frac:.4f}, "
            f"paths={best['n_paths']})")
    return best


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    image_path, output_path = sys.argv[1], sys.argv[2]
    from reference_whitelist import verify_path, output_file
    image_path = str(verify_path(image_path))
    output_path = str(output_file(output_path, suffix='.svg'))
    report_path = None
    if '--report' in sys.argv:
        report_path = str(output_file(sys.argv[sys.argv.index('--report') + 1], suffix='.json'))

    tokens_path = os.path.join(os.path.dirname(__file__), '..', '..',
                               '00_governance', 'design-tokens.json')
    with open(tokens_path, 'r', encoding='utf-8') as f:
        design_tokens = json.load(f)

    result = vectorize(image_path, design_tokens)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result['svg'])

    print(f"[HIFI] SSIM(reference)={result['ssim_ref']:.4f} "
          f"SSIM(original)={result['ssim_orig']:.4f} "
          f"gate={'PASS' if result['gate_passed'] else 'FAIL'}")
    if report_path:
        rep = {kk: vv for kk, vv in result.items() if kk != 'svg'}
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    sys.exit(0 if result['gate_passed'] else 3)


if __name__ == '__main__':
    main()
