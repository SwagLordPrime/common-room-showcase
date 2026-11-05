"""The shared GPU calendar: one small JSON file saying who owns the GPU when.

A reader (the Common Room's local workers today, BATCH later) loads the file, asks whether
the GPU is quiet for it at a moment, and stays off the GPU while it is. Format and meaning are
written down in GPU_CALENDAR.md at the package root. Standard library only; this module reads
one file and never writes, creates folders or touches the network.

    {"protocol": "gpu-calendar/1",
     "windows": [{"days": "weekdays", "start": "11:30", "end": "12:05",
                  "owner": "shane", "label": "GPU desk (midday)"}]}

Times are 24-hour HH:MM on the PC's local clock, America/Chicago. A window whose end is before
its start crosses midnight: it starts on each listed day and ends the next morning. Every
function takes `now` as epoch seconds (or a datetime: aware ones are exact, naive ones are read
as Central wall time) and an optional `central` clock with local(epoch) and epoch(wall), the
shape of ollama_worker.CentralTime (which is what is used when none is given).
"""
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path
import json, re

PROTOCOL = 'gpu-calendar/1'
OWNERS = ('batch', 'common-room', 'shane')
MAX_BYTES = 64 * 1024
MAX_WINDOWS = 64
LABEL_LIMIT = 60
DAY_NAMES = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
DAY_SETS = {'weekdays': frozenset(range(5)), 'weekends': frozenset({5, 6}), 'daily': frozenset(range(7))}
TOP_KEYS = frozenset({'protocol', 'windows', 'updated', 'note'})
WINDOW_KEYS = frozenset({'days', 'start', 'end', 'owner', 'label'})
SCAN_DAYS = range(-1, 9)           # a window may have started yesterday; a week ahead is a full cycle

Window = namedtuple('Window', 'days start end owner label')      # start and end are minutes after midnight
Interval = namedtuple('Interval', 'start end label owner')       # epoch seconds, end exclusive


class CalendarError(ValueError):
    """The calendar cannot be used. str(error) is the plain-words reason a reader reports."""


def _text(value, limit):
    value = ''.join(c for c in str(value) if c >= ' ' and c != '\x7f')
    return value if len(value) <= limit else value[:limit - 3] + '...'


def _minutes(value, what):
    match = re.fullmatch(r'([01][0-9]|2[0-3]):([0-5][0-9])', value) if type(value) is str else None
    if not match:
        raise CalendarError('%s must be HH:MM, 24-hour, US Central (for example "08:20").' % what)
    return int(match[1]) * 60 + int(match[2])


def _days(value, where):
    if type(value) is str:
        text = value.strip().lower()
        if text in DAY_SETS:
            return DAY_SETS[text]
        names = [part.strip() for part in text.split(',')]
        if names and all(name in DAY_NAMES for name in names):
            return frozenset(DAY_NAMES[name] for name in names)
    elif type(value) is list and value and all(type(d) is int and 0 <= d <= 6 for d in value):
        return frozenset(value)
    raise CalendarError('%s: days must be weekdays, weekends, daily or day names like "mon,wed,fri".' % where)


def parse_calendar(value):
    """The decoded JSON object as a list of Window, or CalendarError."""
    if type(value) is not dict:
        raise CalendarError('The calendar must be a JSON object.')
    unknown = set(value) - TOP_KEYS
    if unknown:
        raise CalendarError('Unknown calendar keys: %s.' % ', '.join(sorted(_text(k, 40) for k in unknown)))
    if value.get('protocol') != PROTOCOL:
        raise CalendarError('The calendar protocol must be "%s".' % PROTOCOL)
    rows = value.get('windows')
    if type(rows) is not list or len(rows) > MAX_WINDOWS:
        raise CalendarError('"windows" must be a list of at most %d windows.' % MAX_WINDOWS)
    windows = []
    for index, row in enumerate(rows, 1):
        where = 'Window %d' % index
        if type(row) is not dict or not {'days', 'start', 'end', 'owner'} <= set(row) or set(row) - WINDOW_KEYS:
            raise CalendarError('%s needs days, start, end and owner (and may have a label).' % where)
        if row['owner'] not in OWNERS:
            raise CalendarError('%s: owner must be one of %s.' % (where, ', '.join(OWNERS)))
        start = _minutes(row['start'], where + ' start')
        end = _minutes(row['end'], where + ' end')
        if start == end:
            raise CalendarError('%s: start and end must differ (an end before the start crosses midnight).' % where)
        label = row.get('label', row['start'] + '-' + row['end'])
        if type(label) is not str:
            raise CalendarError('%s: label must be text.' % where)
        windows.append(Window(_days(row['days'], where), start, end, row['owner'], _text(label, LABEL_LIMIT)))
    return windows


