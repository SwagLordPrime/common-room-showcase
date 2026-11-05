"""File relay: lets a Cowork chat act as one named Common Room seat through files.

A Cowork chat can read and write files but cannot reach the Windows loopback room.
This relay runs on the room's own host, started next to the room. For each named
actor it watches a `requests/` folder, performs each request through the room's
own `/api/command` or `/api/handoff` using that actor's own credential (via
CrewClient, so it never invents authority), and writes back a response plus a
fresh scoped `relay-state.json`. It adds no authority beyond what the actor's own
credential already grants; it only removes the need for a shared browser tab.

Nothing here parses model text as instructions, touches the room's SQLite store
directly, or acts as `keeper`. A malformed or disallowed request produces an
error response file; it is never sent to the room. One bad request never stops
another request, and one actor's trouble never stops another actor.

A chat can replace any folder in its own relay folder with a link at any moment.
The relay therefore pins the folder chain by identity, re-checks it immediately
before every read and write, uses directory handles for those operations where the
platform has them (POSIX), never moves, renames or deletes a request, and only ever
writes files with relay-only names (`resp-*.json`, `relay-state.json`). See the
comment block above `_pin` (gate R, third attempt, 2026-09-24).
"""
from pathlib import Path
import argparse, hashlib, json, os, re, stat, sys, time, uuid

from .activation import safe_path
from .crew_client import CrewClient, CrewError
from .protocol import RoomError, canonical, decode_request
from .store import FIELDS
from . import chat as chat_rules
from .plans import resolve_task_id

