# -*- coding: utf-8 -*-
"""A09 — compile_3mf (Stage 4): 2D monochrome SVG artifact -> printable 3MF mesh.

Consumes only svg_artifact_v1 artifacts flagged 3mf_eligible (STANDARD_VECTOR,
closed paths, monochrome). Pipeline per the compile_3mf TZ:
  T1  parse SVG -> 2D shapely polygons with holes (vector-first, raster fallback)
  T2  scale px->mm, Y-flip, extrude; polarity cutout (plate minus shapes) / raised
  T3  manifold/watertight + printer-bounds guard rails
  T4  export .3mf (unit=millimeter) + mesh_artifact_v1 sidecar + graph mirror

CLI:
    python ops/scripts/compile_3mf.py [--only COMP-NNN] [--profile cfg.json] [--no-clean]
"""
import os
import sys
import io
import re
import glob
import json
import time
import zipfile
import datetime
import xml.etree.ElementTree as ET

import numpy as np
import cv2
import trimesh
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.ops import unary_union
from shapely import make_valid
from shapely.affinity import affine_transform
from svgpathtools import parse_path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline_config
import hifi_vectorize  # render_svg for the raster fallback
import reference_whitelist as whitelist
import svg_contract

OUT_DIR = "07_output_3mf"
SAMPLE_STEP = 0.5          # local-px spacing when flattening Beziers (~0.2px in viewBox)
RASTER_LONG = 1600         # long side (px) for the raster fallback


# ----------------------------------------------------------------------------- T1
def _parse_scale(transform):
    if transform:
        m = re.search(r'scale\(\s*([-\d.eE]+)\s*(?:[, ]\s*([-\d.eE]+))?\s*\)', transform)
        if m:
            sx = float(m.group(1))
            sy = float(m.group(2)) if m.group(2) else sx
            return sx, sy
    return 1.0, 1.0


def _parse_translate(transform):
    if transform:
        m = re.search(r'translate\(\s*([-\d.eE]+)\s*(?:[, ]\s*([-\d.eE]+))?\s*\)', transform)
        if m:
            tx = float(m.group(1))
            ty = float(m.group(2)) if m.group(2) else 0.0
            return tx, ty
    return 0.0, 0.0


def _flatten_subpath(sub):
    """Sample a continuous svgpathtools subpath into a list of (x, y)."""
    pts = []
    for seg in sub:
        n = int(max(2, min(120, round(seg.length() / SAMPLE_STEP))))
        for i in range(n):
            z = seg.point(i / n)
            pts.append((z.real, z.imag))
    z = sub[-1].point(1.0)
    pts.append((z.real, z.imag))
    return pts


