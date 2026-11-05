"""Phone push when something lands on the Needs Shane list (plan turnons-2026-10-03, job notify).

Ships OFF. Nothing happens until Shane writes crew\\notify.json himself (crew\\notify.example.json shows the
shape); with no file, this prints one line and exits 0. "Start Common Room Crew.cmd" opens its window only
when that file exists.

It runs as the room seat `notify`, role observer, a member of `orchestra` only (claude-room ruling #1181: the
phone covers the orchestra work; the board shows everything). An observer can read the room and the room
refuses every command it sends (store.py: "Observers cannot post commands."); this module sends none anyway.

Each cycle it reads the room's state as that seat, asks common_room.needs.needs_shane(snapshot) for the list
of {id, kind, since, summary, action}, and pushes only ids it has never pushed before: at most one push per
batch window, none during quiet hours (US Central), de-duplicated across restarts through a small state file
under .runtime\\notify\\. A push carries a count and up to three summaries, cleaned again here: never an
action line, a path, a key or file contents. The ntfy topic is a secret: it is never printed, logged, put in
an error message or written anywhere but Shane's own notify.json.

The one network call in the room code: an HTTPS POST to the ntfy server named in notify.json (RUN_RULES
2026-10-01, Safety). Standard library only.
"""
from pathlib import Path
from urllib.parse import urlsplit
import argparse, json, os, re, sys, time, urllib.error, urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'crew' / 'notify.json'
CREDENTIAL = ROOT / '.runtime' / 'handoffs' / 'notify-credential.json'
STATE_DIR = ROOT / '.runtime' / 'notify'
STATE_NAME = 'state.json'
SEAT = 'notify'
TITLE = 'Common Room needs you'
CLOSING = 'Answer in claude-room or reply to the email.'
OFF_LINE = 'notify off: no crew\\notify.json'
DEFAULT_SERVER = 'https://ntfy.sh'
DEFAULT_BATCH_MINUTES = 10
CONFIG_KEYS = {'server', 'topic', 'quiet_hours', 'batch_minutes'}
TOPIC = re.compile(r'[A-Za-z0-9_-]{1,64}')
PLACEHOLDER = 'CHANGE-ME'
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')   # plain http only to a server on this PC (a local ntfy, or a test)
QUIET = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])')
POLL_SECONDS = 60
MAX_SUMMARIES = 3
SUMMARY_LIMIT = 140
SEEN_SECONDS = 14 * 86400          # a pushed id is remembered this long after it left the list
POST_TIMEOUT = 15
PATHISH = re.compile(r'[\\/]|^[A-Za-z]:')
TOKENISH = re.compile(r'[A-Za-z0-9_\-]{24,}')
HEX = re.compile(r'[0-9a-f]+')


class ConfigError(ValueError):
    """notify.json is present but not usable. Messages never contain the topic."""


def load_config(path=CONFIG):
    """None when the file is missing (notify is off); otherwise the checked settings."""
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError('it could not be read (' + type(exc).__name__ + ')')
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError('it is not valid JSON')
    if type(data) is not dict:
        raise ConfigError('it must be one JSON object')
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise ConfigError('unknown setting(s): ' + ', '.join(unknown))
    server = data.get('server', DEFAULT_SERVER)
    if type(server) is not str:
        raise ConfigError('server must be text like https://ntfy.sh')
    parts = urlsplit(server)
    if (parts.scheme not in ('https', 'http') or not parts.hostname or parts.path not in ('', '/') or parts.query
            or parts.fragment or parts.username or parts.password
            or (parts.scheme == 'http' and parts.hostname not in LOCAL_HOSTS)):
        raise ConfigError('server must be https://<host>, with no path (plain http only for this PC)')
    topic = data.get('topic')
    if type(topic) is not str or not TOPIC.fullmatch(topic):
        raise ConfigError('topic must be 1-64 letters, digits, - or _')
    if topic == PLACEHOLDER:
        raise ConfigError('topic is still the example value; put your own private topic in')
    quiet = data.get('quiet_hours')
    if quiet is not None:
        match = QUIET.fullmatch(quiet) if type(quiet) is str else None
        if not match:
            raise ConfigError('quiet_hours must look like 23:00-08:00 (24-hour, US Central)')
        h1, m1, h2, m2 = (int(x) for x in match.groups())
        quiet = (h1 * 60 + m1, h2 * 60 + m2)
        if quiet[0] == quiet[1]:
            raise ConfigError('quiet_hours must start and end at different times')
    batch = data.get('batch_minutes', DEFAULT_BATCH_MINUTES)
    if type(batch) is not int or not 1 <= batch <= 1440:
        raise ConfigError('batch_minutes must be a whole number from 1 to 1440')
    return {'server': server.rstrip('/'), 'topic': topic, 'quiet_hours': quiet, 'batch_minutes': batch}


