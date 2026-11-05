"""Shared participant client for shell-capable crew: the crew CLI, the local-model
worker and the file relay all use this one path.

It reads only the caller's own credential file, re-reads that file when the room
restarts and rotates keys, and never invents authority: every write still goes
through the room's own /api/command or /api/handoff under a caller-kept request ID,
so a retry after a lost response is answered from the room's stored receipt.
"""
from pathlib import Path
import re, time, uuid
from .participant import Participant
from .protocol import decode_request
from .keeper_vault import STUB_REFUSAL, is_stub

ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
CREDENTIAL_FIELDS = {'origin', 'token', 'actor_id', 'expires_at'}


class CrewError(ValueError):
    """A refusal with a stable code, e.g. 'unauthenticated', 'lease', 'forbidden'."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def split_error(exc):
    """Participant errors arrive as 'code: message'; connection loss has no code."""
    text = str(exc)
    head, sep, tail = text.partition(': ')
    if sep and re.fullmatch(r'[a-z_]{1,40}', head):
        return head, tail
    if text.startswith('Local room unavailable'):
        return 'unavailable', text
    return 'error', text


def check_credential_path(path, root=ROOT):
    """Same boundary the participant CLI enforces: own package runtime/checks, no links."""
    root = Path(root).resolve()
    credential = Path(path).absolute()
    if not any(credential.resolve().is_relative_to((root / n).resolve()) for n in ('.runtime', '.checks')):
        raise CrewError('config', 'Credential must be in this package runtime or checks directory.')
    for part in [credential, *credential.parents]:
        if part == root or not part.is_relative_to(root):
            break
        if part.is_symlink() or part.is_junction():
            raise CrewError('config', 'Linked credential paths refused.')
    if not credential.name.endswith('-credential.json'):
        raise CrewError('config', 'Credential files are named <actor>-credential.json.')
    return credential


def load_credential(path, root=ROOT):
    credential = check_credential_path(path, root)
    try:
        if credential.stat().st_size > 4096:
            raise CrewError('config', 'Oversized credential file.')
        config = decode_request(credential.read_bytes())
    except OSError as exc:
        raise CrewError('config', 'Credential file is missing or unreadable; ask Shane to start the room with your seat selected.') from exc
    except ValueError as exc:
        raise CrewError('config', 'Credential file is not valid JSON.') from exc
    if is_stub(config):
        # The owner key is in the vault; only `crew --as keeper` (crew-shane) reads it there.
        raise CrewError('config', STUB_REFUSAL)
    if set(config) != CREDENTIAL_FIELDS or type(config['actor_id']) is not str:
        raise CrewError('config', 'Invalid credential file.')
    if credential.name != config['actor_id'] + '-credential.json':
        raise CrewError('config', 'Credential file name does not match its actor.')
    return config


def new_request_id(prefix='crew'):
    value = re.sub(r'[^A-Za-z0-9_.:-]', '-', str(prefix))[:80].strip('-') or 'crew'
    value = value + '-' + uuid.uuid4().hex[:20]
    if not REQUEST_ID.fullmatch(value):
        value = 'crew-' + uuid.uuid4().hex
    return value


class RenewIds:
    """Request ids for a worker's lease renewals (S0 F1, 2026-10-15). They used to be numbered from the task_renew rows
    in /api/state history, which S0's bounded state no longer lists, so every renewal at a fence reused one id and the
    room replayed it. Now each renewal gets a fresh id. Only a renewal whose reply was lost keeps its id, so the retry is
    the same request and the room answers it from its receipt; once a reply arrives the id is done with."""
    def __init__(self):
        self.pending = {}

    def renew(self, client, task_id, fence, seconds):
        key = (task_id, fence)
        request_id = self.pending.get(key) or new_request_id('renew-%s-f%d' % (task_id, fence))
        self.pending = {key: request_id}       # one lease at a time: an older task or fence is never retried
        result = client.command('renew', {'task_id': task_id, 'fence': fence, 'seconds': seconds}, request_id)
        self.pending.pop(key, None)
        return request_id, result


class CrewClient:
    """One actor's connection. Re-reads its credential after a key rotation."""

    def __init__(self, credential_path, root=ROOT, expected_actor=None, sleep=time.sleep,
                 reconnect_attempts=3, reconnect_delay=2.0, loader=None):
        self.path = Path(credential_path)
        self.root = Path(root)
        self.expected_actor = expected_actor
        self.sleep = sleep
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self.loader = loader    # keeper only: keeper_vault.keeper_loader, which reads the vault behind the stub
        self.actor_id = None
        self._participant = None

    def connect(self):
        config = self.loader() if self.loader is not None else load_credential(self.path, self.root)
        if self.expected_actor and config['actor_id'] != self.expected_actor:
            raise CrewError('identity', 'Credential names a different actor; refusing to act as it.')
        participant = Participant(config['origin'], config['token'])
        self._drop_session()    # a session being replaced is logged out first, so it never lingers in the room's table
        try:
            identity = participant.login()
        except ValueError as exc:
            raise CrewError(*split_error(exc)) from exc
        if identity.get('actor') != config['actor_id']:
            raise CrewError('identity', 'Credential identity mismatch.')
        if self.actor_id is not None and identity['actor'] != self.actor_id:
            raise CrewError('identity', 'Credential file now names a different actor.')
        self.actor_id = identity['actor']
        self.expected_actor = self.expected_actor or self.actor_id
        self._participant = participant
        return self.actor_id

    def _drop_session(self):
        """Forget the current session and log it out of the room (best effort, never raises). The room keeps
        every login for up to 900 s after its last use and refuses all logins at 256 live sessions (board.py),
        so a session that is replaced or finished must be logged out, not left to age out. The room's logout
        route is called through the participant's own request method (participant.py is not part of this
        change); a dead session simply answers unauthenticated, which is ignored."""
        participant, self._participant = self._participant, None
        if participant is None or not participant.cookie:
            return
        try:
            participant._request('/api/logout', {})
        except Exception:       # room down, session already expired or keys rotated: nothing more to clean up from here
            pass

    def logout(self):
        """End this client's session (best effort, idempotent, never raises). The next call connects afresh."""
        self._drop_session()

    def _call(self, operation):
        if self._participant is None:
            self.connect()
        try:
            return operation(self._participant)
        except ValueError as exc:
            code, message = split_error(exc)
            if code not in {'unauthenticated', 'unavailable'}:
                raise CrewError(code, message) from exc
            last = CrewError(code, message)
        # Room restarted or keys rotated: re-read our own credential and retry the SAME call.
        # Writes keep their request ID, so a retry is answered from the stored receipt.
        for attempt in range(self.reconnect_attempts):
            if attempt:
                self.sleep(self.reconnect_delay)
            try:
                self.connect()
                return operation(self._participant)
            except CrewError as exc:
                last = exc
                if exc.code not in {'unauthenticated', 'unavailable', 'config'}:
                    raise
            except ValueError as exc:
                code, message = split_error(exc)
                last = CrewError(code, message)
                if code not in {'unauthenticated', 'unavailable'}:
                    raise last from exc
        raise last

    def state(self):
        return self._call(lambda p: p.state())

    def watch_state(self):
        """One poll for `crew watch`: /api/state over the ONE session this client holds (it connects on first use).
        The room answering unauthenticated (session expired, keys rotated, room restarted) gets exactly ONE reconnect,
        which logs the dead session out and re-reads this seat's own credential file. Anything else, a room that is
        down included, is raised at once for the watch loop's own back-off to pace: _call's three reconnect attempts
        with 2 s pauses suit a write that must get through, but would stretch a poll against a down room from 2 s to
        14 s (measured) and let a 590 s watch outlive its 600 s tool window."""
        if self._participant is None:
            self.connect()
        try:
            return self._participant.state()
        except ValueError as exc:
            code, message = split_error(exc)
            if code != 'unauthenticated':
                raise CrewError(code, message) from exc
        self.connect()
        try:
            return self._participant.state()
        except ValueError as exc:
            raise CrewError(*split_error(exc)) from exc

    def command(self, action, payload, request_id):
        if type(request_id) is not str or not REQUEST_ID.fullmatch(request_id):
            raise CrewError('invalid', 'Request IDs are 1-128 letters, digits, _ . : or -.')
        return self._call(lambda p: p.command(action, payload, request_id))

    def handoff(self, operation, payload):
        return self._call(lambda p: p.handoff(operation, payload))

    def check_keys(self, value):
        """fix2-board (VERIFY8A N3): ask the room whether `value` holds any room key (owner only; the
        room writes nothing): 'forbidden' when it does. plans.refuse_plan_keys runs it on a whole plan."""
        try:
            return self._call(lambda p: p.key_check(value))
        except CrewError as exc:
            if exc.code == 'not_found':
                raise CrewError('config', 'This room is older than this crew tool and cannot check a plan for room '
                                          'keys; restart the room, then approve again. Nothing was sent.') from exc
            raise

    # Convenience views over one /api/state snapshot.
    @staticmethod
    def task_in(state, task_id):
        for task in state.get('tasks', []):
            if task['id'] == task_id:
                return task
        raise CrewError('forbidden', 'Task is not visible to this participant.')

    def my_queue(self, state, statuses=('delivered', 'acknowledged', 'claimed')):
        return [t for t in state.get('tasks', []) if t['recipient'] == self.actor_id and t['status'] in statuses]

    def review_queue(self, state):
        return [t for t in state.get('tasks', []) if t['status'] == 'ready_for_review' and t.get('builder') != self.actor_id]