def _only_polygons(geom):
    """Reduce any geometry to a MultiPolygon of its polygonal parts."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    polys = [g for g in getattr(geom, 'geoms', []) if isinstance(g, (Polygon,))]
    polys += [gg for g in getattr(geom, 'geoms', [])
              if isinstance(g, MultiPolygon) for gg in g.geoms]
    return MultiPolygon(polys) if polys else None


def svg_to_polygons_vector(svg_content):
    """Vector-first: <path> Beziers -> rings -> per-path even-odd fill -> union.
    Returns (vw, vh, MultiPolygon|None). None signals 'fall back to raster'."""
    root = ET.fromstring(svg_content)
    vb = [float(x) for x in re.split(r'[\s,]+', root.get('viewBox').strip())]
    vw, vh = vb[2], vb[3]

    g = next((e for e in root.iter() if e.tag.split('}')[-1] == 'g'), None)
    if g is None:
        return vw, vh, None
    sx, sy = _parse_scale(g.get('transform'))

    path_geoms = []
    for el in g.iter():
        if el.tag.split('}')[-1] != 'path':
            continue
        d = el.get('d')
        if not d:
            continue
        tx, ty = _parse_translate(el.get('transform'))
        try:
            P = parse_path(d)
        except Exception:
            return vw, vh, None
        pg = None
        for sub in P.continuous_subpaths():
            ring = [(sx * (x + tx), sy * (y + ty)) for x, y in _flatten_subpath(sub)]
            if len(ring) < 3:
                continue
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = make_valid(poly)
            pg = poly if pg is None else pg.symmetric_difference(poly)  # even-odd
        if pg is not None and not pg.is_empty:
            path_geoms.append(pg)

    if not path_geoms:
        return vw, vh, None
    geom = make_valid(unary_union(path_geoms))
    geom = _only_polygons(geom)
    if geom is None or not geom.is_valid:
        return vw, vh, None
    return vw, vh, geom


def svg_to_polygons_raster(svg_content, vw, vh):
    """Raster fallback: render -> binary mask -> contour hierarchy (ext + holes)."""
    scale = RASTER_LONG / max(vw, vh)
    rw, rh = int(round(vw * scale)), int(round(vh * scale))
    rgb = hifi_vectorize.render_svg(svg_content, rw, rh)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = (gray < 128).astype(np.uint8) * 255
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return None
    hierarchy = hierarchy[0]
    polys = []
    for i, cnt in enumerate(contours):
        if hierarchy[i][3] != -1:      # has a parent -> it is a hole, handled below
            continue
        if len(cnt) < 3:
            continue
        ext = [(p[0][0] / scale, p[0][1] / scale) for p in cnt]
        holes = []
        for j, ch in enumerate(contours):
            if hierarchy[j][3] == i and len(ch) >= 3:
                holes.append([(p[0][0] / scale, p[0][1] / scale) for p in ch])
        poly = Polygon(ext, holes)
        if not poly.is_valid:
            poly = make_valid(poly)
        if not poly.is_empty:
            polys.append(poly)
    if not polys:
        return None
    return _only_polygons(make_valid(unary_union(polys)))


def geometry_candidates(svg_content):
    """Vector-first, raster fallback (compile_3mf T1.3). Returns
    (vw, vh, [(source, geometry), ...]) — the caller tries each in order and
    keeps the first that yields a valid manifold mesh. The vector path is exact
    but can produce extrusion-hostile rings on complex logos; the raster contour
    path (simple closed curves) is the robust backstop."""
    vw, vh, gv = svg_to_polygons_vector(svg_content)
    cands = []
    if gv is not None and gv.is_valid and not gv.is_empty:
        cands.append(('vector', gv))
    gr = svg_to_polygons_raster(svg_content, vw, vh)
    if gr is not None and not gr.is_empty:
        cands.append(('raster', gr))
    return vw, vh, cands


def _clean_min_feature(geom, min_area):
    """Drop polygons and interior holes whose area < min_area (printer dust)."""
    kept = []
    for p in geom.geoms:
        if p.area < min_area:
            continue
        holes = [list(r.coords) for r in p.interiors if Polygon(r).area >= min_area]
        kept.append(Polygon(list(p.exterior.coords), holes))
    return MultiPolygon(kept) if kept else None


# ----------------------------------------------------------------------------- T2
def _poly_to_mesh(poly, height):
    """One shapely polygon (possibly with holes) -> a clean manifold solid.

    trimesh.extrude_polygon's earcut cap triangulation leaves open edges on
    polygons with MANY holes. So extrude the exterior solid and boolean-subtract
    each hole prism (manifold3d) — every piece is then a watertight volume."""
    ext = trimesh.creation.extrude_polygon(Polygon(poly.exterior.coords), height)
    if not poly.interiors:
        return ext
    eps = max(0.05, 0.01 * height)
    holes = [trimesh.creation.extrude_polygon(Polygon(r.coords), height + 2 * eps)
             for r in poly.interiors]
    hu = holes[0] if len(holes) == 1 else trimesh.boolean.union(holes)
    hu.apply_translation([0, 0, -eps])
    return trimesh.boolean.difference([ext, hu])


def _extrude_geom(geom, height):
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    meshes = [_poly_to_mesh(p, height) for p in polys if p.area > 0]
    meshes = [m for m in meshes if m is not None and len(m.faces) > 0]
    if not meshes:
        return None
    # Union into a single clean manifold (handles touching/overlapping pieces).
    return meshes[0] if len(meshes) == 1 else trimesh.boolean.union(meshes)


def build_mesh(geom, vw, vh, cfg, erode_mm=0.0):
    """Scale to mm, Y-flip, extrude per polarity. Returns (mesh, px_to_mm).

    erode_mm shrinks the black shapes before a CUTOUT — thin walls between close
    holes otherwise collapse through the export roundtrip into a non-watertight
    stencil (masson_logo_01). A sub-mm erosion thickens the walls; only used as a
    fallback when the un-eroded cutout fails the reimport gate."""
    px = cfg['target_long_side_mm'] / max(vw, vh)
    h_mm = vh * px
    mgeom = affine_transform(geom, [px, 0, 0, -px, 0, h_mm])   # mm + Y-flip
    mgeom = make_valid(mgeom)
    mgeom = _only_polygons(mgeom)
    if mgeom is None:
        return None, px
    mgeom = _clean_min_feature(mgeom, cfg['min_feature_mm'] ** 2)
    if mgeom is None:
        return None, px
    if erode_mm > 0:
        mgeom = _only_polygons(make_valid(mgeom.buffer(-erode_mm).buffer(0)))
        if mgeom is None:
            return None, px

    height = cfg['extrusion_height_mm']
    if cfg['polarity'] == 'cutout':
        # 3D boolean (manifold3d): a solid plate minus the black shapes poking
        # through both faces -> guaranteed-watertight stencil with through-holes.
        # (2D-difference + single extrude leaves open edges on complex logos.)
        eps = max(0.05, 0.01 * height)
        plate = trimesh.creation.extrude_polygon(box(0, 0, vw * px, h_mm), height)
        shapes = _extrude_geom(mgeom, height + 2 * eps)
        if shapes is None:
            mesh = plate
        else:
            shapes.apply_translation([0, 0, -eps])
            mesh = trimesh.boolean.difference([plate, shapes])
    else:  # raised
        if cfg['base_plate']:
            base = _extrude_geom(box(0, 0, vw * px, h_mm), cfg['base_plate_mm'])
            relief = _extrude_geom(mgeom, height)
            relief.apply_translation([0, 0, cfg['base_plate_mm']])
            mesh = trimesh.boolean.union([base, relief])
        else:
            mesh = _extrude_geom(mgeom, height)
    return mesh, px


# ----------------------------------------------------------------------------- T3
def validate_mesh(mesh, cfg):
    """Guard rails: watertight, winding-consistent, positive volume, printer bounds."""
    reasons = []
    if mesh is None or len(mesh.faces) == 0:
        return False, {'reasons': ['empty mesh'], 'volume_mm3': 0.0,
                       'bbox_mm': [0, 0, 0], 'watertight': False,
                       'winding_consistent': False, 'within_bounds': False}
    # Remove degenerate (zero-area) faces only. Do NOT merge_vertices: the
    # manifold3d boolean output is already a clean watertight manifold, and
    # welding nearby vertices can collapse thin features into non-manifold edges
    # (this silently broke an already-watertight masson_logo_01 cutout).
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()

    wt = bool(mesh.is_watertight)
    wc = bool(mesh.is_winding_consistent)
    vol = float(mesh.volume)
    ext = [float(x) for x in mesh.extents]
    bounds = cfg['max_printer_bounds_mm']
    within = all(ext[i] <= bounds[i] + 1e-6 for i in range(3))

    if not wt:
        reasons.append(f"not watertight (open edges={len(_open_edges(mesh))})")
    if not wc:
        reasons.append("winding inconsistent (normals not all outward)")
    if vol <= 0:
        reasons.append(f"non-positive volume ({vol:.3f})")
    if not within:
        reasons.append(f"exceeds printer bounds {ext} > {bounds}")
    ok = wt and wc and vol > 0 and within
    return ok, {'reasons': reasons, 'volume_mm3': round(vol, 3),
                'bbox_mm': [round(e, 3) for e in ext], 'watertight': wt,
                'winding_consistent': wc, 'within_bounds': within}


def _open_edges(mesh):
    try:
        return mesh.edges_open
    except Exception:
        return []


# ----------------------------------------------------------------------------- T4
def set_3mf_unit(data, unit='millimeter'):
    """Ensure the 3MF model declares <model unit="millimeter">."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            content = zin.read(item.filename)
            if item.filename.replace('\\', '/').endswith('3dmodel.model'):
                text = content.decode('utf-8')
                m = re.search(r'<model\b[^>]*>', text)
                if m and 'unit=' not in m.group(0):
                    text = text.replace(m.group(0),
                                        m.group(0)[:-1] + f' unit="{unit}">', 1)
                content = text.encode('utf-8')
            zout.writestr(item, content)
    return out.getvalue()


