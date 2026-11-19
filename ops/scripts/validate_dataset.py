"""Audit source -> raw -> COMP -> passport -> SVG -> exported 3MF lineage."""
import argparse
import io
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

import jsonschema
import numpy as np
import trimesh

import reference_whitelist as W
import svg_contract as S
from compile_svg import validate_monochrome
from compile_3mf import validate_metadata, validate_mesh
import pipeline_config

ROOT = W.ROOT


def audit(refresh=False):
    registry = W.load_registry()
    errors = []
    schemas = json.loads((ROOT / '00_governance/schemas/passport_schema_v1.json').read_text(encoding='utf-8'))
    records = {}
    by_source = {}
    svg_checks = []
    mesh_checks = []

    def fail(message):
        errors.append(message)

    for path in sorted((ROOT / '03_knowledge_graph').glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            W.verify_record(data)
            jsonschema.validate(data, schemas)
            cid = data['passport_id']
            if path.stem != cid or cid in records:
                raise ValueError('duplicate/mismatched COMP ID')
            if data['source_file'] in by_source:
                raise ValueError('duplicate source COMP record')
            automation = data.get('automation_meta', {})
            if automation.get('entity_id') != cid or automation.get('source_ref') != data['source_file']:
                raise ValueError('automation references must identify the real COMP and raster')
            export_path = automation.get('export_path', '')
            if data.get('compile_status') == 'PASSED':
                if export_path != '05_output_svg/' + data.get('output_filename', '') or not (ROOT / export_path).is_file():
                    raise ValueError('automation export_path does not resolve to the released SVG')
            elif export_path and not (ROOT / export_path).is_file():
                raise ValueError('automation export_path points to an absent artifact')
            records[cid] = data
            by_source[data['source_file']] = cid
            passport = ROOT / '04_passports' / (cid + '.md')
            if not passport.is_file():
                raise ValueError('missing passport')
            text = passport.read_text(encoding='utf-8')
            if data['source_file'] not in text or data['source_sha256'] not in text:
                raise ValueError('passport source identity mismatch')
            if export_path and export_path not in text:
                raise ValueError('passport export path differs from graph')
        except Exception as error:
            fail(f'graph/{path.name}: {error}')

    for path in (ROOT / '04_passports').glob('*.md'):
        if path.stem not in records:
            fail(f'passport without valid record: {path.name}')

    for path in (ROOT / '02_processing').glob('*.json'):
        try:
            W.verify_record(json.loads(path.read_text(encoding='utf-8')))
        except Exception as error:
            fail(f'processing/{path.name}: {error}')

    for path in sorted((ROOT / '05_output_svg').rglob('*.svg')):
        try:
            text = path.read_text(encoding='utf-8')
            xml = ET.fromstring(text)
            meta = S.extract_metadata(text)
            if not meta:
                raise ValueError('missing contract')
            schema_errors = S.validate_artifact(meta)
            if schema_errors:
                raise ValueError(str(schema_errors))
            cid = meta['comp_id']
            data = records[cid]
            if (meta.get('source_relative_path') != data['source_file']
                    or meta.get('source_sha256') != data['source_sha256']
                    or meta != data.get('artifact_metadata')):
                raise ValueError('embedded/graph/source metadata mismatch')
            color = validate_monochrome(text)
            topo = S.topology_axis(text)
            if color or topo['passed'] != meta['gate_results']['topological']['passed']:
                raise ValueError('actual color/topology differs from contract')
            if xml.get('viewBox') != meta['viewbox']:
                raise ValueError('viewBox differs from metadata')
            _, _, width, height = map(float, xml.get('viewBox').split())
            if (width, height) != (data['resolution']['width_px'], data['resolution']['height_px']):
                raise ValueError('viewBox differs from source dimensions')
            eligible = bool(not color and topo['passed'] and meta['render_mode'] == 'STANDARD_VECTOR')
            if meta['3mf_eligible'] != eligible:
                raise ValueError('incorrect 3MF eligibility')
            released = path.parent == ROOT / '05_output_svg'
            if released:
                expected_threshold = pipeline_config.gate(meta['render_mode'])
                visual = meta['gate_results']['visual']
                if (not visual['passed'] or visual['value'] < expected_threshold
                        or visual['threshold'] != expected_threshold
                        or data.get('compile_status') != 'PASSED'
                        or meta['output_filename'] != path.name):
                    raise ValueError('released SVG does not pass current gates')
            svg_checks.append({'path': path.relative_to(ROOT).as_posix(), 'comp_id': cid,
                'released': released, 'sha256': W.sha256(path), 'ssim': meta['gate_results']['visual']['value'],
                'render_mode': meta['render_mode'], '3mf_eligible': eligible})
        except Exception as error:
            fail(f'SVG/{path.name}: {error}')

    for path in sorted((ROOT / '07_output_3mf').rglob('*.3mf')):
        try:
            sidecar = Path(str(path) + '.json')
            meta = json.loads(sidecar.read_text(encoding='utf-8'))
            issues = validate_metadata(meta)
            if issues:
                raise ValueError(str(issues))
            data = records[meta['comp_id']]
            svg_path = ROOT / '05_output_svg' / meta['source_svg_filename']
            if (meta.get('source_relative_path') != data['source_file']
                    or meta.get('source_sha256') != data['source_sha256']
                    or not svg_path.is_file()
                    or meta.get('source_svg_sha256') != W.sha256(svg_path)
                    or not data['artifact_metadata']['3mf_eligible']
                    or data.get('compile_status') != 'PASSED'):
                raise ValueError('mesh lineage differs from released SVG/source')
            blob = path.read_bytes()
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                models = [name for name in archive.namelist() if name.endswith('.model')]
                if not models or any(ET.fromstring(archive.read(name)).get('unit') != 'millimeter' for name in models):
                    raise ValueError('3MF units must explicitly be millimeter')
            scene = trimesh.load(io.BytesIO(blob), file_type='3mf')
            mesh = scene.to_geometry() if hasattr(scene, 'to_geometry') else scene
            config = pipeline_config.three_mf_config()
            config['max_printer_bounds_mm'] = meta['max_printer_bounds_mm']
            ok, diag = validate_mesh(mesh, config)
            if not ok or not np.allclose(diag['bbox_mm'], meta['bbox_mm'], atol=0.002):
                raise ValueError('reimported geometry fails bounds/manifold checks: ' + str(diag))
            if abs(diag['volume_mm3'] - meta['volume_mm3']) > max(0.1, meta['volume_mm3'] * 0.0001):
                raise ValueError('reimported volume differs from metadata')
            if meta != data.get('mesh_artifact_metadata', {}).get(meta['polarity']):
                raise ValueError('mesh sidecar differs from graph mirror')
            mesh_checks.append({'path': path.relative_to(ROOT).as_posix(), 'comp_id': meta['comp_id'],
                'sha256': W.sha256(path), 'polarity': meta['polarity'], 'units': 'millimeter', **diag})
        except Exception as error:
            fail(f'3MF/{path.name}: {error}')

    for path in (ROOT / '07_output_3mf').rglob('*.3mf.json'):
        if not Path(str(path)[:-5]).is_file():
            fail('orphan mesh sidecar: ' + path.name)
    for path in (ROOT / '07_output_3mf').rglob('*.failure.json'):
        try:
            d = json.loads(path.read_text(encoding='utf-8'))
            source = records[d['comp_id']]
            if (d['status'] != 'QUARANTINED' or not d['reasons']
                    or d['source_relative_path'] != source['source_file']
                    or d['source_sha256'] != source['source_sha256']):
                raise ValueError('unlinked failure diagnostic')
        except Exception as error:
            fail(f'failure diagnostic/{path.name}: {error}')

    # Reverse edges matter: deleting an artifact must never turn the audit green.
    for cid, data in records.items():
        stem = Path(data.get('output_filename', cid)).stem
        eligible = (data.get('compile_status') == 'PASSED'
                    and data.get('artifact_metadata', {}).get('3mf_eligible') is True)
        for polarity in ('cutout', 'raised'):
            mesh_path = ROOT / '07_output_3mf' / f'{stem}_{polarity}.3mf'
            diagnostic = ROOT / '07_output_3mf/quarantine' / f'{stem}_{polarity}.failure.json'
            mirror = data.get('mesh_artifact_metadata', {}).get(polarity)
            if mirror and not mesh_path.is_file():
                fail(f'graph mesh mirror without binary: {cid}/{polarity}')
            if eligible and not (mesh_path.is_file() or diagnostic.is_file()):
                fail(f'eligible SVG without generation outcome: {cid}/{polarity}')
            if mesh_path.is_file() and diagnostic.is_file():
                fail(f'conflicting mesh release and failure: {cid}/{polarity}')

    for entry in registry['references']:
        source = entry['canonical_relative_path']
        cid = by_source.get(source)
        if cid is None:
            fail('source without classified record: ' + source)
            continue
        data = records[cid]
        svg = next((s['path'] for s in svg_checks if s['comp_id'] == cid and s['released']), None)
        if data.get('compile_status') == 'PASSED' and svg is None:
            fail('PASSED record without released SVG: ' + cid)
        if not refresh:
            expected_meshes = sorted(m['path'] for m in mesh_checks if m['comp_id'] == cid)
            if (entry.get('associated_comp_id') != cid
                    or entry.get('knowledge_graph_record') != f'03_knowledge_graph/{cid}.json'
                    or entry.get('passport') != f'04_passports/{cid}.md'
                    or entry.get('svg') != svg
                    or sorted(entry.get('three_mf_artifacts', [])) != expected_meshes):
                fail('registry associations differ from actual artifacts: ' + cid)
        if refresh:
            variants = {}
            for polarity in ('cutout', 'raised'):
                released_mesh = next((m for m in mesh_checks if m['comp_id'] == cid and m['polarity'] == polarity), None)
                stem = Path(data.get('output_filename', cid)).stem
                failure_path = ROOT / '07_output_3mf/quarantine' / f'{stem}_{polarity}.failure.json'
                if released_mesh:
                    variants[polarity] = {'status': 'PASSED', 'artifact': released_mesh['path']}
                elif failure_path.exists():
                    diagnostic = json.loads(failure_path.read_text(encoding='utf-8'))
                    variants[polarity] = {'status': 'QUARANTINED', 'reasons': diagnostic['reasons']}
                else:
                    variants[polarity] = {'status': 'NOT_ELIGIBLE' if not data.get('artifact_metadata', {}).get('3mf_eligible') else 'NOT_GENERATED'}
            entry.update(associated_comp_id=cid, passport=f'04_passports/{cid}.md',
                knowledge_graph_record=f'03_knowledge_graph/{cid}.json', svg=svg,
                three_mf_artifacts=[m['path'] for m in mesh_checks if m['comp_id'] == cid],
                three_mf_variants=variants,
                status=data.get('compile_status', 'CLASSIFIED'))

    outside = []
    allowed = set(W.allowed_roots())
    for folder in ('01_input_raster', 'input'):
        base = ROOT / folder
        if not base.exists():
            continue
        for path in base.rglob('*'):
            if path.is_file() and path.suffix.lower() in W.IMAGE_EXTENSIONS:
                if not any(path.is_relative_to(ROOT / a) for a in allowed):
                    outside.append(path.relative_to(ROOT).as_posix())
    if outside:
        fail('unapproved raster sources remain: ' + str(len(outside)))
    archive_files = list((ROOT / '06_archive').rglob('*')) if (ROOT / '06_archive').exists() else []
    if archive_files:
        fail('archive is not empty')

    broken_links = []
    for path in ROOT.rglob('*.md'):
        if '.git' in path.relative_to(ROOT).parts:
            continue
        for link in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8', errors='replace')):
            if '://' in link or link.startswith(('#', 'mailto:')):
                if link.startswith('file:'):
                    broken_links.append(path.relative_to(ROOT).as_posix() + ': private file URI')
                continue
            target = link.split('#')[0].split(' "')[0]
            if target and not (path.parent / target).exists():
                broken_links.append(path.relative_to(ROOT).as_posix() + ': ' + target)
    if broken_links:
        fail('broken internal links: ' + str(len(broken_links)))

    if refresh and not errors:
        (ROOT / 'reference-whitelist.json').write_text(json.dumps(registry, indent=2) + '\n', encoding='utf-8')
    counts = {'raster_sources': registry['reference_count'], 'comp_records': len(records),
        'passports': len(list((ROOT / '04_passports').glob('*.md'))),
        'svg_outputs': len(list((ROOT / '05_output_svg').rglob('*.svg'))),
        'svg_released': sum(s['released'] for s in svg_checks),
        'svg_quarantined': len(list((ROOT / '05_output_svg/quarantine').glob('*.svg'))),
        '3mf_binaries': len(list((ROOT / '07_output_3mf').rglob('*.3mf'))),
        '3mf_sidecars': len(list((ROOT / '07_output_3mf').rglob('*.3mf.json'))),
        '3mf_failure_diagnostics': len(list((ROOT / '07_output_3mf').rglob('*.failure.json'))),
        'orphan_references': len(errors) - bool(broken_links) - bool(archive_files),
        'archive_files': sum(p.is_file() for p in archive_files), 'broken_internal_links': len(broken_links)}
    return {'status': 'PASS' if not errors else 'FAIL', 'counts': counts,
        'whitelist_fingerprint': registry['fingerprint_sha256'],
        'errors': errors, 'broken_links': broken_links, 'svg_checks': svg_checks, 'mesh_checks': mesh_checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-links', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = audit(args.refresh_links)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'counts', 'whitelist_fingerprint', 'errors')}, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
