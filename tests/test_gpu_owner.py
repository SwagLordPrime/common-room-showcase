"""The shared GPU calendar is wired to the two local workers (tasks gpu-owner and gpu-owner-decouple, plan turnons-2026-10-03).

Shane approved the location D:\\Work Area\\Artifacts\\shared\\gpu-calendar.json and ruled that the two "GPU desk"
windows belong to BATCH. He edits that file by hand and this suite is the switch-over gate, so NOTHING HERE READS IT:
a missing or edited calendar can never fail the suite. What is tested instead:
  - both worker configs name the shared path, and the worker's own config reader hands it back;
  - load_calendar and the owner rule are checked on a fixture built inside the test from the in-tree example
    (crew/gpu-calendar.example.json), never on the real file. The README beside the real file has the check command for it;
  - every test below runs under a tripwire that fails it if it touches the shared file or lists its folder, and the
    tripwire itself is tested on a throwaway path so it cannot be vacuous.

RED note: the first version of this file (gpu-owner, 79ad23f) read the real file, so it failed with that file missing
(1 failure, 1 error) or its GPU desk owners edited (1 failure); the tests below pass either way (measured in the
gpu-owner-decouple report).
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import builtins, io, json, os, unittest
from tests.helpers import case_dir
from common_room import gpu_calendar as gc
from common_room.ollama_worker import read_config

ROOT = Path(__file__).resolve().parents[1]
SHARED = r'D:\Work Area\Artifacts\shared\gpu-calendar.json'
WORKERS = ('local-a.json', 'local-b.json')
EXAMPLE = ROOT / 'crew' / 'gpu-calendar.example.json'


def desk_owners(windows):
    """The owners of the windows labelled 'GPU desk...'. Shane's ruling: they all belong to batch."""
    return sorted({w.owner for w in windows if w.label.startswith('GPU desk')})


class Tripwire:
    """While installed, records every attempt to open, stat, test or list `target` (a file) or its folder."""
    PATCHED = ((builtins, 'open'), (io, 'open'), (os, 'stat'), (os, 'lstat'), (os, 'listdir'), (os, 'scandir'),
               (os.path, 'isfile'), (os.path, 'exists'), (os.path, 'isdir'))

    def __init__(self, target):
        self.hits = []
        self.target = os.path.normcase(os.path.abspath(target))
        self.folder = os.path.dirname(self.target)
        self.saved = []

    def _wrap(self, name, real):
        def wrapper(*args, **kwargs):
            where = args[0] if args else kwargs.get('path', kwargs.get('file'))
            try:
                if where is not None and not isinstance(where, int):
                    seen = os.path.normcase(os.path.abspath(os.fsdecode(where)))
                    if seen in (self.target, self.folder):
                        self.hits.append('%s(%s)' % (name, seen))
            except Exception:
                pass
            return real(*args, **kwargs)
        return wrapper

    def __enter__(self):
        for owner, name in self.PATCHED:
            real = getattr(owner, name)
            self.saved.append((owner, name, real))
            setattr(owner, name, self._wrap(name, real))
        return self

    def __exit__(self, *exc):
        for owner, name, real in reversed(self.saved):
            setattr(owner, name, real)
        self.saved = []


class GuardedCase(unittest.TestCase):
    """Every test in a subclass runs under the tripwire and fails if it touched the shared calendar or its folder."""
    def setUp(self):
        self.tripwire = Tripwire(SHARED).__enter__()

    def tearDown(self):
        self.tripwire.__exit__(None, None, None)
        self.assertEqual(self.tripwire.hits, [], 'this suite must never read the hand-edited shared calendar')


