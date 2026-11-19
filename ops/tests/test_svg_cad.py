# -*- coding: utf-8 -*-
"""Regression tests for the svg_cad blockers (T0-T3).

Run from the project root:  python ops/tests/test_svg_cad.py
Exits non-zero on the first failed assertion.

Covers:
  T0  gates are sourced from the SSOT (no drift)
  T1  artifact metadata builds, schema-validates, and round-trips through the SVG
  T2  topological axis (closed vs open) and 3mf_eligible derivation
  T3  filtration invariant — a smooth gradient (which would chain every cluster
      into the background) must NOT yield an empty foreground (R-03 regression)
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, 'ops', 'scripts'))

import pipeline_config as pc          # noqa: E402
import svg_contract as C              # noqa: E402
import hifi_vectorize as H            # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}")
        sys.exit(1)


# ---- T0: SSOT is the single source of gate values ------------------------
check("T0 sketch gate from SSOT == 0.75", pc.gate('SKETCH_VECTOR') == 0.75)
check("T0 standard gate from SSOT == 0.93", pc.gate('STANDARD_VECTOR') == 0.93)
check("T0 active colour model == monochrome", pc.active_color_model() == 'monochrome')

# ---- T2: topology axis + 3mf_eligible ------------------------------------
CLOSED = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 20" width="10" '
          'height="20">\n  <g fill="#000000">\n    <path d="M0 0 C2 0 4 0 6 0 '
          'C3 3 3 3 0 4 C0 2 0 1 0 0 Z " /></g></svg>')
topo_closed = C.topology_axis(CLOSED)
check("T2 closed paths pass topology", topo_closed['passed'])
check("T2 open path fails topology",
      not C.topology_axis(CLOSED.replace(' Z ', ' '))['passed'])
check("T2 raster image fails topology",
      not C.topology_axis(CLOSED + '<image href="x"/>')['passed'])

std = C.build_metadata('COMP-001', '3.5', 'deterministic_vtracer', 'STANDARD_VECTOR',
                       None, 's.png', 'svg_s.svg', 0.95, 0.93, [], topo_closed, '0 0 10 20')
check("T2 STANDARD+closed+mono -> 3mf_eligible True", std['3mf_eligible'] is True)
sk = C.build_metadata('COMP-002', '3.5', 'deterministic_vtracer', 'SKETCH_VECTOR',
                      None, 's.png', 'svg_s.svg', 0.99, 0.75, [], topo_closed, '0 0 10 20')
check("T2 SKETCH never 3mf_eligible (even SSIM .99)", sk['3mf_eligible'] is False)
col = C.build_metadata('COMP-003', '3.5', 'deterministic_vtracer', 'STANDARD_VECTOR',
                       None, 's.png', 'svg_s.svg', 0.95, 0.93, ['RULE2: non-black'],
                       topo_closed, '0 0 10 20')
check("T2 colour violation -> 3mf_eligible False", col['3mf_eligible'] is False)

# ---- T1: schema conformance + embed round-trip ---------------------------
check("T1 valid metadata passes schema", C.validate_artifact(std) == [])
bad = dict(std); del bad['viewbox']
check("T1 metadata missing field fails schema", bool(C.validate_artifact(bad)))
embedded = C.embed_metadata(CLOSED, std)
check("T1 metadata embeds into <svg>", '<metadata id="svgcad"' in embedded)
check("T1 embed/extract round-trips", C.extract_metadata(embedded) == std)

# ---- T3: filtration invariant (R-03 regression) --------------------------
_test_temp = tempfile.TemporaryDirectory()
grad = np.tile(np.linspace(0, 255, 256).astype(np.uint8)[:, None], (1, 256))
gp = os.path.join(_test_temp.name, 'gradient.png')
Image.fromarray(np.stack([grad] * 3, -1)).save(gp)
r = H.vectorize_monochrome(gp, 'LOW', log=lambda *a: None)
# R-03: must not silently emit an empty foreground. Either >=1 path retained,
# or explicitly flagged for quarantine (gate_passed False + invariant_violation).
non_empty = r.get('n_paths', 0) >= 1 and not r.get('invariant_violation')
flagged = r.get('n_paths', 0) == 0 and r.get('gate_passed') is False
check("T3 gradient keeps >=1 path OR is flagged (never silent-empty)",
      non_empty or flagged)
check("T3 gradient was rescued to non-empty foreground", r.get('n_paths', 0) >= 1)

# ---- suitability gate: blurry/featureless source rejected (cat_3 class) ------
import compile_svg as CS  # noqa: E402
import numpy as _np  # noqa: E402
from PIL import Image as _Image  # noqa: E402
_blur = os.path.join(_test_temp.name, 'blur.png')
_Image.fromarray((_np.ones((200, 200, 3)) * 180).astype('uint8')).save(_blur)  # featureless
_low, _ = CS.source_too_low_detail(_blur)
check("suitability: featureless source rejected", _low is True)
_sharp = os.path.join(_test_temp.name, 'sharp.png')
_a = _np.full((200, 200, 3), 255, 'uint8'); _a[60:140, 60:140] = 0       # crisp square
_Image.fromarray(_a).save(_sharp)
_lo2, _ = CS.source_too_low_detail(_sharp)
check("suitability: crisp graphic NOT rejected", _lo2 is False)

print(f"\nAll {PASS} svg_cad assertions passed.")
_test_temp.cleanup()
