"""Package integrity witnesses, including extra files and corrupt payloads."""
import importlib.util, unittest
from tests.helpers import case_dir
class ReleaseTests(unittest.TestCase):
    def module(self):
        p=__import__('pathlib').Path(__file__).resolve().parents[1]/'scripts/package_release.py'
        self.assertTrue(p.exists(),'Portable package verifier missing')
        spec=importlib.util.spec_from_file_location('package_release',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
    def test_exact_payload_hashes_and_extras(self):
        m=self.module();root=case_dir('package');(root/'file.txt').write_bytes(b'fictional')
        import hashlib
        (root/'PACKAGE_SHA256SUMS.txt').write_text(hashlib.sha256(b'fictional').hexdigest()+'  file.txt\n',encoding='utf-8')
        self.assertEqual(m.verify(root),1)
        (root/'extra.txt').write_text('unexpected',encoding='utf-8')
        with self.assertRaises(ValueError):m.verify(root)
        (root/'extra.txt').unlink();(root/'file.txt').write_text('corrupted',encoding='utf-8')
        with self.assertRaises(ValueError):m.verify(root)
    def test_manifest_traversal_refused(self):
        m=self.module();root=case_dir('package')
        (root/'PACKAGE_SHA256SUMS.txt').write_text('0'*64+'  ../outside.txt\n',encoding='utf-8')
        with self.assertRaises(ValueError):m.verify(root)