@unittest.skipUnless(os.name == 'nt', 'the shared calendar path is a Windows path (D:\\Work Area)')
class SharedCalendarWiringTests(GuardedCase):
    def test_both_worker_configs_name_the_shared_calendar(self):
        for name in WORKERS:
            config = json.loads((ROOT / 'crew' / name).read_text(encoding='utf-8'))
            self.assertEqual(config.get('gpu_calendar'), SHARED, name + ' must set gpu_calendar to the shared file')

    def test_the_workers_own_config_reader_hands_the_same_absolute_path_back(self):
        for name in WORKERS:
            config = read_config(ROOT / 'crew' / name)
            self.assertEqual(config.get('gpu_calendar'), SHARED, name)
            self.assertTrue(Path(config['gpu_calendar']).is_absolute(), name)

    def test_the_workers_keep_their_own_quiet_windows_as_well(self):
        # The workers take the union of their own windows and the calendar (GPU_CALENDAR.md), so the
        # wiring must not have removed the safety net.
        for name in WORKERS:
            self.assertGreaterEqual(len(read_config(ROOT / 'crew' / name)['quiet_windows']), 5, name)


class FixtureCalendarTests(GuardedCase):
    """load_calendar and the owner rule, on calendars built here from the in-tree example."""
    def fixture(self, owner_for_desks):
        data = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        for row in data['windows']:
            if row['label'].startswith('GPU desk'):
                row['owner'] = owner_for_desks
        path = case_dir('gpu-owner') / 'calendar.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path, data

    def test_a_calendar_of_the_real_shape_loads_with_both_gpu_desk_windows_owned_by_batch(self):
        path, data = self.fixture('batch')
        windows = gc.load_calendar(path)
        self.assertEqual(len(windows), len(data['windows']))
        self.assertEqual([w.label for w in windows if w.label.startswith('GPU desk')],
                         ['GPU desk (midday)', 'GPU desk (afternoon)'])
        self.assertEqual(desk_owners(windows), ['batch'])

    def test_the_owner_rule_tells_a_wrong_calendar_from_the_ruling(self):
        right = gc.load_calendar(self.fixture('batch')[0])
        wrong = gc.load_calendar(self.fixture('shane')[0])
        self.assertEqual(desk_owners(right), ['batch'])
        self.assertEqual(desk_owners(wrong), ['shane'])
        self.assertNotEqual(desk_owners(right), desk_owners(wrong))

    def test_correcting_the_desk_owners_changes_no_window_time_or_label(self):
        path, data = self.fixture('batch')
        seed = json.loads(EXAMPLE.read_text(encoding='utf-8'))['windows']
        got = [(w.days, w.start, w.end, w.label) for w in gc.load_calendar(path)]
        want = [(w.days, w.start, w.end, w.label) for w in gc.parse_calendar({'protocol': 'gpu-calendar/1', 'windows': seed})]
        self.assertEqual(got, want)


class TripwireTests(unittest.TestCase):
    """The tripwire is not vacuous: it sees every kind of touch. Uses a throwaway path, never the real calendar."""
    def test_it_records_open_stat_exists_isfile_and_a_folder_listing(self):
        folder = case_dir('gpu-owner-tripwire') / 'shared'
        target = folder / 'gpu-calendar.json'                      # neither exists
        with Tripwire(str(target)) as wire:
            for call in (lambda: open(str(target), 'rb'), lambda: target.stat(), lambda: os.path.exists(str(target)),
                         lambda: os.path.isfile(str(target)), lambda: os.listdir(str(folder)), lambda: list(os.scandir(str(folder)))):
                try:
                    call()
                except OSError:
                    pass
        kinds = sorted({h.split('(')[0] for h in wire.hits})
        self.assertEqual(kinds, sorted(['open', 'stat', 'exists', 'isfile', 'listdir', 'scandir']), wire.hits)

    def test_it_ignores_other_paths_and_puts_everything_back(self):
        saved = [getattr(owner, name) for owner, name in Tripwire.PATCHED]
        elsewhere = case_dir('gpu-owner-tripwire') / 'other.json'
        elsewhere.write_text('{}', encoding='utf-8')
        with Tripwire(str(case_dir('gpu-owner-tripwire') / 'shared' / 'gpu-calendar.json')) as wire:
            elsewhere.read_bytes()
            self.assertTrue(elsewhere.is_file())
        self.assertEqual(wire.hits, [])
        self.assertEqual([getattr(owner, name) for owner, name in Tripwire.PATCHED], saved)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
