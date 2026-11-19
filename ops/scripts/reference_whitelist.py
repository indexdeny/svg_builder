"""Canonical path + SHA-256 membership for the owner's two source folders."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif'}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def reject_links(path, root):
    path, root = Path(path).absolute(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path escapes project root')
    for part in (path, *path.parents):
        if part == root:
            break
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                raise ValueError('Links/reparse points are not allowed: ' + str(part.relative_to(root)))


def allowed_roots(root=ROOT):
    root = Path(root).resolve()
    config = json.loads((root / '00_governance/pipeline_config.json').read_text(encoding='utf-8'))
    folders = config['source_dataset']['allowed_roots']
    expected = {'01_input_raster/complex_gradients', '01_input_raster/high_contrast'}
    if len(folders) != 2 or set(folders) != expected:
        raise ValueError('Configuration must contain exactly the two owner-approved folders')
    return folders


def discover(root=ROOT):
    root = Path(root).resolve()
    entries = []
    for category in allowed_roots(root):
        folder = root / category
        reject_links(folder, root)
        if not folder.is_dir():
            raise FileNotFoundError('Allowed source folder missing: ' + category)
        def onerror(error):
            raise error
        for base, dirs, files in os.walk(folder, onerror=onerror, followlinks=False):
            for name in dirs + files:
                reject_links(Path(base) / name, root)
            for name in sorted(files):
                path = Path(base) / name
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    if name != '.gitkeep':
                        raise ValueError('Unexpected non-image in source folder: ' + path.relative_to(root).as_posix())
                    continue
                with Image.open(path) as image:
                    image.load()
                    entries.append({'canonical_relative_path': path.relative_to(root).as_posix(),
                        'filename': path.name, 'extension': path.suffix.lower(),
                        'file_size': path.stat().st_size, 'sha256': sha256(path),
                        'width': image.width, 'height': image.height, 'format': image.format,
                        'source_category': Path(category).name})
    return sorted(entries, key=lambda item: item['canonical_relative_path'])


def fingerprint(entries):
    identities = [{k: e[k] for k in ('canonical_relative_path', 'sha256')} for e in entries]
    return hashlib.sha256(json.dumps(identities, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_registry(root=ROOT):
    root = Path(root).resolve()
    entries = discover(root)
    old_path = root / 'reference-whitelist.json'
    previous = json.loads(old_path.read_text(encoding='utf-8')) if old_path.exists() else {}
    old = {(e['canonical_relative_path'], e['sha256']): e for e in previous.get('references', [])}
    for entry in entries:
        former = old.get((entry['canonical_relative_path'], entry['sha256']), {})
        for key, default in {'associated_comp_id': None, 'passport': None,
            'knowledge_graph_record': None, 'svg': None, 'three_mf_artifacts': [],
            'three_mf_variants': {},
            'status': 'SOURCE_ONLY'}.items():
            entry[key] = former.get(key, default)
    result = {'schema': 'reference_whitelist_v1', 'membership_rule': 'actual files under allowed_roots',
        'allowed_roots': allowed_roots(root), 'fingerprint_sha256': fingerprint(entries),
        'reference_count': len(entries), 'references': entries}
    old_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return result


def load_registry(root=ROOT, check_files=True):
    root = Path(root).resolve()
    data = json.loads((root / 'reference-whitelist.json').read_text(encoding='utf-8'))
    expected = data['references']
    if (len({e['canonical_relative_path'] for e in expected}) != len(expected)
            or data.get('reference_count') != len(expected)
            or fingerprint(expected) != data.get('fingerprint_sha256')
            or data.get('allowed_roots') != allowed_roots(root)):
        raise ValueError('Invalid whitelist registry')
    if not check_files:
        return data
    actual = discover(root)
    if (data.get('reference_count') != len(actual) or len(expected) != len(actual)
            or fingerprint(expected) != data.get('fingerprint_sha256')
            or fingerprint(actual) != data['fingerprint_sha256']):
        raise ValueError('Whitelist differs from actual source files; inspect changes and refresh registry')
    for a, e in zip(actual, expected):
        if any(a[key] != e.get(key) for key in a):
            raise ValueError('Whitelist file metadata mismatch: ' + a['canonical_relative_path'])
    return data


def verify_record(record, root=ROOT):
    root = Path(root).resolve()
    source, digest = record.get('source_file'), record.get('source_sha256')
    if not isinstance(source, str) or not digest or '\\' in source:
        raise ValueError('Source requires canonical relative path and SHA-256')
    path = Path(source)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != source:
        raise ValueError('Noncanonical source path')
    registry = load_registry(root, check_files=False)
    entry = next((e for e in registry['references'] if e['canonical_relative_path'] == source), None)
    if entry is None or entry['sha256'] != digest:
        raise ValueError('Source path/hash is not in the canonical whitelist')
    reject_links(root / source, root)
    if not (root / source).is_file() or sha256(root / source) != digest:
        raise ValueError('Source bytes differ from the whitelist')
    return root / source


def verify_path(path, root=ROOT):
    """CLI convenience: exact project-relative path, never basename lookup."""
    root = Path(root).resolve()
    path = Path(path)
    if '..' in path.parts:
        raise ValueError('Parent traversal is not allowed')
    candidate = path if path.is_absolute() else root / path
    reject_links(candidate, root)
    if not candidate.is_file():
        raise ValueError('Source image is absent')
    return verify_record({'source_file': candidate.relative_to(root).as_posix(),
                          'source_sha256': sha256(candidate)}, root)


def clean_generated(output_dir, patterns, root=ROOT):
    """Validate the complete deletion set before deleting matching output files."""
    root = Path(root).resolve()
    output_dir = Path(output_dir).absolute()
    reject_links(output_dir, root)
    if output_dir.resolve() not in {root / '05_output_svg', root / '07_output_3mf'}:
        raise ValueError('Cleanup requires an explicit SVG or 3MF output directory')
    paths = []
    for pattern in patterns:
        if '..' in Path(pattern).parts or Path(pattern).is_absolute():
            raise ValueError('Unsafe cleanup pattern')
        for path in output_dir.glob(pattern):
            reject_links(path, root)
            if not path.resolve().is_relative_to(output_dir.resolve()):
                raise ValueError('Cleanup target escapes output directory')
            if path.is_file() and path not in paths:
                paths.append(path)
    for path in paths:
        path.unlink()
    return len(paths)


def processing_directory(folder, root=ROOT, allow_missing=False):
    """Legacy destructive helpers may act only on explicit processing copies."""
    root = Path(root).resolve()
    folder = Path(folder).absolute()
    reject_links(folder, root)
    if (not folder.resolve().is_relative_to(root / '02_processing')
            or (not allow_missing and not folder.is_dir())):
        raise ValueError('Use an explicit existing output folder under 02_processing; originals are immutable')
    if folder.is_dir():
        for path in folder.iterdir():
            reject_links(path, root)
    return folder


def output_file(path, root=ROOT, suffix=None):
    """Constrain auxiliary writes to processing/staging/diagnostic output areas."""
    root = Path(root).resolve()
    path = Path(path).absolute()
    reject_links(path, root)
    directories = ('02_processing', '05_output_svg/staging', '05_output_svg/quarantine', 'ops/qa_previews')
    if (not any(path.resolve().is_relative_to(root / directory) for directory in directories)
            or (suffix is not None and path.suffix.lower() != suffix)):
        raise ValueError('Output must use an explicit processing/staging/diagnostic path with the expected extension')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Inventory actual files after owner-approved dataset changes')
    args = parser.parse_args()
    registry = write_registry() if args.refresh else load_registry()
    print(json.dumps({'reference_count': registry['reference_count'],
                      'fingerprint_sha256': registry['fingerprint_sha256']}))


if __name__ == '__main__':
    main()
