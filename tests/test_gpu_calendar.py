"""The shared GPU calendar file (common_room/gpu_calendar.py): loading, the quiet question,
midnight crossing and the plain-words reasons a bad file gives.

RED note: written before the module existed, so its first run on the base commit fails at
import ("cannot import name 'gpu_calendar' from 'common_room'").
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json, unittest
from datetime import datetime, timedelta, timezone
from tests.helpers import case_dir
from common_room import gpu_calendar as gc
from common_room.ollama_worker import CentralTime

CDT = timezone(timedelta(hours=-5))
CST = timezone(timedelta(hours=-6))


def cdt(*args):
    return datetime(*args, tzinfo=CDT).timestamp()


def calendar(*windows, **extra):
    return dict({'protocol': 'gpu-calendar/1', 'windows': list(windows)}, **extra)


def window(days, start, end, owner='shane', label=None):
    row = {'days': days, 'start': start, 'end': end, 'owner': owner}
    if label is not None:
        row['label'] = label
    return row


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.dir = case_dir('gpu-calendar')
        self.central = CentralTime(use_zoneinfo=False)      # fixed rules, no tzdata needed

    def write(self, value, name='calendar.json', raw=None):
        path = self.dir / name
        path.write_bytes(raw if raw is not None else json.dumps(value).encode('utf-8'))
        return path

    def windows(self, *rows):
        return gc.parse_calendar(calendar(*rows))

    def reason(self, value=None, raw=None, path=None):
        with self.assertRaises(gc.CalendarError) as caught:
            gc.load_calendar(path or self.write(value, raw=raw))
        return str(caught.exception)

    # -- loading ---------------------------------------------------------------------------
    def test_loads_the_documented_format(self):
        path = self.write(calendar(window('weekdays', '08:20', '09:00', 'batch', 'BATCH desks'),
                                   window('daily', '20:50', '21:35', 'common-room'),
                                   window('mon,wed,fri', '07:00', '07:30'),
                                   window('weekends', '10:00', '11:00'),
                                   window([5, 6], '12:00', '13:00'), updated='2026-10-01', note='x'))
        windows = gc.load_calendar(path)
        self.assertEqual(len(windows), 5)
        self.assertEqual(windows[0], gc.Window(frozenset(range(5)), 8 * 60 + 20, 9 * 60, 'batch', 'BATCH desks'))
        self.assertEqual(windows[1].label, '20:50-21:35')                  # label defaults to the times
        self.assertEqual(windows[2].days, frozenset({0, 2, 4}))
        self.assertEqual(windows[3].days, frozenset({5, 6}))
        self.assertEqual(windows[4].days, frozenset({5, 6}))
        self.assertEqual(gc.load_calendar(self.write(calendar())), [])     # no windows: valid, never quiet
        bom = self.write(None, 'bom.json', raw=b'\xef\xbb\xbf' + json.dumps(calendar()).encode('utf-8'))
        self.assertEqual(gc.load_calendar(bom), [])                          # editors on Windows add a BOM

    def test_day_names_are_case_and_space_tolerant(self):
        self.assertEqual(self.windows(window(' Mon , TUE ', '01:00', '02:00'))[0].days, frozenset({0, 1}))
        self.assertEqual(self.windows(window('SUN', '01:00', '02:00'))[0].days, frozenset({6}))

    def test_bad_files_give_a_plain_reason(self):
        missing = str(self.dir / 'nope.json')
        self.assertIn('not found', self.reason(path=self.dir / 'nope.json'))
        self.assertIn(missing, self.reason(path=self.dir / 'nope.json'))
        self.assertIn('not a file', self.reason(path=self.dir))
        self.assertIn('not valid UTF-8 JSON', self.reason(raw=b'{"protocol": '))
        self.assertIn('not valid UTF-8 JSON', self.reason(raw=b'\xff\xfe\x00bad'))
        self.assertIn('not valid UTF-8 JSON', self.reason(raw=b'[' * 5000))        # nesting deep enough to overflow the parser
        self.assertIn('larger than', self.reason(raw=b' ' * (gc.MAX_BYTES + 1)))
        self.assertIn('JSON object', self.reason([1, 2]))
        self.assertIn('protocol', self.reason({'windows': []}))
        self.assertIn('protocol', self.reason(dict(calendar(), protocol='gpu-calendar/2')))
        self.assertIn('windows', self.reason({'protocol': 'gpu-calendar/1'}))
        self.assertIn('windows', self.reason({'protocol': 'gpu-calendar/1', 'windows': 'daily'}))
        self.assertIn('Unknown calendar keys: extra', self.reason(dict(calendar(), extra=1)))
        self.assertIn('at most %d' % gc.MAX_WINDOWS,
                      self.reason(calendar(*[window('daily', '01:00', '02:00')] * (gc.MAX_WINDOWS + 1))))

    def test_bad_windows_say_which_window_and_why(self):
        cases = [
            (window('weekdays', '8:20', '09:00'), 'Window 2 start must be HH:MM'),
            (window('weekdays', '08:20', '24:00'), 'Window 2 end must be HH:MM'),
            (window('weekdays', '08:20', '08:20'), 'must differ'),
            (window('someday', '08:20', '09:00'), 'days must be'),
            (window('mon,funday', '08:20', '09:00'), 'days must be'),
            (window([7], '08:20', '09:00'), 'days must be'),
            (window([True], '08:20', '09:00'), 'days must be'),
            (window('', '08:20', '09:00'), 'days must be'),
            (window('daily', '08:20', '09:00', owner='stranger'), 'owner must be one of'),
            ({'days': 'daily', 'start': '08:20', 'end': '09:00'}, 'needs days, start, end and owner'),
            (dict(window('daily', '08:20', '09:00'), colour='red'), 'needs days, start, end and owner'),
            (dict(window('daily', '08:20', '09:00'), label=5), 'label must be text'),
            ('daily', 'needs days, start, end and owner'),
        ]
        for row, phrase in cases:
            with self.subTest(row=row):
                text = self.reason(calendar(window('daily', '01:00', '02:00'), row))      # the bad one is window 2
                self.assertIn('Window 2', text)
                self.assertIn(phrase, text)

    def test_labels_are_clipped_and_stripped_of_control_characters(self):
        label = self.windows(window('daily', '01:00', '02:00', label='a\x00b\x1b' + 'z' * 200))[0].label
        self.assertEqual(len(label), gc.LABEL_LIMIT)
        self.assertTrue(label.startswith('ab'))
        self.assertNotIn('\x00', label)

    # -- the quiet question -----------------------------------------------------------------
    def test_is_quiet_and_span(self):
        windows = self.windows(window('weekdays', '11:30', '12:05', 'shane', 'GPU desk'))
        wed = lambda h, m: cdt(2026, 9, 23, h, m)                           # a Wednesday (CDT)
        self.assertTrue(gc.is_quiet(wed(11, 30), windows, self.central))      # start is inside
        self.assertTrue(gc.is_quiet(wed(12, 4), windows, self.central))
        self.assertFalse(gc.is_quiet(wed(12, 5), windows, self.central))      # end is outside
        self.assertFalse(gc.is_quiet(wed(11, 29), windows, self.central))
        self.assertFalse(gc.is_quiet(cdt(2026, 9, 26, 11, 40), windows, self.central))      # Saturday
        hit = gc.quiet_window(wed(11, 20), windows, 600, self.central)         # 11:20 + 10 min touches 11:30
        self.assertEqual((hit.label, hit.owner), ('GPU desk', 'shane'))
        self.assertEqual(hit.end - hit.start, 35 * 60)
        self.assertIsNone(gc.quiet_window(wed(11, 19), windows, 600, self.central))
        self.assertIsNone(gc.quiet_window(wed(11, 40), [], 600, self.central))

    def test_midnight_crossing_window(self):
        windows = self.windows(window('fri', '22:00', '06:00', 'batch', 'night shift'))
        at = lambda d, h, m: cdt(2026, 9, d, h, m)            # Fri 25 Sep 2026, Sat 26, Sun 27
        self.assertTrue(gc.is_quiet(at(25, 22, 0), windows, self.central))
        self.assertTrue(gc.is_quiet(at(25, 23, 59), windows, self.central))
        self.assertTrue(gc.is_quiet(at(26, 0, 0), windows, self.central))      # the next day, still the same window
        self.assertTrue(gc.is_quiet(at(26, 5, 59), windows, self.central))
        self.assertFalse(gc.is_quiet(at(26, 6, 0), windows, self.central))
        self.assertFalse(gc.is_quiet(at(25, 21, 59), windows, self.central))
        self.assertFalse(gc.is_quiet(at(26, 22, 0), windows, self.central))    # Saturday night is not listed
        self.assertFalse(gc.is_quiet(at(27, 3, 0), windows, self.central))     # Sunday 03:00: nothing started Saturday
        self.assertFalse(gc.is_quiet(at(24, 23, 0), windows, self.central))    # Thursday night: not listed
        self.assertEqual(gc.next_change(at(25, 23, 0), windows, self.central), at(26, 6, 0))
        self.assertEqual(gc.next_change(at(26, 7, 0), windows, self.central), datetime(2026, 10, 2, 22, 0, tzinfo=CDT).timestamp())
        daily = self.windows(window('daily', '22:00', '06:00'))
        self.assertTrue(gc.is_quiet(at(27, 3, 0), daily, self.central))
        self.assertTrue(gc.quiet_window(at(26, 21, 55), daily, 600, self.central))     # a job would run into it

    def test_midnight_crossing_across_the_clocks_changing_back(self):
        windows = self.windows(window('daily', '22:00', '06:00'))
        # Sat 31 Oct 2026 22:00 CDT opens it; the clocks fall back at 02:00 on Sunday 1 Nov; it ends 06:00 CST.
        self.assertTrue(gc.is_quiet(datetime(2026, 10, 31, 23, 0, tzinfo=CDT), windows, self.central))
        self.assertTrue(gc.is_quiet(datetime(2026, 11, 1, 5, 59, tzinfo=CST), windows, self.central))
        self.assertFalse(gc.is_quiet(datetime(2026, 11, 1, 6, 0, tzinfo=CST), windows, self.central))
        self.assertEqual(gc.next_change(datetime(2026, 11, 1, 1, 30, tzinfo=CDT), windows, self.central),
                         datetime(2026, 11, 1, 6, 0, tzinfo=CST).timestamp())
        # A naive datetime reads as Central wall time.
        self.assertTrue(gc.is_quiet(datetime(2026, 7, 15, 23, 0), windows, self.central))
        self.assertFalse(gc.is_quiet(datetime(2026, 7, 15, 12, 0), windows, self.central))

    def test_next_change_and_next_start(self):
        windows = self.windows(window('daily', '09:00', '10:00'), window('daily', '10:00', '10:30'),
                               window('daily', '10:15', '11:00'), window('daily', '14:00', '15:00'))
        at = lambda h, m, d=23: cdt(2026, 9, d, h, m)
        # Quiet now: the end of the whole touching/overlapping run, not the first window's end.
        self.assertEqual(gc.next_change(at(9, 30), windows, self.central), at(11, 0))
        self.assertEqual(gc.next_change(at(10, 20), windows, self.central), at(11, 0))
        # Clear now: the next start.
        self.assertEqual(gc.next_change(at(8, 0), windows, self.central), at(9, 0))
        self.assertEqual(gc.next_change(at(12, 0), windows, self.central), at(14, 0))
        self.assertEqual(gc.next_change(at(15, 0), windows, self.central), at(9, 0, 24))
        self.assertEqual(gc.next_start(at(9, 30), windows, self.central), at(10, 0))
        self.assertEqual(gc.next_start(at(8, 0), windows, self.central), at(9, 0))
        self.assertIsNone(gc.next_change(at(8, 0), [], self.central))
        self.assertIsNone(gc.next_start(at(8, 0), [], self.central))

    def test_default_clock_is_central_time(self):
        windows = self.windows(window('daily', '11:30', '12:05'))
        # Whatever the machine's clock, 11:40 CDT in July is 16:40 UTC and must read as quiet.
        self.assertTrue(gc.is_quiet(datetime(2026, 7, 15, 16, 40, tzinfo=timezone.utc).timestamp(), windows))
        self.assertFalse(gc.is_quiet(datetime(2026, 7, 15, 17, 40, tzinfo=timezone.utc).timestamp(), windows))

    def test_example_calendar_loads_and_matches_todays_worker_windows(self):
        root = __import__('pathlib').Path(gc.__file__).resolve().parents[1]
        windows = gc.load_calendar(root / 'crew' / 'gpu-calendar.example.json')
        for name in ('local-a.json', 'local-b.json'):
            config = json.loads((root / 'crew' / name).read_text(encoding='utf-8'))
            own = [(w['days'], w['start'], w['end'], w['label']) for w in config['quiet_windows']]
            seeded = [(w['days'], w['start'], w['end'], w['label'])
                      for w in json.loads((root / 'crew' / 'gpu-calendar.example.json').read_text(encoding='utf-8'))['windows']]
            self.assertEqual(seeded, own, name)
        self.assertEqual(len(windows), 5)
        self.assertTrue(all(w.owner in gc.OWNERS for w in windows))

    def test_module_is_offline_and_read_only(self):
        source = (__import__('pathlib').Path(gc.__file__)).read_text(encoding='utf-8')
        for forbidden in ['import subprocess', 'socket', 'urllib', 'http', 'write_', 'mkdir', 'os.system', 'eval(', 'exec(']:
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
