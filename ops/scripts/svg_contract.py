# -*- coding: utf-8 -*-
"""SVG output contract (svg_cad T1 keystone + T2 axes).

The canonical artifact of compile_svg.py is a STANDALONE monochrome <svg>
(xmlns, explicit width/height, single viewBox) that carries its own metadata in
a <metadata id="svgcad"> JSON block and is mirrored into the knowledge graph.

This module provides the contract pieces:
  - topology_axis(svg)  : T2 topological quality axis (closed paths, no raster/gradient)
  - build_metadata(...)  : assemble the artifact metadata object
  - embed_metadata(svg, meta) : inline <metadata> into the standalone SVG
  - extract_metadata(svg) : read it back
  - validate_artifact(meta) : JSON-schema conformance (svg_artifact_v1)

Quality axes are kept ORTHOGONAL (audit principle 2): visual (SSIM), colour
(monochrome conformance) and topological (closedness) are independent; the
visual axis governs 2D release, while 3mf_eligible is derived from the colour +
topological axes and the render_mode (SKETCH_VECTOR is never 3mf_eligible).
"""
import json
import os
import re

import jsonschema

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), '..', '..',
                            '00_governance', 'schemas', 'svg_artifact_v1.schema.json')
_SCHEMA = None


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        with open(_SCHEMA_PATH, 'r', encoding='utf-8') as f:
            _SCHEMA = json.load(f)
    return _SCHEMA


def topology_axis(svg_content):
    """T2 topological axis: a minimal 3mf-eligibility FLAG, not a full geometry
    validator. Every <path> must have all subpaths closed (one Z/z per M/m), and
    the document must contain no raster image or gradient. Returns
    {'axis','passed','violations'}."""
    violations = []
    for token, label in [('data:image', 'embedded raster (base64)'),
                         ('<image', 'raster <image> element'),
                         ('linearGradient', 'linear gradient'),
                         ('radialGradient', 'radial gradient'),
                         ('<filter', 'filter')]:
        if token in svg_content:
            violations.append(f'contains {label}')

    paths = re.findall(r'<path\b[^>]*\sd="([^"]+)"', svg_content)
    if not paths:
        violations.append('no <path> elements (empty geometry)')
    for i, d in enumerate(paths):
        n_sub = len(re.findall(r'[Mm]', d))
        n_close = len(re.findall(r'[Zz]', d))
        if n_sub == 0 or n_close < n_sub:
            violations.append(f'path {i}: open subpath(s) ({n_close} close / {n_sub} start)')
            break   # one example is enough for the flag
    return {'axis': 'closed_paths_no_raster', 'passed': not violations,
            'violations': violations}


def build_metadata(comp_id, spec_version, engine, render_mode, fallback_from,
                   source_filename, output_filename, ssim, ssim_threshold,
                   color_violations, topo, viewbox, px_to_mm=None,
                   source_relative_path=None, source_sha256=None):
    """Assemble the artifact metadata object (svg_artifact_v1).

    3mf_eligible = colour-conformant AND topologically closed AND not a sketch
    (audit principle 3: SKETCH_VECTOR -> 3mf_eligible = False regardless of SSIM)."""
    color_ok = not color_violations
    eligible = bool(color_ok and topo['passed'] and render_mode == 'STANDARD_VECTOR')
    metadata = {
        'schema': 'svg_artifact_v1',
        'comp_id': comp_id,
        'spec_version': str(spec_version),
        'engine': engine,
        'render_mode': render_mode,
        'fallback_from': fallback_from,
        'source_filename': source_filename,
        'output_filename': output_filename,
        'gate_results': {
            'visual': {'axis': 'ssim', 'value': round(float(ssim), 4),
                       'threshold': float(ssim_threshold),
                       'passed': bool(ssim >= ssim_threshold)},
            'color': {'axis': 'monochrome', 'passed': bool(color_ok),
                      'violations': list(color_violations)},
            'topological': topo,
        },
        '3mf_eligible': eligible,
        'viewbox': viewbox,
        'units': 'px',
        'px_to_mm': px_to_mm,
    }
    if source_relative_path is not None:
        metadata['source_relative_path'] = source_relative_path
        metadata['source_sha256'] = source_sha256
    return metadata


def validate_artifact(meta):
    """JSON-schema conformance check. Returns a list of violation strings."""
    v = jsonschema.Draft7Validator(_schema())
    return [f"{'/'.join(map(str, e.path))}: {e.message}"
            for e in sorted(v.iter_errors(meta), key=lambda e: list(e.path))]


def _xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def embed_metadata(svg_content, meta):
    """Inline the metadata JSON as <metadata id="svgcad"> right after the opening
    <svg ...> tag. JSON has no colour hex, so the monochrome validator is
    unaffected; XML specials are escaped defensively."""
    payload = _xml_escape(json.dumps(meta, ensure_ascii=False, separators=(',', ':')))
    block = f'\n  <metadata id="svgcad" type="application/json">{payload}</metadata>'
    m = re.search(r'<svg\b[^>]*>', svg_content)
    if not m:
        return svg_content
    i = m.end()
    return svg_content[:i] + block + svg_content[i:]


def extract_metadata(svg_content):
    """Read the embedded svgcad metadata back out, or None."""
    m = re.search(r'<metadata id="svgcad"[^>]*>(.*?)</metadata>', svg_content, re.DOTALL)
    if not m:
        return None
    raw = m.group(1).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    try:
        return json.loads(raw)
    except Exception:
        return None
