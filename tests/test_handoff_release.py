"""Runtime export requirements before handoff packaging is implemented."""
import tempfile,unittest,zipfile
from pathlib import Path
from scripts import package_handoffs
from tests.helpers import case_dir

class HandoffReleaseTests(unittest.TestCase):
    def test_archive_is_deterministic_and_has_no_runtime_credentials(self):
        root=case_dir('handoff-package');a=root/'first.zip';b=root/'second.zip'
        self.assertEqual(package_handoffs.build(a),package_handoffs.build(b))
        with zipfile.ZipFile(a) as z:
            names=z.namelist()
            for required in ['Start Common Room Handoffs.cmd','common_room/participant.py','common_room/handoff_room.py','HANDOFF_RUNBOOK_2026-09-12.md','scripts/check_handoff_package.py']:
                self.assertIn(required,names)
            self.assertFalse(any('.runtime' in n or 'credential.json' in n or n=='CONNECTION_ACCEPTANCE.json' for n in names))
            extracted=root/'extracted';z.extractall(extracted)
        self.assertEqual(package_handoffs.verify(extracted),len(names)-1)
        (extracted/'web/board.js').write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError):package_handoffs.verify(extracted)