def load_calendar(path):
    """Read and check the calendar file. Any problem (missing, unreadable, too big, not JSON,
    wrong shape) raises CalendarError carrying the reason; there is no half-loaded result."""
    path = Path(path)
    try:
        info = path.stat()
        if not path.is_file():
            raise CalendarError('The calendar path is not a file: %s' % _text(path, 200))
        if info.st_size > MAX_BYTES:
            raise CalendarError('The calendar file is larger than %d KB.' % (MAX_BYTES // 1024))
        data = path.read_bytes()
    except FileNotFoundError:
        raise CalendarError('The calendar file was not found: %s' % _text(path, 200)) from None
    except OSError as exc:
        raise CalendarError('The calendar file could not be read (%s): %s' % (type(exc).__name__, _text(path, 200))) from None
    try:
        value = json.loads(data.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError, RecursionError):      # RecursionError: absurdly nested brackets
        raise CalendarError('The calendar file is not valid UTF-8 JSON.') from None
    return parse_calendar(value)


def _central(central):
    if central is None:
        from .ollama_worker import CentralTime       # imported late: ollama_worker imports this module
        central = CentralTime()
    return central


def _epoch(now, central):
    if isinstance(now, datetime):
        return now.timestamp() if now.tzinfo is not None else central.epoch(now)
    return float(now)


def intervals(now, windows, central=None):
    """Every quiet interval near `now` (yesterday through the next week), sorted by start."""
    central = _central(central)
    today = central.local(_epoch(now, central)).date()
    found = []
    for offset in SCAN_DAYS:
        day = today + timedelta(days=offset)
        midnight = datetime(day.year, day.month, day.day)
        for w in windows:
            if day.weekday() in w.days:
                over = 0 if w.start < w.end else 1
                found.append(Interval(central.epoch(midnight + timedelta(minutes=w.start)),
                                      central.epoch(midnight + timedelta(days=over, minutes=w.end)),
                                      w.label, w.owner))
    return sorted(found)


def quiet_window(now, windows, span=0, central=None):
    """The first interval that [now, now+span] touches, or None. span 0 asks about this instant."""
    central = _central(central)
    at = _epoch(now, central)
    for item in intervals(at, windows, central):
        if item.start <= at + span and item.end > at:
            return item
    return None


def is_quiet(now, windows, central=None):
    return quiet_window(now, windows, 0, central) is not None


def next_start(now, windows, central=None):
    """Epoch of the next interval start after `now`, or None."""
    central = _central(central)
    at = _epoch(now, central)
    starts = [i.start for i in intervals(at, windows, central) if i.start > at]
    return min(starts) if starts else None


def next_change(now, windows, central=None):
    """Epoch when quiet flips: the end of the unbroken quiet stretch holding `now` (touching or
    overlapping windows count as one stretch), else the next start. None when nothing changes
    within the scanned week."""
    central = _central(central)
    at = _epoch(now, central)
    items = intervals(at, windows, central)
    end = None
    for item in items:
        if item.start <= at < item.end or (end is not None and item.start <= end):
            end = item.end if end is None else max(end, item.end)
    if end is not None:
        return end
    starts = [i.start for i in items if i.start > at]
    return min(starts) if starts else None