def export_bytes(mesh, unit='millimeter'):
    return set_3mf_unit(bytes(mesh.export(file_type='3mf')), unit)


def reimport_watertight(data):
    """DoD #4: the exported .3mf itself must reimport as a watertight mesh.
    Some meshes are watertight in memory yet lose it through the 3MF roundtrip
    (e.g. a pathological cutout boolean) — those must not be released."""
    r = trimesh.load(io.BytesIO(data), file_type='3mf')
    g = r.to_geometry() if hasattr(r, 'to_geometry') else r
    return bool(g.is_watertight)


def export_3mf(mesh, path, unit='millimeter'):
    with open(path, 'wb') as f:
        f.write(export_bytes(mesh, unit))


def build_metadata(comp_id, source_svg, px, cfg, diag):
    return {
        'schema': 'mesh_artifact_v1',
        'comp_id': comp_id,
        'source_svg_filename': source_svg,
        'spec_version': str(pipeline_config.spec_version()),
        'px_to_mm': round(px, 6),
        'extrusion_height_mm': float(cfg['extrusion_height_mm']),
        'polarity': cfg['polarity'],
        'base_plate': bool(cfg['base_plate']) and cfg['polarity'] == 'raised',
        'volume_mm3': diag['volume_mm3'],
        'bbox_mm': diag['bbox_mm'],
        'watertight': diag['watertight'],
        'winding_consistent': diag['winding_consistent'],
        'max_printer_bounds_mm': [float(x) for x in cfg['max_printer_bounds_mm']],
        'within_bounds': diag['within_bounds'],
    }