def in_quiet_hours(quiet, wall):
    """True when the US Central wall time `wall` (a naive datetime) falls inside quiet (start, end) minutes."""
    if quiet is None:
        return False
    minute = wall.hour * 60 + wall.minute
    start, end = quiet
    return start <= minute < end if start < end else (minute >= start or minute < end)


def clean(text, limit=SUMMARY_LIMIT):
    """One summary line, safe for a phone: anything path-like or key-like is hidden, then it is cut."""
    words = []
    for word in str(text or '').split():
        if PATHISH.search(word):
            words.append('[path]')
        else:
            words.append(TOKENISH.sub(lambda m: m.group(0) if HEX.fullmatch(m.group(0)) else '[hidden]', word))
    line = ' '.join(words)
    return line if len(line) <= limit else line[:limit - 3].rstrip() + '...'


def message(items):
    """The push body: a count and up to three summaries, then where to answer. Never an action line."""
    count = len(items)
    lines = [str(count) + (' thing needs you:' if count == 1 else ' things need you:')]
    lines += ['- ' + clean(item.get('summary')) for item in items[:MAX_SUMMARIES]]
    if count > MAX_SUMMARIES:
        lines.append('...and ' + str(count - MAX_SUMMARIES) + ' more on the board.')
    lines.append(CLOSING)
    return '\n'.join(lines)