ACTOR_NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')
# Request filenames must end in .json and use only safe, Windows-legal characters.
REQUEST_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.json')
# Relay request ids are stricter than the room's own idempotency-key pattern: no
# ':' (illegal in a Windows filename) and capped well under MAX_PATH.
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
RESERVED_STEMS = {'CON', 'PRN', 'AUX', 'NUL'} | {p + str(i) for p in ('COM', 'LPT') for i in range(1, 10)}
MAX_REQUEST_BYTES = 16384
PRESCAN_LIMIT = 65536
MAX_PER_CYCLE = 20
RETRY = object()   # A4: _handle_request's "leave this request for the next cycle" (the room was unreachable)
# A4: how long a command or handoff request that cannot reach the room waits before it is answered "unavailable".
# A renewal's restart is seconds (start_room retries a busy port 5 x 2 s); join cards tell a chat to wait 90 s.
RETRY_SECONDS = 60.0
RESPONSE_PREFIX = 'resp-'
STATE_NAME = 'relay-state.json'
# Every file the relay writes during a cycle carries one of these names (N18 defense in depth).
RELAY_OWNED = re.compile(r'(resp-[A-Za-z0-9_.-]{1,100}\.json|relay-state\.json)')
# Directory-handle file operations: POSIX yes, Windows no (see _pin).
# (os.replace takes src_dir_fd/dst_dir_fd wherever os.rename does; os.listdir takes a directory fd
# wherever os.supports_fd lists it.)
DIR_FD = (os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd and os.stat in os.supports_dir_fd
          and os.unlink in os.supports_dir_fd and os.listdir in os.supports_fd
          and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'))

# resolve_decision and cancel pass through: the room lets the owner, or the sender of a helper
# task, use them (2026-09-24 orchestra build), and refuses everyone else itself.
COMMAND_ACTIONS = {'ack', 'claim', 'renew', 'finish', 'fail', 'wait_decision', 'review', 'note', 'request_help',
                   'resolve_decision', 'cancel', 'question_mark'}   # question_mark: A1, 2026-10-24
OWNER_ACTIONS = {'intake', 'deliver', 'assign', 'reinstate', 'pause', 'resume', 'revoke', 'grant', 'grant_handoff',
                 'stop_all', 'resume_all'}  # the last two (core-stop) are the keeper's room-wide switch
HANDOFF_OPERATIONS = {'stage_text', 'view_text', 'receipt', 'task_view'}
assert COMMAND_ACTIONS | OWNER_ACTIONS == set(FIELDS)

README_TEMPLATE = """# Relay folder for {actor}

This folder lets a Cowork chat act as the Common Room seat "{actor}" without
reaching the Windows room directly. A relay process on Shane's PC watches
`requests/` in this folder, performs each request through the room using this
seat's own credential, and writes the answer into `responses/`. It also keeps
`relay-state.json` here: this seat's own scoped view of the room.

The easiest way to use it is the helper, which does all of the below for you:

    python3 <tree>/crew/relay_ask.py <this folder> state
    python3 <tree>/crew/relay_ask.py <this folder> '<request json>'

With the helper, give `"request_id": "auto"` and it makes a fresh id, never one
another chat on this seat used, and prints it. Written by hand, "auto" is just an
id like any other, so choose your own.

## Writing a request

Write a small JSON file to a temporary name inside `requests/`, then rename it
to its final name (the relay ignores names ending in `.tmp`). Final names end in
`.json` and use only letters, digits, `_`, `.` and `-` (at most 101 characters,
not a Windows device name such as `CON` or `NUL`). At most 16 KiB. The
`request_id` inside: 1-64 letters, digits, `_`, `.` or `-` (no `:`).

    {{"request_id": "claim-1", "kind": "command", "action": "claim",
      "payload": {{"task_id": "t...", "seconds": 1800}}}}
        actions: ack, claim, renew, finish, fail, wait_decision, review, note,
        request_help, question_mark, resolve_decision and cancel (the room decides
        who may; today that is the owner, or the sender of a helper task). intake,
        deliver, assign, reinstate, pause, resume, revoke, stop_all, resume_all and grants are refused
        here before anything is sent.
    {{"request_id": "stage-1", "kind": "handoff", "operation": "stage_text",
      "payload": {{"task_id": "t...", "fence": 2, "text": "..."}}}}
        operations: stage_text, view_text, receipt, task_view (full task and events).
    {{"request_id": "state-1", "kind": "state"}}

Task references may be a full id or a unique visible prefix of 6 or more characters.
Use request_help only for a different worker in the project, never your own seat.

Text caps are characters: finish summary, note body, failure explanation and
resolve_decision body 4000; decision question, review note and cancellation reason
2000; room stop/resume reason 500 (owner route only). Helper title 120, body 4000,
acceptance 2000. stage_text allows at most 12000 characters AND 12000 UTF-8 bytes;
the whole JSON request still has the 16 KiB byte budget above.

Existing resolve_decision supports "refund": true, with a nonempty body as the
written reason. The owner or an authorized helper sender may refund one strike,
never below zero; only the owner may refund a task stopped at two strikes.
Owner-role seats cannot use the relay. A successful response's task.refunds and
the saved resolve_decision event contain reason, strikes_before and strikes_after.

## Questions addressed to this seat

A note whose first line starts `QUESTION FOR {actor}:` is a question for this
seat. It appears in `relay-state.json` under `questions` (to = {actor}) with
its marks: delivered, read, accepted, checked. The relay keeping it there is
only "delivered". Your own `state` request is you picking it up: it marks each
unread question to this seat "read" and lists them in its answer as
`questions_read`. Take a question up with
`{{"kind": "command", "action": "question_mark", "payload": {{"note_seq": N,
"mark": "accepted"}}}}` and answer in a note citing #N; the asker marks it
"checked" the same way once your answer settles it. Questions this seat asked
are under `asked`, with their marks. Marks wake nobody.

## Reading the answer

The answer appears as `responses/resp-<request_id>.json`: `{{"ok": true, "result":
...}}` or `{{"ok": false, "error": "<code>", "message": "..."}}`, with
`request_file` and `request_sha256` naming exactly which file it answers. A
request the relay could not parse at all is answered as
`responses/resp-invalid-<file name without .json>.json`.

**The relay never moves, renames or deletes your request.** It answers each file
once, even across a relay restart (it checks the answer already on disk). Tidy
your own folder: move answered requests into `archive/` yourself (the helper
does). To try a request again by hand, write it under a **new file name**, or
first rename the old `responses/resp-<request_id>.json`: the same file name with
the same content counts as already answered and is skipped. Keeping the same
`request_id` is safe: the room answers a repeat of something it already did
from its stored receipt, and simply tries a refused request again.

## When the room renews its keys

Every few hours the room starts a new key cycle on the same port. The relay
re-reads this seat's credential file by itself, so nothing is needed from you.
A request written while the room restarts is answered once it is back (it is
never answered "unavailable" for that). After a renewal, `relay-state.json`
holds `room_renewed`, a short note with the new instance id and expiry and
the command that prints your current join card, until this seat's next
command. Answers themselves never change.

## This is still the room

An answer is a record of what the room did or refused. Its rules apply in full:
two strikes stop a task, and the builder of a task can never review it. If a
folder here stops being an ordinary folder, the relay refuses to touch it.
"""


class RelayError(Exception):
    """A startup or configuration refusal. The CLI turns this into exit(2)."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


class RelayMalformed(Exception):
    """A request file this relay refuses to interpret at all; never sent to the room."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


class RelayFault(Exception):
    """An operational fault confined to one actor's cycle; never sent to the room,
    never allowed to stop another actor or another request."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _task_payload(client, payload):
    """Expand short references using only this seat's authenticated, visible state.

    Full task IDs pass through unchanged so normal requests and receipt retries
    do not depend on a second state fetch. The room still enforces all authority.
    """
    if 'task_id' not in payload:
        return payload
    ref = payload['task_id']
    if type(ref) is str and len(ref) == 33:
        return payload
    return dict(payload, task_id=resolve_task_id(client.state(), ref))


def _sanitize_filename(name):
    # A relay-facing name (a request_id, or a raw file stem used for an
    # 'invalid-' response) must become a safe, deterministic filename
    # component regardless of what a chat actually wrote on disk: no ':'
    # (illegal on Windows), no other exotic characters, never empty.
    cleaned = re.sub(r'[^A-Za-z0-9_.-]', '-', name)
    cleaned = cleaned.strip('.-')
    return (cleaned or 'file')[:80]


def _atomic_write_text(root, target, content):
    target = safe_path(root, target)
    temporary = safe_path(root, target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp'))
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        safe_path(root, target)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


class Relay:
    """Watches request folders for `actors` and performs them through the room.

    `root` is the room's own root (where `<actor>-credential.json` files and
    `connection.json` live) — the same path passed as `--root` to
    `common_room.activation`. Folders are created under `root/relay/<actor>/`.
    """
    chat = None   # A3: OFF unless __init__ sets it; a subclass that never calls Relay.__init__ (DuetRelay) stays OFF

    def __init__(self, root, actors, clock=time.time, state_every=15.0, chat=None):
        self.root = Path(root).absolute()
        try:
            self.chat = chat_rules.pair_value(chat)   # A3: None (OFF) is exactly the base head's relay
        except ValueError as exc:
            raise RelayError('config', str(exc)) from exc
        self.clock = clock
        self.state_every = state_every
        wanted = self._validate_actors(actors)
        self.relay_root = self._prepare_dir(self.root / 'relay')
        self.folders = {}
        self.clients = {}
        self.last_state_write = {}
        self.handled = {}
        self.retrying = {}   # A4: actor -> {request entry key: first time it met an unreachable room}
        self.actors = []
        # F10: the folder identities seen at startup are the only ones this process will ever use.
        self.startup = {'room': self._dir_identity(self.root), 'relay': self._dir_identity(self.relay_root)}
        for actor in wanted:
            try:
                self.folders[actor] = self._prepare_actor_folder(actor)
                folder = self.folders[actor]
                for key in ('root', 'requests', 'responses'):
                    self.startup[actor + ':' + key] = self._dir_identity(folder[key])
            except (RelayError, RelayFault) as exc:   # F13: one bad seat never stops the others
                self.folders.pop(actor, None)
                self._log(actor, None, None, None, False, 'seat_refused_at_startup')
                print('common_room.relay: seat ' + actor + ' skipped: ' + getattr(exc, 'message', str(exc)),
                      file=sys.stderr, flush=True)
                continue
            self.actors.append(actor)
            self.handled[actor] = set()
            self.retrying[actor] = {}
            self.clients[actor] = CrewClient(self._credential_path(actor), expected_actor=actor)
            self.last_state_write[actor] = 0.0
        if not self.actors:
            raise RelayError('config', 'No seat could be started (see the lines above).')

    # -- startup --------------------------------------------------------

    def _validate_actors(self, actors):
        actors = list(actors)
        if len(set(actors)) != len(actors):
            raise RelayError('config', 'Duplicate actor names in --actors.')
        for name in actors:
            if type(name) is not str or not ACTOR_NAME.fullmatch(name):
                raise RelayError('config', 'Invalid actor name: ' + repr(name))
            if name == 'keeper':
                raise RelayError('config', 'The room owner (keeper) cannot be run through the relay.')
        return actors

    def _prepare_dir(self, path):
        if path.is_symlink() or path.is_junction():
            raise RelayError('config', str(path) + ' must not be a symlink or junction.')
        if path.exists() and not path.is_dir():
            raise RelayError('config', str(path) + ' exists and is not a directory.')
        try:
            path.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RelayError('config', str(path) + ' exists and is not a directory.') from exc
        if path.is_symlink() or path.is_junction():
            raise RelayError('config', str(path) + ' must not be a symlink or junction.')
        return path

    def _prepare_actor_folder(self, actor):
        actor_dir = self._prepare_dir(self.relay_root / actor)
        requests_dir = self._prepare_dir(actor_dir / 'requests')
        responses_dir = self._prepare_dir(actor_dir / 'responses')
        archive_dir = self._prepare_dir(actor_dir / 'archive')
        state_path = actor_dir / STATE_NAME
        readme_path = actor_dir / 'README.md'
        for path in (state_path, readme_path):
            if path.is_symlink() or path.is_junction():
                raise RelayError('config', str(path) + ' must not be a symlink or junction.')
            if path.exists() and path.is_dir():
                raise RelayError('config', str(path) + ' exists and is a directory.')
        readme = README_TEMPLATE.format(actor=actor)
        if self.chat is not None and actor in self.chat['seats']:
            readme += chat_rules.readme_section(self.chat, actor)   # A3: the chat section, ON and the pair only
        _atomic_write_text(actor_dir, readme_path, readme)
        return {'root': actor_dir, 'requests': requests_dir, 'responses': responses_dir,
                'archive': archive_dir, 'state': state_path, 'readme': readme_path}

    def _credential_path(self, actor):
        return self.root / (actor + '-credential.json')

    # -- per-cycle folder pinning (H1, N1, N18) ----------------------------------------------
    #
    # A chat owns its relay folder and can replace any folder in it with a link at any
    # moment, including while this relay waits on the room. Four rules keep every file
    # operation inside the seat's real folders:
    #   1. The folder chain is pinned by identity (st_dev, st_ino) at the start of a cycle
    #      and re-checked immediately before every read and every write.
    #   2. On POSIX every read and write goes through directory handles opened (no link
    #      following) at pin time, so a later swap of a path cannot redirect it at all.
    #      Windows has no directory handles for these calls; there the re-check runs
    #      immediately before the call, never across the room call or a sleep.
    #   3. Request entries are pinned by identity when listed and re-verified on the open
    #      handle (fstat) before a byte is read.
    #   4. The relay never moves, renames or deletes a request, and every file it writes
    #      has a relay-only name (resp-*.json, relay-state.json), so even a race won on
    #      Windows could only create or replace a file with one of those names.

    def _pin(self, actor):
        folder = self.folders[actor]
        chain = [('room', self.root), ('relay', self.relay_root), ('seat', folder['root']),
                 ('requests', folder['requests']), ('responses', folder['responses'])]
        expected = {'room': self.startup['room'], 'relay': self.startup['relay'],
                    'seat': self.startup[actor + ':root'], 'requests': self.startup[actor + ':requests'],
                    'responses': self.startup[actor + ':responses']}
        pin = {'actor': actor, 'chain': [], 'fds': {}, 'paths': {k: p for k, p in chain}}
        try:
            for key, path in chain:
                ident = self._dir_identity(path)
                if ident != expected[key]:
                    raise RelayFault('linked_folder', str(path) + ' is not the folder this relay started with.')
                pin['chain'].append((key, path, ident))
                if DIR_FD and key not in ('room', 'relay'):
                    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    pin['fds'][key] = fd
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != ident:
                        raise RelayFault('linked_folder', str(path) + ' changed while it was being opened.')
        except RelayFault:
            self._unpin(pin)
            raise
        except OSError as exc:
            self._unpin(pin)
            raise RelayFault('linked_folder', str(path) + ' is not a real, unlinked directory.') from exc
        return pin

    @staticmethod
    def _unpin(pin):
        for fd in pin['fds'].values():
            try:
                os.close(fd)
            except OSError:
                pass
        pin['fds'] = {}

    @staticmethod
    def _dir_identity(path):
        try:
            info = os.lstat(path)
            linked = stat.S_ISLNK(info.st_mode) or Path(path).is_junction()
        except OSError as exc:
            raise RelayFault('linked_folder', str(path) + ' is missing.') from exc
        if linked or not stat.S_ISDIR(info.st_mode):
            raise RelayFault('linked_folder', str(path) + ' is not a real, unlinked directory.')
        return (info.st_dev, info.st_ino)

    def _recheck(self, pin):
        for key, path, ident in pin['chain']:
            if self._dir_identity(path) != ident:
                raise RelayFault('linked_folder', str(path) + ' changed during the cycle.')

    def _verify_actor_dirs(self, actor):
        """Kept for callers that only need the check: pin and release."""
        self._unpin(self._pin(actor))

    # -- one cycle --------------------------------------------------------

    def process_once(self):
        for actor in self.actors:
            try:
                self._process_actor(actor)
            except Exception as exc:  # M1: one actor's fault never stops another actor.
                self._log(actor, None, None, None, False, 'actor_fault:' + type(exc).__name__)

    def _process_actor(self, actor):
        if not self._credential_path(actor).exists():
            self._log(actor, None, None, None, False, 'credential_missing')
            return
        try:
            pin = self._pin(actor)
        except RelayFault as exc:
            self._log(actor, None, None, None, False, exc.code)
            return
        try:
            self._cycle(actor, pin)
        except RelayFault as exc:
            self._log(actor, None, None, None, False, exc.code)
        finally:
            self._unpin(pin)

    def _cycle(self, actor, pin):
        client = self.clients[actor]
        candidates = self._list_requests(pin)
        if candidates:
            # L3: the connected identity's role is checked before any request
            # in this cycle is sent, not only when the state file is written.
            preflight, preflight_error = self._fetch_state(client)
            if preflight is not None and self._role(preflight) == 'owner':
                self._log(actor, None, None, None, False, 'owner_role')
                return
            for entry in candidates:
                try:
                    outcome = self._handle_request(actor, client, pin, entry)
                except RelayFault:
                    raise                     # a folder changed: stop now; the request is tried again later
                except Exception as exc:      # M1: one bad request never stops another.
                    outcome = None
                    self._log(actor, None, None, None, False, 'request_fault:' + type(exc).__name__)
                if outcome is RETRY:
                    # A4: the room could not be reached (a key renewal restarts it on the same port). Nothing is
                    # answered; this request and the rest wait for the next cycle, under the same request id.
                    break
                self.handled[actor].add(entry['key'])
        now = self.clock()
        due = now - self.last_state_write.get(actor, 0.0) >= self.state_every
        if candidates or due:
            fresh, fresh_error = self._fetch_state(client)
            if fresh is not None:
                self._persist_state(actor, pin, fresh)
            else:
                self._log(actor, None, 'state', None, False, fresh_error)

    def _list_requests(self, pin):
        actor = pin['actor']
        requests_dir = pin['paths']['requests']
        self._recheck(pin)
        names = os.listdir(pin['fds']['requests']) if DIR_FD else os.listdir(requests_dir)
        seen, items = set(), []
        for name in names:
            if name.endswith('.tmp'):
                continue
            try:
                info = self._entry_lstat(pin, name)
            except OSError:
                continue
            key = (name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            seen.add(key)
            if key in self.handled[actor]:
                continue
            items.append({'name': name, 'key': key, 'info': info})
        # The ledger only needs entries whose file is still there.
        self.handled[actor] &= seen
        self.retrying[actor] = {k: v for k, v in self.retrying[actor].items() if k in seen}
        items.sort(key=lambda e: e['info'].st_mtime_ns)
        return items[:MAX_PER_CYCLE]

    @staticmethod
    def _entry_lstat(pin, name):
        if DIR_FD:
            return os.stat(name, dir_fd=pin['fds']['requests'], follow_symlinks=False)
        return os.lstat(pin['paths']['requests'] / name)

    # -- one request --------------------------------------------------------

    def _handle_request(self, actor, client, pin, entry):
        name = entry['name']
        stem = name[:-5] if name.endswith('.json') else name
        request_id = kind = action = None
        result = None
        ok = False
        error_code = None
        message = None
        digest = None
        try:
            data = self._read_request(pin, entry)
            digest = hashlib.sha256(data).hexdigest()
            try:
                obj = self._parse_bytes(data, actor)
            except RelayMalformed:
                if self._already_answered(pin, RESPONSE_PREFIX + 'invalid-' + _sanitize_filename(stem) + '.json', name, digest):
                    self._log(actor, None, None, None, True, 'already_answered')
                    return
                raise
            request_id = obj['request_id']
            kind = obj['kind']
            # F1: after a relay restart the in-memory ledger is empty; the answer on disk is the record.
            if self._already_answered(pin, RESPONSE_PREFIX + _sanitize_filename(request_id) + '.json', name, digest):
                self._log(actor, request_id, kind, None, True, 'already_answered')
                return
            if kind == 'command':
                action = obj['action']
                if action in OWNER_ACTIONS:
                    error_code = 'forbidden'
                    message = 'Owner actions are not permitted through the relay.'
                else:
                    result = client.command(action, _task_payload(client, obj['payload']), request_id)
                    ok = True
            elif kind == 'handoff':
                action = obj['operation']
                result = client.handoff(action, _task_payload(client, obj['payload']))
                ok = True
            else:  # state — M2: report the truth about whether it was written.
                fresh, fresh_error = self._fetch_state(client)
                read, refused = [], []
                if fresh is not None:
                    read, refused = self._mark_questions_read(actor, client, fresh)
                    if read:
                        fresh, fresh_error = self._fetch_state(client)
                if fresh is None:
                    error_code = fresh_error
                    message = 'State refresh failed; nothing was written.'
                elif self._persist_state(actor, pin, fresh):
                    result = {'written': True}
                    if read or refused:
                        result['questions_read'] = read
                        if refused:
                            result['questions_not_marked'] = refused
                    ok = True
                else:
                    error_code = 'io'
                    message = 'State refresh could not be written to disk.'
        except RelayMalformed as exc:
            body = self._error_body(request_id, exc.code, exc.message)
            body['request_file'] = name
            body['request_sha256'] = digest
            self._write_file(pin, 'responses', RESPONSE_PREFIX + 'invalid-' + _sanitize_filename(stem) + '.json', body)
            self._log(actor, request_id, kind, action, False, exc.code)
            return
        except CrewError as exc:
            if exc.code == 'unavailable' and self._hold_for_retry(actor, entry):
                # A4: the room could not be reached, as while a key renewal restarts it on the same port. That is no
                # answer to a command: it waits, under the same request id, for up to RETRY_SECONDS, then is answered
                # "unavailable" as before (a room that is really down). State requests keep their M2 answer at once.
                self._log(actor, request_id, kind, action, False, 'unavailable_retry')
                return RETRY
            ok = False
            error_code = exc.code
            message = str(exc)
        self.retrying[actor].pop(entry['key'], None)

        body = {'ok': ok, 'request_id': request_id, 'kind': kind, 'request_file': name, 'request_sha256': digest}
        if ok:
            body['result'] = result
        else:
            body['error'] = error_code
            body['message'] = message
        self._write_file(pin, 'responses', RESPONSE_PREFIX + _sanitize_filename(request_id) + '.json', body)
        self._log(actor, request_id, kind, action, ok, None if ok else error_code)

    def _already_answered(self, pin, response_name, request_file, digest):
        """True when responses/<response_name> already answers this exact file and content.
        A chat could only make its own request be skipped by forging this, never another's."""
        self._recheck(pin)
        try:
            if DIR_FD:
                fd = os.open(response_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pin['fds']['responses'])
            else:
                path = pin['paths']['responses'] / response_name
                if os.path.islink(path):
                    return False
                fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        except OSError:
            return False
        try:
            info = os.fstat(fd)
            # N-c: a hard-linked answer file is not the relay's own; treat it as unanswered.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > PRESCAN_LIMIT:
                return False
            raw = os.read(fd, PRESCAN_LIMIT + 1)
        finally:
            os.close(fd)
        try:
            body = json.loads(raw.decode('utf-8'))
        except ValueError:
            return False
        return isinstance(body, dict) and body.get('request_file') == request_file and body.get('request_sha256') == digest

    def _read_request(self, pin, entry):
        name, pinned = entry['name'], entry['info']
        if not REQUEST_NAME.fullmatch(name):
            raise RelayMalformed('invalid', 'Request filenames must end in .json and use only safe characters.')
        if name[:-5].upper() in RESERVED_STEMS:
            raise RelayMalformed('invalid', 'Request filename must not be a reserved Windows device name.')
        if stat.S_ISLNK(pinned.st_mode) or not stat.S_ISREG(pinned.st_mode):
            raise RelayMalformed('invalid', 'Request file failed a safety check: links and non-files are refused.')
        if pinned.st_nlink != 1:
            raise RelayMalformed('invalid', 'Request file failed a safety check: shared (hard-linked) files are refused.')
        if pinned.st_size > PRESCAN_LIMIT:
            raise RelayMalformed('too_large', 'Request exceeds the 16 KiB budget.')
        self._recheck(pin)
        try:
            if DIR_FD:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pin['fds']['requests'])
            else:
                again = os.lstat(pin['paths']['requests'] / name)
                if (again.st_dev, again.st_ino) != (pinned.st_dev, pinned.st_ino) or stat.S_ISLNK(again.st_mode):
                    raise RelayMalformed('invalid', 'Request file changed before it was read.')
                fd = os.open(str(pin['paths']['requests'] / name), os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        except OSError as exc:
            raise RelayMalformed('invalid', 'Request file could not be opened safely.') from exc
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (pinned.st_dev, pinned.st_ino) or not stat.S_ISREG(opened.st_mode) \
                    or opened.st_nlink != 1:
                raise RelayMalformed('invalid', 'Request file changed before it was read.')
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_REQUEST_BYTES:
                    raise RelayMalformed('too_large', 'Request exceeds the 16 KiB budget.')
        finally:
            os.close(fd)
        return b''.join(chunks)

    def _parse_bytes(self, data, actor=None):
        try:
            obj = decode_request(data)
        except RoomError as exc:
            raise RelayMalformed(exc.code, str(exc)) from exc
        request_id = obj.get('request_id')
        if type(request_id) is not str or not REQUEST_ID.fullmatch(request_id):
            raise RelayMalformed('invalid', 'request_id must be letters, digits, _, . or - only, up to 64 characters.')
        if request_id.split('.')[0].upper() in RESERVED_STEMS:
            raise RelayMalformed('invalid', 'request_id must not be a reserved Windows device name.')
        kind = obj.get('kind')
        if kind == 'command':
            if set(obj) != {'request_id', 'kind', 'action', 'payload'}:
                raise RelayMalformed('invalid', 'Command requests need exactly request_id, kind, action and payload.')
            action = obj['action']
            payload = obj['payload']
            chat_actions = set(chat_rules.ACTIONS) if self.chat is not None and actor in self.chat['seats'] else set()
            if type(action) is not str or action not in (COMMAND_ACTIONS | OWNER_ACTIONS | chat_actions):
                raise RelayMalformed('invalid', 'Unsupported command action.')
            if type(payload) is not dict:
                raise RelayMalformed('invalid', 'payload must be an object.')
        elif kind == 'handoff':
            if set(obj) != {'request_id', 'kind', 'operation', 'payload'}:
                raise RelayMalformed('invalid', 'Handoff requests need exactly request_id, kind, operation and payload.')
            operation = obj['operation']
            payload = obj['payload']
            if type(operation) is not str or operation not in HANDOFF_OPERATIONS:
                raise RelayMalformed('invalid', 'Unsupported handoff operation.')
            if type(payload) is not dict:
                raise RelayMalformed('invalid', 'payload must be an object.')
        elif kind == 'state':
            if set(obj) != {'request_id', 'kind'}:
                raise RelayMalformed('invalid', 'State requests take no other fields.')
        else:
            raise RelayMalformed('invalid', 'kind must be command, handoff or state.')
        return obj

    def _hold_for_retry(self, actor, entry):
        """True while a request that met an unreachable room is inside its RETRY_SECONDS (by this relay's clock)."""
        first = self.retrying[actor].setdefault(entry['key'], self.clock())
        return self.clock() - first < RETRY_SECONDS

    @staticmethod
    def _mark_questions_read(actor, client, state):
        """A1: a `state` request from this seat's own folder is the seat picking its questions up, so each question
        addressed to it that is not yet read is marked read, under this seat's own credential. The relay's own
        periodic state write never does this: a question the relay merely holds is delivered, not read. Returns the
        seqs marked and [{seq, error}] for any the room refused (left delivered; the next state request tries again)."""
        read, refused = [], []
        for question in state.get('questions') or []:
            if question.get('to') != actor or (question.get('marks') or {}).get('read') is not None:
                continue
            seq = question['seq']
            try:
                # One id per question: a retry, or the same seat's crew watch, answers from the stored receipt.
                client.command('question_mark', {'note_seq': seq, 'mark': 'read'}, 'qmark-read-' + str(seq))
                read.append(seq)
            except CrewError as exc:
                refused.append({'seq': seq, 'error': exc.code})
        return read, refused

    @staticmethod
    def _error_body(request_id, code, message):
        body = {'ok': False, 'error': code, 'message': message}
        if request_id is not None:
            body['request_id'] = request_id
        return body

    # -- state --------------------------------------------------------

    @staticmethod
    def _role(state):
        actor = state.get('actor')
        return actor.get('role') if isinstance(actor, dict) else None

    def _fetch_state(self, client):
        try:
            return client.state(), None
        except CrewError as exc:
            return None, exc.code

    def _persist_state(self, actor, pin, state):
        """Write `state` for `actor` and report the truth about it (M2): a
        caller must never claim `written: true` unless this returns True."""
        if self._role(state) == 'owner':
            self._log(actor, None, 'state', None, False, 'owner_role')
            return False
        payload = dict(state)
        payload['relay_written_at'] = state.get('server_time')
        payload['relay_actor'] = actor
        try:
            self._write_file(pin, 'seat', STATE_NAME, payload)
        except RelayFault:
            raise
        except (OSError, ValueError) as exc:
            self._log(actor, None, 'state', None, False, 'state_write_failed:' + type(exc).__name__)
            return False
        self.last_state_write[actor] = self.clock()
        return True

    # -- writes --------------------------------------------------------

    def _write_file(self, pin, where, name, value):
        """Create or replace one relay-owned file (resp-*.json or relay-state.json) in the
        pinned `where` folder ('responses' or 'seat'). Never follows a link, never writes a
        shared (hard-linked) target, never touches any other name."""
        if not RELAY_OWNED.fullmatch(name):
            raise ValueError('The relay only writes relay-owned file names.')
        data = canonical(value).encode('utf-8')
        temporary = name + '.' + uuid.uuid4().hex + '.tmp'
        self._recheck(pin)
        if DIR_FD:
            dfd = pin['fds'][where]
            try:
                existing = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
                raise ValueError('Refusing to replace a link or shared file: ' + name)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                try:
                    os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=dfd)
                except OSError:
                    pass
                raise
            return
        # Windows: no directory handles for these calls. The chain was re-checked just
        # above with no room call or sleep in between; check the target itself too.
        folder = pin['paths'][where]
        target, temp = folder / name, folder / temporary
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1 or target.is_junction()):
            raise ValueError('Refusing to replace a link or shared file: ' + name)
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._recheck(pin)
            os.replace(temp, target)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _log(self, actor, request_id, kind, action, ok, error):
        print(json.dumps({'actor': actor, 'request_id': request_id, 'kind': kind,
                           'action': action, 'ok': ok, 'error': error}, sort_keys=True), flush=True)