def validate_metadata(meta):
    import jsonschema
    with open('00_governance/schemas/mesh_artifact_v1.schema.json', encoding='utf-8') as f:
        schema = json.load(f)
    v = jsonschema.Draft7Validator(schema)
    return [f"{'/'.join(map(str, e.path))}: {e.message}"
            for e in sorted(v.iter_errors(meta), key=lambda e: list(e.path))]


def mirror_to_graph(comp_id, meta):
    path = f"03_knowledge_graph/{comp_id}.json"
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        d['mesh_artifact_metadata'] = meta
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"graph mirror failed for {comp_id}: {e}") from e


def log_trace(comp_id, status, duration_ms, meta=None, error=None):
    entry = {"timestamp": datetime.datetime.utcnow().isoformat() + "Z",
             "agent_id": "A09", "skill_id": "svg-to-3mf",
             "input_artifact": meta.get('source_svg_filename') if meta else "",
             "output_artifact": (f"{OUT_DIR}/{os.path.splitext(meta['source_svg_filename'])[0]}_{meta['polarity']}.3mf"
                                 if status == "Success" and meta else ""),
             "status": status, "duration_ms": int(duration_ms),
             "volume_mm3": meta.get('volume_mm3') if meta else None, "error": error}
    with open('ops/logs/agent_trace.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


# ----------------------------------------------------------------------------- T5
def find_svg(svg_name):
    direct = os.path.join('05_output_svg', svg_name)
    if os.path.exists(direct):
        return direct
    # Tolerate minor underscore drift from manual renames (svg_x -> svg_x).
    norm = re.sub(r'_+', '_', svg_name)
    for p in glob.glob('05_output_svg/svg_*.svg'):
        if re.sub(r'_+', '_', os.path.basename(p)) == norm:
            return p
    return None


def _attempt(cands, vw, vh, cfg, polarity, start):
    """Build + validate + reimport-check one polarity. cutout retries with shape
    erosion (thin-wall fix). Returns (verified_bytes, px, src, diag, None) on
    success, else (None, None, None, None, reasons)."""
    erodes = [0.0, 0.3, 0.6] if polarity == 'cutout' else [0.0, 0.05, 0.1]
    reasons = ["no geometry source produced a valid manifold"]
    for cand_src, geom in cands:
        for ero in erodes:
            try:
                m, p = build_mesh(geom, vw, vh, cfg, erode_mm=ero)
                o, dg = validate_mesh(m, cfg)               # T3 guard rails (in-memory)
            except Exception as e:
                reasons = [f"{cand_src} erode={ero}: {e}"]
                continue
            if not o:
                reasons = [f"{cand_src} erode={ero}: {dg['reasons']}"]
                continue
            data = export_bytes(m, cfg.get('output_units', 'millimeter'))
            if not reimport_watertight(data):               # DoD #4 (exported artifact)
                reasons = [f"{cand_src} erode={ero}: 3MF not watertight on reimport"]
                continue
            return data, p, cand_src, dg, None
    return None, None, None, None, reasons


def process_one(comp_id, comp_data, cli_override):
    """Emit BOTH a cutout STENCIL and a raised RELIEF for the eligible artifact
    (owner requirement: 'и трафарет, и рельеф'). Each variant is validated and
    released independently; a variant that cannot be made watertight quarantines
    on its own without blocking the other."""
    whitelist.verify_record(comp_data)
    start = time.time()
    am = comp_data.get('artifact_metadata', {})
    svg_name = am.get('output_filename') or comp_data.get('output_filename')
    if not isinstance(svg_name, str) or os.path.basename(svg_name) != svg_name or '/' in svg_name or '\\' in svg_name:
        raise ValueError('Source SVG requires a plain output filename')
    stem = os.path.splitext(svg_name)[0] if svg_name else comp_id
    base_cfg = pipeline_config.three_mf_config(comp_data.get('three_mf_override'),
                                               cli_override)

    svg_path = find_svg(svg_name) if svg_name else None
    if not svg_path:
        for polarity in ('cutout', 'raised'):
            reconcile_outcome(f'{stem}_{polarity}', False)
            _quarantine(comp_id, f'{stem}_{polarity}', None, [f"source SVG not found: {svg_name}"], start)
        mirror_to_graph(comp_id, {})
        return {'cutout': 'quarantine', 'raised': 'quarantine'}
    with open(svg_path, encoding='utf-8') as f:
        svg = f.read()
    embedded = svg_contract.extract_metadata(svg)
    if (embedded != am or embedded.get('comp_id') != comp_id
            or embedded.get('source_relative_path') != comp_data['source_file']
            or embedded.get('source_sha256') != comp_data['source_sha256']
            or not embedded.get('gate_results', {}).get('visual', {}).get('passed')
            or not embedded.get('3mf_eligible')):
        raise ValueError('Source SVG does not match the released whitelist artifact')
    vw, vh, cands = geometry_candidates(svg)
    if not cands:                                           # T1 empty invariant
        for polarity in ('cutout', 'raised'):
            reconcile_outcome(f'{stem}_{polarity}', False)
            _quarantine(comp_id, f'{stem}_{polarity}', None, ["empty geometry after extraction"], start)
        mirror_to_graph(comp_id, {})
        return {'cutout': 'quarantine', 'raised': 'quarantine'}

    os.makedirs(OUT_DIR, exist_ok=True)
    graph_meta, outcome = {}, {}
    for polarity in ('cutout', 'raised'):
        cfg = dict(base_cfg)
        cfg['polarity'] = polarity
        if polarity == 'raised':
            cfg['base_plate'] = True                        # relief = design on a backing
        vstem = f"{stem}_{polarity}"
        data, px, src, diag, reasons = _attempt(cands, vw, vh, cfg, polarity, start)
        if data is None:
            reconcile_outcome(vstem, False)
            _quarantine(comp_id, vstem, None, reasons, start)
            print(f"[FAIL] {comp_id} [{polarity}] -> quarantine: {reasons}")
            outcome[polarity] = 'quarantine'
            continue
        meta = build_metadata(comp_id, svg_name, px, cfg, diag)
        meta.update(source_relative_path=comp_data['source_file'],
                    source_sha256=comp_data['source_sha256'],
                    source_svg_sha256=whitelist.sha256(svg_path))
        schema_errs = validate_metadata(meta)               # T4 contract
        if schema_errs:
            reconcile_outcome(vstem, False)
            _quarantine(comp_id, vstem, None, [f"metadata schema: {schema_errs}"], start, meta)
            outcome[polarity] = 'quarantine'
            continue
        with open(f"{OUT_DIR}/{vstem}.3mf", 'wb') as f:     # reuse the verified bytes
            f.write(data)
        _write_sidecar(f"{OUT_DIR}/{vstem}.3mf.json", meta)
        reconcile_outcome(vstem, True)
        log_trace(comp_id, "Success", (time.time() - start) * 1000, meta)
        graph_meta[polarity] = meta
        outcome[polarity] = 'release'
        print(f"[OK] {comp_id} ({src}) [{polarity}] -> {OUT_DIR}/{vstem}.3mf  "
              f"bbox_mm={diag['bbox_mm']} vol={diag['volume_mm3']:.0f}")
    mirror_to_graph(comp_id, graph_meta)
    return outcome


def reconcile_outcome(stem, released):
    """Remove only the opposite outcome for this exact artifact variant."""
    if os.path.basename(stem) != stem or '/' in stem or '\\' in stem or stem in ('.', '..'):
        raise ValueError('Invalid artifact stem')
    output = whitelist.ROOT / OUT_DIR
    paths = ([output / 'quarantine' / (stem + '.failure.json')]
             if released else [output / (stem + '.3mf'), output / (stem + '.3mf.json')])
    for path in paths:
        whitelist.reject_links(path, whitelist.ROOT)
        if not path.resolve().is_relative_to(output.resolve()):
            raise ValueError('Outcome cleanup escapes declared 3MF output directory')
    for path in paths:
        if path.is_file():
            path.unlink()


def _write_sidecar(path, meta):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def _quarantine(comp_id, stem, mesh, reasons, start, meta=None):
    qdir = f"{OUT_DIR}/quarantine"
    os.makedirs(qdir, exist_ok=True)
    if mesh is not None:
        try:
            export_3mf(mesh, f"{qdir}/{stem}.3mf")
        except Exception:
            pass
    diagnostic = dict(meta or {})
    diagnostic.update(comp_id=comp_id, status='QUARANTINED', reasons=reasons)
    graph_path = f'03_knowledge_graph/{comp_id}.json'
    if os.path.exists(graph_path):
        with open(graph_path, encoding='utf-8') as stream:
            source = json.load(stream)
        diagnostic.update(source_relative_path=source.get('source_file'),
                          source_sha256=source.get('source_sha256'),
                          source_svg_filename=source.get('output_filename'))
    _write_sidecar(f"{qdir}/{stem}.failure.json", diagnostic)
    log_trace(comp_id, "Failed", (time.time() - start) * 1000, meta, "; ".join(reasons))


def clean_outputs():
    n = whitelist.clean_generated(whitelist.ROOT / OUT_DIR,
        ['*.3mf', '*.3mf.json', 'quarantine/*.3mf', 'quarantine/*.3mf.json',
         'staging/*.3mf', 'staging/*.3mf.json', 'quarantine/*.failure.json'])
    print(f"[CLEAN] removed {n} old 3MF artifact(s)")


def main():
    whitelist.load_registry()
    os.makedirs('ops/logs', exist_ok=True)
    for path in glob.glob('03_knowledge_graph/COMP-*.json'):
        with open(path, encoding='utf-8') as stream:
            whitelist.verify_record(json.load(stream))
    argv = sys.argv[1:]
    only = argv[argv.index('--only') + 1] if '--only' in argv else None
    cli_override = None
    if '--profile' in argv:
        with open(argv[argv.index('--profile') + 1], encoding='utf-8') as f:
            cli_override = json.load(f)
    if '--no-clean' not in argv:
        clean_outputs()

    print("Starting A09: compile_3mf (Stage 4) ...")
    work = []
    for fp in sorted(glob.glob('03_knowledge_graph/COMP-*.json')):
        cid = os.path.splitext(os.path.basename(fp))[0]
        if only and cid != only:
            continue
        with open(fp, encoding='utf-8') as f:
            data = json.load(f)
        work.append((cid, data))

    released = quarantined = skipped = 0
    for cid, data in work:
        eligible = (data.get('artifact_metadata', {}).get('3mf_eligible') is True
                    and data.get('compile_status') == 'PASSED')
        if not eligible:
            print(f"[SKIP] Object {cid} not eligible for 3D extrusion")
            skipped += 1
            continue
        res = process_one(cid, data, cli_override)          # {polarity: status}
        released += sum(1 for v in res.values() if v == 'release')
        quarantined += sum(1 for v in res.values() if v == 'quarantine')

    print(f"\n--- Stage 4 Summary (cutout + raised per object) ---\n"
          f"Released variants: {released}  Quarantined: {quarantined}  "
          f"Skipped objects (not eligible): {skipped}")
    return 1 if quarantined else 0


if __name__ == '__main__':
    sys.exit(main())