def post_ntfy(server, topic, title, body, timeout=POST_TIMEOUT):
    """(ok, short reason). The reason is a status code or an error type name, never the URL or the topic."""
    request = urllib.request.Request(server + '/' + topic, data=body.encode('utf-8'), method='POST',
                                     headers={'Title': title, 'Content-Type': 'text/plain; charset=utf-8'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        return False, 'HTTP ' + str(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, type(exc).__name__
    return 200 <= status < 300, 'HTTP ' + str(status)


def empty_state():
    return {'version': 1, 'seen': {}, 'pending': [], 'last_push': None}


def load_state(state_dir):
    """The saved state, or a fresh one. A damaged file is set aside (renamed .bad) rather than trusted."""
    path = Path(state_dir) / STATE_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return empty_state(), None
    except (OSError, ValueError):
        data = None
    if (type(data) is not dict or data.get('version') != 1 or type(data.get('seen')) is not dict
            or type(data.get('pending')) is not list or not (data.get('last_push') is None or type(data['last_push']) in (int, float))):
        try:
            os.replace(path, path.with_name(STATE_NAME + '.bad'))
        except OSError:
            pass
        return empty_state(), 'notify: the saved state was damaged; starting fresh (items now listed may be pushed again).'
    return data, None


def save_state(state_dir, state):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    temp = state_dir / (STATE_NAME + '.tmp')
    temp.write_text(json.dumps(state, sort_keys=True), encoding='utf-8')
    os.replace(temp, state_dir / STATE_NAME)


class Notifier:
    """One cycle at a time: read the room, remember new ids, push when the window and the hour allow."""

    def __init__(self, config, client, needs, state_dir=STATE_DIR, clock=time.time, central=None, post=post_ntfy,
                 log=print):
        if central is None:
            from .ollama_worker import CentralTime
            central = CentralTime()
        self.config, self.client, self.needs, self.state_dir = config, client, needs, Path(state_dir)
        self.clock, self.central, self.post, self.log = clock, central, post, log

    def cycle(self):
        snapshot = self.client.state()
        items = self.needs(snapshot)
        now = self.clock()
        current = {}
        for item in items:
            current.setdefault(item['id'], item)
        state, warning = load_state(self.state_dir)
        if warning:
            self.log(warning)
        seen, pending = state['seen'], [p for p in state['pending'] if p in current]   # gone before a push: dropped
        new = [i for i in current if i not in seen]
        for item_id in new:
            seen[item_id] = now
            pending.append(item_id)
        for item_id in list(seen):
            if item_id not in current and now - seen[item_id] > SEEN_SECONDS:
                del seen[item_id]
        if new:
            self.log('notify: ' + str(len(new)) + ' new item(s) on the Needs Shane list.')
        result = {'new': len(new), 'pending': len(pending), 'pushed': 0, 'held': None}
        if pending:
            last = state['last_push']
            if in_quiet_hours(self.config['quiet_hours'], self.central.local(now)):
                result['held'] = 'quiet hours'
            elif last is not None and now - last < self.config['batch_minutes'] * 60:
                result['held'] = 'batch window'
            else:
                ok, reason = self.post(self.config['server'], self.config['topic'], TITLE,
                                       message([current[i] for i in pending]))
                if ok:
                    self.log('notify: pushed ' + str(len(pending)) + ' item(s).')
                    result['pushed'], state['last_push'], pending = len(pending), now, []
                else:
                    self.log('notify: push failed (' + reason + '); trying again next cycle.')
                    result['held'] = 'push failed'
        state['seen'], state['pending'] = seen, pending
        save_state(self.state_dir, state)
        result['pending'] = len(pending)
        return result


def main(argv=None, sleep=time.sleep, out=print):
    p = argparse.ArgumentParser(description='Phone push for the Needs Shane list (ships off: needs crew\\notify.json).')
    p.add_argument('--config', type=Path, default=CONFIG)
    p.add_argument('--credential', type=Path, default=CREDENTIAL, help='The notify seat credential (observer).')
    p.add_argument('--state-dir', type=Path, default=STATE_DIR)
    p.add_argument('--loop', action='store_true', help='Keep running, one cycle every --poll seconds.')
    p.add_argument('--poll', type=int, default=POLL_SECONDS)
    a = p.parse_args(argv)
    try:
        config = load_config(a.config)
    except ConfigError as exc:
        out('notify: crew\\notify.json is not usable: ' + str(exc) + '. Nothing was sent.')
        return 2
    if config is None:
        out(OFF_LINE)
        return 0
    try:
        from .needs import needs_shane
    except ImportError:
        out('notify: common_room/needs.py is not in this tree yet (the panels job), so there is nothing to read.')
        return 2
    try:    # S0 F4: the list the room worked out before it clipped history details; needs_shane() where a tree lacks it
        from .needs import room_needs
    except ImportError:
        room_needs = needs_shane
    from .crew_client import CrewClient, CrewError
    client = CrewClient(a.credential, expected_actor=SEAT)
    notifier = Notifier(config, client, room_needs, a.state_dir, log=out)
    off = False
    while True:
        try:
            current = load_config(a.config)
        except ConfigError as exc:
            out('notify: crew\\notify.json is not usable: ' + str(exc) + '. Nothing was sent.')
            return 2
        if current is None:
            if not a.loop:
                out(OFF_LINE)
                return 0
            if not off:
                out(OFF_LINE + ' (it was removed; waiting quietly until it is back)')
            off = True
        else:
            off = False
            notifier.config = current
            try:
                notifier.cycle()
            except CrewError as exc:
                out('notify: the room did not answer (' + exc.code + '); trying again next cycle.')
        if not a.loop:
            return 0
        sleep(max(5, a.poll))


if __name__ == '__main__':
    sys.exit(main())