class RelayLock:
    """One relay per room: <room>/relay.lock, the same pattern as the room's .launcher.lock
    (fcntl on POSIX, msvcrt on Windows). Closing the file releases it."""

    def __init__(self, root):
        self.path = Path(root) / 'relay.lock'
        self.handle = None
        self.reason = None   # 'linked' or 'busy' after a refused acquire()

    def acquire(self):
        # N-e: a lock file that is a link (or not a plain, unshared file) is refused with its
        # own reason, so the message never claims another relay is running when none is.
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            info = None
        if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                                 or info.st_nlink != 1 or self.path.is_junction()):
            self.reason = 'linked'
            return False
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except OSError:
            self.reason = 'linked'
            return False
        handle = os.fdopen(fd, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.reason = 'busy'
            return False
        self.handle = handle
        self.reason = None
        return True

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actors', nargs='+', required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / '.runtime' / 'handoffs')
    parser.add_argument('--interval', type=float, default=3.0)
    parser.add_argument('--state-every', type=float, default=15.0)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    # A3: the live room's relay reads the chat flag through the one decision function, on its own tree, at start.
    tree = Path(__file__).resolve().parents[1]
    pair = None
    if args.root.absolute() == (tree / '.runtime' / 'handoffs').absolute():
        try:
            pair = chat_rules.chat_pair(tree / chat_rules.FLAG_PATH)
        except chat_rules.ChatFlagError as exc:
            print('common_room.relay: ' + str(exc) + '. No relay started.', file=sys.stderr)
            raise SystemExit(2)
    lock = RelayLock(args.root)
    if not lock.acquire():
        if lock.reason == 'linked':
            print('common_room.relay: ' + str(lock.path) + ' is not a plain file (a link, junction or shared '
                  'file). No relay started. Tell Claude; do not delete anything by hand.', file=sys.stderr)
        else:
            print('common_room.relay: another relay is already running for this room; close its window first.',
                  file=sys.stderr)
        raise SystemExit(2)
    try:
        relay = Relay(args.root, args.actors, state_every=args.state_every, **({'chat': pair} if pair is not None else {}))
    except (RelayError, RelayFault) as exc:   # N-f: a linked --root is a clean refusal, not a traceback
        lock.release()
        print('common_room.relay: ' + exc.message, file=sys.stderr)
        raise SystemExit(2)
    try:
        if args.once:
            relay.process_once()
            return
        while True:
            try:
                relay.process_once()
            except Exception as exc:  # M1: the scheduler itself must survive a cycle's fault.
                print(json.dumps({'fault': type(exc).__name__, 'message': str(exc)}), file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


if __name__ == '__main__':
    main()
