"""Run the complete pipeline on clearly synthetic geometric test fixtures.

Fixtures live only in an isolated temporary project, never in the owner's dataset.
No original image, portrait, logo, SVG or 3MF is needed from the owner's machine.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix='svg-builder-public-smoke-') as folder:
        sandbox = Path(folder).resolve()
        assert sandbox.parent == Path(tempfile.gettempdir()).resolve()
        assert not sandbox.is_relative_to(ROOT)
        shutil.copytree(ROOT / 'ops/scripts', sandbox / 'ops/scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / '00_governance', sandbox / '00_governance')
        for category in ('complex_gradients', 'high_contrast'):
            (sandbox / '01_input_raster' / category).mkdir(parents=True)
        image = Image.new('RGB', (180, 120), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((35, 25, 145, 95), fill='black')
        draw.rectangle((65, 45, 115, 75), fill='white')
        image.save(sandbox / '01_input_raster/high_contrast/synthetic_frame.png')
        image = Image.new('RGB', (160, 128), 'white')
        draw = ImageDraw.Draw(image)
        draw.ellipse((30, 20, 130, 108), outline='black', width=6)
        draw.line((45, 85, 80, 35, 115, 85), fill='black', width=4)
        image.save(sandbox / '01_input_raster/complex_gradients/synthetic_lines.png')
        commands = [
            ['reference_whitelist.py', '--refresh'], ['deconstruct.py'], ['validate.py'],
            ['compile_svg.py'], ['compile_3mf.py'],
            ['validate_dataset.py', '--refresh-links', '--report', 'smoke-result.json']]
        for arguments in commands:
            result = subprocess.run([sys.executable, '-u', 'ops/scripts/' + arguments[0], *arguments[1:]],
                                    cwd=sandbox, capture_output=True, text=True, errors='replace')
            print(f'{arguments[0]}: exit {result.returncode}', flush=True)
            if result.returncode:
                print(result.stdout[-6000:])
                print(result.stderr[-3000:])
                return result.returncode
        result = json.loads((sandbox / 'smoke-result.json').read_text())
        counts = result['counts']
        if counts['svg_released'] != 2 or counts['3mf_binaries'] != 4:
            raise RuntimeError('Expected two SVGs and both 3MF polarities for both synthetic fixtures')
        registry = json.loads((sandbox / 'reference-whitelist.json').read_text())
        entry = registry['references'][0]
        cid = entry['associated_comp_id']
        stem = Path(entry['svg']).stem
        (sandbox / 'tiny-printer.json').write_text(json.dumps({'max_printer_bounds_mm': [1, 1, 1]}))
        def execute(script, arguments, expected):
            completed = subprocess.run([sys.executable, 'ops/scripts/' + script, *arguments],
                cwd=sandbox, capture_output=True, text=True, errors='replace')
            if completed.returncode != expected:
                raise RuntimeError(script + ': unexpected exit\n' + completed.stdout[-3000:] + completed.stderr[-2000:])
        execute('compile_3mf.py', ['--only', cid, '--profile', 'tiny-printer.json', '--no-clean'], 1)
        for polarity in ('cutout', 'raised'):
            if (sandbox / '07_output_3mf' / f'{stem}_{polarity}.3mf').exists():
                raise RuntimeError('Stale released binary survived a failed repeat attempt')
        execute('validate_dataset.py', ['--refresh-links'], 0)
        execute('compile_3mf.py', ['--only', cid, '--no-clean'], 0)
        for polarity in ('cutout', 'raised'):
            if (sandbox / '07_output_3mf/quarantine' / f'{stem}_{polarity}.failure.json').exists():
                raise RuntimeError('Stale failure survived a successful repeat attempt')
        execute('validate_dataset.py', ['--refresh-links'], 0)
        missing = sandbox / '07_output_3mf' / f'{stem}_cutout.3mf'
        assert missing.resolve().is_relative_to(sandbox / '07_output_3mf')
        saved = missing.read_bytes()
        missing.unlink()
        execute('validate_dataset.py', [], 1)
        missing.write_bytes(saved)
        execute('validate_dataset.py', [], 0)
        execute('sketch_vectorize.py', ['01_input_raster/complex_gradients/synthetic_lines.png',
                                     '02_processing/sketch_candidate.svg'], 0)
        print('Repeat PASS -> FAIL -> PASS; missing binary rejected; SKETCH_VECTOR CLI passed.', flush=True)
        print(json.dumps({'status': 'PASS', 'fixture_dataset': 'synthetic geometry; not owner references',
                          'counts': counts}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
