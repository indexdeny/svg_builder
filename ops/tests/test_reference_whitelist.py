"""Membership, provenance and deletion containment regression tests."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import reference_whitelist as W
except ImportError:
    W = None


class WhitelistTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(W, 'whitelist enforcement is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '00_governance').mkdir()
        self.folders = ['01_input_raster/complex_gradients', '01_input_raster/high_contrast']
        (self.root / '00_governance/pipeline_config.json').write_text(json.dumps(
            {'source_dataset': {'allowed_roots': self.folders}}))
        for folder in self.folders:
            (self.root / folder).mkdir(parents=True)
        self.a = self.root / self.folders[0] / 'same.png'
        Image.new('RGB', (20, 10), 'white').save(self.a)
        self.b = self.root / self.folders[1] / 'same.png'
        Image.new('RGB', (12, 16), 'black').save(self.b)
        W.write_registry(self.root)

    def record(self, p):
        return {'source_file': p.relative_to(self.root).as_posix(),
                'source_sha256': hashlib.sha256(p.read_bytes()).hexdigest()}

    def cli(self, script, *arguments):
        scripts = self.root / 'ops/scripts'
        if not scripts.exists():
            original = Path(__file__).resolve().parents[1] / 'scripts'
            shutil.copytree(original, scripts, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(original.parents[1] / '00_governance/design-tokens.json', self.root / '00_governance/design-tokens.json')
        return subprocess.run([sys.executable, str(scripts / script), *map(str, arguments)],
                              cwd=self.root, capture_output=True, text=True, errors='replace')

    def test_duplicate_basenames_remain_distinct(self):
        entries = W.load_registry(self.root)['references']
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]['canonical_relative_path'], entries[1]['canonical_relative_path'])
        self.assertEqual(W.verify_record(self.record(self.a), self.root), self.a)
        self.assertEqual(W.verify_record(self.record(self.b), self.root), self.b)

    def test_basename_alone_cannot_establish_lineage(self):
        record = self.record(self.a)
        record['source_file'] = 'same.png'
        with self.assertRaises(ValueError):
            W.verify_record(record, self.root)

    def test_changed_source_bytes_are_rejected(self):
        record = self.record(self.a)
        Image.new('RGB', (20, 10), 'red').save(self.a)
        with self.assertRaises(ValueError):
            W.verify_record(record, self.root)

    def test_external_and_traversal_sources_are_rejected(self):
        for value in ('input/same.png', '../same.png', str(self.a)):
            record = self.record(self.a)
            record['source_file'] = value
            with self.assertRaises(ValueError):
                W.verify_record(record, self.root)

    def test_missing_category_blocks_inventory(self):
        self.b.unlink()
        self.b.parent.rmdir()
        with self.assertRaises((ValueError, FileNotFoundError)):
            W.write_registry(self.root)

    def test_unregistered_new_source_blocks_intake(self):
        Image.new('RGB', (10, 10), 'red').save(self.a.parent / 'new.png')
        with self.assertRaises(ValueError):
            W.load_registry(self.root)

    def test_missing_hash_cannot_establish_lineage(self):
        with self.assertRaises(ValueError):
            W.verify_record({'source_file': self.a.relative_to(self.root).as_posix()}, self.root)

    def test_symlink_cannot_escape_allowed_category(self):
        external = self.root / 'external.png'
        Image.new('RGB', (10, 10), 'red').save(external)
        link = self.a.parent / 'escape.png'
        try:
            link.symlink_to(external)
        except OSError as error:
            self.skipTest('symlink privilege unavailable: ' + str(error))
        with self.assertRaises(ValueError):
            W.write_registry(self.root)

    def test_cleanup_only_removes_matching_outputs(self):
        output = self.root / '05_output_svg'
        output.mkdir()
        (output / 'old.svg').write_text('<svg/>')
        (output / 'keep.txt').write_text('keep')
        W.clean_generated(output, ['*.svg'], self.root)
        self.assertFalse((output / 'old.svg').exists())
        self.assertTrue((output / 'keep.txt').exists())
        self.assertTrue(self.a.exists())

    def test_cleanup_rejects_sources_and_external_directory(self):
        for path in (self.a.parent, self.root.parent):
            with self.assertRaises(ValueError):
                W.clean_generated(path, ['*'], self.root)
        self.assertTrue(self.a.exists())

    def test_svg_stage_rejects_unapproved_source(self):
        import compile_svg
        with self.assertRaises(ValueError):
            compile_svg.find_source_image('input/rogue.png')

    def test_legacy_bridge_rejects_unapproved_source(self):
        import vtracer_bridge
        with self.assertRaises(ValueError):
            vtracer_bridge.find_image_path('input/rogue.png')

    def test_mesh_stage_rejects_unapproved_record_before_writing(self):
        import compile_3mf
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.root / 'ops/logs').mkdir(parents=True)
        with self.assertRaises(ValueError):
            compile_3mf.process_one('COMP-999999', {'source_file': 'input/rogue.png'}, None)

    def test_contrast_processing_preserves_original_bytes(self):
        import enhance_contrast_kolia as contrast
        self.assertTrue(callable(getattr(contrast, 'enhance', None)), 'contrast tool requires safe explicit output')
        original = self.a.read_bytes()
        output = self.root / '02_processing/contrast/result.png'
        contrast.enhance(self.a, output, self.root)
        self.assertEqual(self.a.read_bytes(), original)
        self.assertTrue(output.is_file())
        with self.assertRaises(ValueError):
            contrast.enhance(self.a, self.a, self.root)

    def test_legacy_mutation_is_limited_to_explicit_processing_output(self):
        self.assertTrue(callable(getattr(W, 'processing_directory', None)))
        with self.assertRaises(ValueError):
            W.processing_directory(self.a.parent, self.root)
        with self.assertRaises(ValueError):
            W.processing_directory(self.root.parent, self.root)
        processing = self.root / '02_processing/copies'
        processing.mkdir(parents=True)
        self.assertEqual(W.processing_directory(processing, self.root), processing)

    def test_autocrop_backup_cannot_target_originals(self):
        folder = self.root / '02_processing/copies'
        folder.mkdir(parents=True)
        shutil.copy2(self.a, folder / self.a.name)
        original = self.a.read_bytes()
        result = self.cli('autocrop.py', folder, '--apply', '--backup', self.a.parent)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('processing', result.stderr)
        self.assertEqual(original, self.a.read_bytes())

    def test_rename_prefix_cannot_escape_output_folder(self):
        folder = self.root / '02_processing/copies'
        folder.mkdir(parents=True)
        copied = folder / self.a.name
        shutil.copy2(self.a, copied)
        result = self.cli('rename_photos.py', folder, '--apply', '--prefix', '../../01_input_raster/high_contrast/rogue')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(copied.exists(), 'Unsafe rename must fail before the first move')

    def test_vectorizer_cli_cannot_overwrite_original(self):
        original = self.a.read_bytes()
        result = self.cli('hifi_vectorize.py', self.a, self.a)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.a.read_bytes(), original)
        self.assertIn('Output', result.stderr)

    def test_automation_references_identify_actual_source_and_record(self):
        import validate
        self.assertTrue(callable(getattr(validate, 'make_automation_meta', None)))
        source = self.a.relative_to(self.root).as_posix()
        meta = validate.make_automation_meta('COMP-100001', source, '05_output_svg/svg_same.svg')
        self.assertEqual(meta['entity_id'], 'COMP-100001')
        self.assertEqual(meta['source_ref'], source)
        self.assertEqual(meta['export_path'], '05_output_svg/svg_same.svg')
        self.assertEqual(validate.make_automation_meta('COMP-100001', source)['export_path'], '')


if __name__ == '__main__':
    unittest.main()
