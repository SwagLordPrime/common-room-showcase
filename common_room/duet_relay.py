"""The duet relay: Claude Duet (Shane's cloud chief-of-staff app) acts as the Common Room seat "duet" through a
Google-Drive-synced folder (plan duet-seat-2026-10-30).

Duet runs in a cloud container and cannot reach this PC's loopback room. Its only bridge is the folder DuetRelay,
synced by Drive for desktop. DuetRelay is therefore a public-ish surface: this relay writes nothing there that has
not passed `duet_mirror.scrub()` and `duet_mirror.guard()`. What leaves the PC is stated in `duet_mirror`'s docstring.

DuetRelay is a subclass of `common_room.relay.Relay`, so the request-id rules, the 16 KiB cap, folder pinning, the
"never move, rename or delete a request" rule and the room's stored receipts are the same code. It differs where
Drive differs from a local folder:

  * layout: requests/ responses/ mirror/ README.md. No archive, no relay-state.json (raw state never leaves the PC;
    mirror/board.json is a whitelisted snapshot instead).
  * temp files live in a folder outside DuetRelay (Drive would upload them), then os.replace into place.
  * Drive lag: a request can arrive late, half written, under a duplicate name ("req-1 (1).json") or twice. A file
    that does not yet parse is held, unanswered and unsent, until its size and mtime have been unchanged for
    SETTLE_SECONDS; a duplicate of an answered request is skipped; a reused request_id with other content is refused
    (in its own file, never over the first answer).
  * duet never reviews, may act only on tasks addressed to duet, and may read handoff views only of tasks it is the
    recipient or sender of; every handoff reply is cut to a whitelist first (see duet_mirror).
  * after LINKED_RESTART_CYCLES cycles in a row with a folder that is not the one pinned at start, and none of them
    a link, the relay exits non-zero (3) so its keepalive window restarts it and it pins the folders again.

CLI:  py -3 -m common_room.duet_relay --config crew/duet.json [--root <runtime>] [--interval 5] [--once]
"""
from pathlib import Path
import argparse, errno, hashlib, json, os, re, stat, sys, time, uuid
from datetime import datetime, timezone

from .crew_client import CrewClient, CrewError
from .duet_mirror import SHA256, BlockedSecret, build_mirror, guard, handoff_view, scrub, scrub_mirror
from .protocol import canonical
from . import relay as base
from .relay import (MAX_PER_CYCLE, MAX_REQUEST_BYTES, OWNER_ACTIONS, PRESCAN_LIMIT, RESERVED_STEMS, RESPONSE_PREFIX,
                    RETRY, DIR_FD, Relay, RelayError, RelayFault, RelayLock, RelayMalformed, _sanitize_filename,
                    _task_payload)

ACTOR = 'duet'
SETTLE_SECONDS = 150.0           # a half-written file must be unchanged this long before it is called invalid
MIRROR_EVERY = 300.0
LINKED_RESTART_CYCLES = 20       # consecutive cycles with a changed (not linked) folder before the relay asks for a restart
WRITE_WAIT_FIRST = 10.0          # seconds to wait after a failed write; doubles on each further failure
WRITE_WAIT_MAX = 300.0
# Drive duplicate names ("req-1 (1).json") are legal here, unlike the base relay.
DUET_REQUEST_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_. ()-]{0,95}\.json')
IGNORED_NAMES = frozenset({'desktop.ini'})
DUET_OWNED = re.compile(r'(resp-[A-Za-z0-9_.-]{1,100}\.json|board\.json|README\.md)')
COMMAND_ACTIONS = base.COMMAND_ACTIONS - {'review'}
TASK_BOUND = frozenset({'ack', 'claim', 'renew', 'finish', 'fail', 'wait_decision'})   # only on a task addressed to duet
RESERVED_ID_PREFIXES = ('invalid-', 'qmark-')   # answer file names (resp-invalid-...) and the relay's own question marks
ENVELOPE = ('ok', 'kind', 'request_id', 'request_file', 'request_sha256', 'performed')   # Duet's own fields in an answer
HOLD = object()                # _handle_request: leave this request for a later cycle, but go on to the next one
BLOCKED_MESSAGE = 'The answer held a private value, so it was not written. Ask on the PC.'
PERFORMED_MESSAGE = ('The room did this, but its answer held a private value, so the answer was not written. '
                     'Ask on the PC.')
# DUET-FIX1 F1: a state request that marked questions read and then could not write the mirror.
PARTIAL_REASONS = {'blocked': 'the snapshot held a private value, so it was not written',
                   'io': 'the disk refused the write',
                   'error': 'the board could not be read or written for another reason'}
PARTIAL_RETRY = 'Send a new state request with a new request_id and a new file name; marks already done are not repeated.'
PARTIAL_SHORT = 'The room marked questions read for this request, but mirror/board.json was not written. The marks stay done.'

DUET_README_TEMPLATE = """# DuetRelay: how Duet talks to the Common Room

This folder is the only bridge between Duet and Shane's PC. A relay program on the PC watches `requests/`,
performs each request in the Common Room as the seat "duet" (using duet's own key, which it refuses to write here), and writes
the answer into `responses/`. It also keeps a read-only snapshot of the board in `mirror/board.json`.
Nothing else is read or written. Do not write anything else into this folder.

## Making a request

Create ONE whole file per request in `requests/`, under a NEW unique name: `<request_id>.json`.
Never edit a file you already wrote, and never reuse a file name. Drive for desktop carries the file to the PC;
that can take up to two minutes. The file may be at most 16 KiB. Names use letters, digits, `_`, `.`, `-`, spaces
and brackets (Drive makes copies named like `req (1).json`), end in `.json`, and are not a Windows device name
such as `CON` or `NUL`; another name is answered as invalid.

The `request_id` inside is 1-64 letters, digits, `_`, `.` or `-`, starting with a letter or digit, and not a
Windows device name. It must not start with `invalid-` or `qmark-`, and must not look like a key (a run of 43
or more random-looking letters and digits is refused as invalid). Ordinary ids, numbers in them included, are
returned exactly as sent. Use a NEW id for every request.
Sending the very same file again is safe: it is skipped, because it was answered. Reusing an id with different
content is refused (see "Reading the answer").

Three kinds of request:

    {"request_id": "state-0001", "kind": "state"}
        Writes a fresh `mirror/board.json` now and answers {"written": true, "mirror": "mirror/board.json"}.

    {"request_id": "note-0002", "kind": "command", "action": "note",
     "payload": {"authority_id": "orchestra", "body": "Hello from Duet"}}

    {"request_id": "stage-0003", "kind": "handoff", "operation": "stage_text",
     "payload": {"task_id": "t...", "fence": 2, "text": "..."}}

A task reference may be a full id or a unique visible prefix of 6 or more characters.

Command actions and their payload fields (all fields shown are required unless marked optional):

    ack              {"task_id"}
    claim            {"task_id", "seconds"}                       seconds is a whole number from 1 to 1800
    renew            {"task_id", "fence", "seconds"}
    finish           {"task_id", "fence", "body", "refs"}         body is your summary; refs is a list of
                                                                  {"path", "sha256"} taken from stage_text answers
    fail             {"task_id", "fence", "body"}                 body explains the failure
    wait_decision    {"task_id", "fence", "question"}
    note             {"authority_id", "body"}
    request_help     {"task_id", "fence", "recipient", "title", "body", "acceptance"}
                     optional: "undoable", "tier", "effort"; use it only for a different worker, never duet itself
    question_mark    {"note_seq", "mark"}                         mark is "read", "accepted" or "checked"
    resolve_decision {"task_id", "body"}                          optional "refund": true with a written reason in body
    cancel           {"task_id", "body"}                          body is the reason

The room decides who may do what. `review` is refused: duet never reviews. ack, claim, renew, finish, fail and
wait_decision work only on a task addressed to duet (otherwise error `not_yours`, and nothing is sent).
intake, deliver, assign, reinstate, pause, resume, revoke, grant, grant_handoff, stop_all and resume_all are
owner actions and are refused before anything is sent.

Handoff operations and their payload fields:

    stage_text   {"task_id", "fence", "text"}     answer: {"path", "sha256"}; use them in finish refs
    view_text    {"task_id", "path", "sha256"}    answer: {"path", "sha256", "text"}
    receipt      {"task_id"}                      answer: task id, scope, status, seats, lease, saved results
    task_view    {"task_id"}                      answer: the task (title, status, seats, lease, strikes, body,
                                                  acceptance) and its events (seq, kind, actor, timestamp only)

All four handoff operations work only on a task whose recipient or sender is duet (otherwise error `not_yours`,
nothing is sent). Their answers hold only the fields listed here. A receipt does not carry the intake reference.

Text caps are characters: finish summary, note body, failure explanation and resolve_decision body 4000;
decision question, review note and cancellation reason 2000; room stop/resume reason 500 (owner route only).
Helper title 120, body 4000, acceptance 2000. stage_text allows at most 12000 characters AND 12000 UTF-8 bytes;
the whole JSON request still has the 16 KiB byte budget above.

## Questions addressed to duet

A note whose first line starts `QUESTION FOR duet:` is a question for this seat. It appears in
`mirror/board.json` under `questions` (to is duet) with its marks: delivered, read, accepted, checked. Your own
`state` request is you picking it up: it marks each unread question to duet "read" and lists them in its answer as
`questions_read`. Take a question up with a `question_mark` command (mark "accepted") and answer in a note citing
#N; the asker marks it "checked" once your answer settles it. Questions duet asked are under `asked`.
Marks wake nobody.

## Reading the answer

The answer is the file `responses/resp-<request_id>.json`:

    {"ok": true, "kind": "...", "result": ..., "request_id": "...", "request_file": "...", "request_sha256": "..."}
    {"ok": false, "kind": "...", "error": "<code>", "message": "...", "request_id": "...", "request_file": "...", "request_sha256": "..."}

ALWAYS check that `request_file` and `request_sha256` in an answer are those of the file you wrote: they name
exactly which file the answer is for. If they are not yours, the answer belongs to an earlier request that used
the same id.

A file the relay could not read as a request at all is answered as `responses/resp-invalid-<file name>.json`.
In the file name every character other than letters, digits, `_`, `.` and `-` becomes `-`, and `-` and `.` are
trimmed from both ends (for example `req (1).json` is answered as `resp-invalid-req--1.json`). The relay answers a
half-written file this way only after the file has stayed unchanged for 150 seconds, so give a file time to arrive.

A request_id used again with different content is not sent. The refusal (error `request_id_reused`) is written to
`responses/resp-<request_id>--reused-<first 8 characters of request_sha256>.json`, and the answer to the first
request is left as it was. Use a new id and a new file name.

If the room did the work but its answer held a private value, you get `{"ok": true, "result": "withheld",
"performed": true, ...}`: the work was done, check the board. If the answer could not be written for any other
reason, you get error `blocked` with `"performed": false`: nothing was done by this request.

A `state` request that marked questions read and then could not write `mirror/board.json` gets a partial answer:

    {"ok": false, "kind": "state", "error": "<reason>", "performed": true, "partial": true,
     "done": {"questions_read": [<question ids>]}, "mirror_written": false, "message": "...", "retry": "..."}

The marks were done and stay done; only the snapshot was not written. `<reason>` is `blocked` (the snapshot held a
private value), `io` (the disk refused the write) or `error` (anything else). Retry with a new state request, a new
request_id and a new file name; it does not mark anything twice. If even that answer held a private value, `done`
holds only `questions_read_count`. A `state` answer that wrote the snapshot may also say `items_left_out`: how many
notes or other items the filter left out of it (never their text).

Allow up to 6 minutes for a round trip (Drive in each direction, plus the relay's own cycle) before you treat the
relay as down. The relay never moves, renames or deletes your files; tidy your own requests if you wish.

## The board snapshot

`mirror/board.json` is the board as duet sees it: tasks (id, title, status, who, lease holder and time,
needs-Shane flag), the last 50 Lounge notes, what is waiting on Shane, questions to and from duet, and the
seats with their role and how recently each was heard from. The relay rewrites it every 5 minutes, and at once
on a `state` request. Read it, never write it. It is a picture with known secret shapes removed (keys and
tokens, secret names, credential and key file names, internet addresses, file paths on the PC, email addresses,
money amounts, long numbers), and a note that names a private file or folder is replaced by a short marker.
A note that holds duet's own key is left out of the snapshot. The relay removes known secret shapes, but
long-path tails, 64-hex strings and lowercase slug passphrases can pass, and any task title, recent Lounge note or
question to or from duet can reach the Drive mirror, so no seat puts a secret in one.

## The Shane desk

Ruled by claude-room, 2026-10-30. Questions only Shane can answer reach him by one of two carriers: you (by text) or the dot (in its chat).

- claude-room answers what it can and tags each remaining line `FOR SHANE #<n> (duet)` or `FOR SHANE #<n> (dot)`.
  A line with no tag means dot.
- The carrier posts `CARRYING #<n>` when it asks Shane. No other seat carries a number that has one.
- You carry your lines to Shane by text. When he answers, post a Lounge note:
  `SHANE UPDATE: answering #<n> (by text, via duet): <his words>`
- **Only claude-room acts on SHANE UPDATE notes.** You never act on one yourself.
- If the carrier has not posted `CARRYING #<n>` within 2 hours, claude-room may re-post the line with the other
  carrier; the first carrier then drops it.
- You never carry anything that needs Shane's own typed words: permission prompts in a seat's own window, AGENTS.md
  changes, Rune or astra-audit acceptances, can't-undo steps, money, security or access. Those lines carry no carrier
  tag and say "Shane's own words needed". A relayed answer never replaces Shane's own typed words where those are
  required.

## This is still the room

An answer is a record of what the room did or refused. Its rules apply in full: two strikes stop a task.
"""


class DuetRelayLock(RelayLock):
    """One duet relay per room: <room>/duet-relay.lock (same pattern as the base relay lock)."""

    def __init__(self, root):
        super().__init__(root)
        self.path = Path(root) / 'duet-relay.lock'


class DuetRelay(Relay):
    """Watches DuetRelay/requests and performs each request as the seat `duet`."""

    def __init__(self, root, folder, tmp_dir=None, clock=time.time, mirror_every=MIRROR_EVERY):
        self.root = Path(root).absolute()
        self.folder = Path(folder).absolute()
        self.clock = clock
        self.mirror_every = float(mirror_every)
        self.state_every = self.mirror_every
        self.relay_root = self.folder
        self.tmp_dir = Path(tmp_dir).absolute() if tmp_dir is not None else self.root / 'duet-tmp'
        self._check_nesting()
        self.startup = {'room': self._dir_identity(self.root)}
        self.folders = {}
        self.clients = {}
        self.handled = {ACTOR: set()}
        self.retrying = {ACTOR: {}}
        self.waiting = {}              # request name -> ((size, mtime_ns), first time seen unchanged)
        self.last_state_write = {ACTOR: 0.0}
        self.last_mirror = None        # None: write the mirror on the first cycle
        self.linked_cycles = 0         # consecutive cycles refused because a pinned folder is no longer the same one
        self.restart_wanted = False
        self.write_wait = 0.0          # back-off after a failed write
        self.backoff_until = 0.0
        self.actors = [ACTOR]
        self._prepare_tmp()
        self.folders[ACTOR] = self._prepare_actor_folder(ACTOR)
        folder_paths = self.folders[ACTOR]
        for key in ('root', 'requests', 'responses', 'mirror'):
            self.startup[key] = self._dir_identity(folder_paths[key])
        self.clients[ACTOR] = CrewClient(self._credential_path(ACTOR), expected_actor=ACTOR)
        pin = self._pin(ACTOR)
        try:
            self._emit(pin, 'root', 'README.md', DUET_README_TEMPLATE, as_text=True)
        except BlockedSecret as exc:
            raise RelayError('config', 'The README held a private value; nothing was written.') from exc
        except (OSError, ValueError) as exc:
            raise RelayError('config', 'The README could not be written: ' + type(exc).__name__) from exc
        finally:
            self._unpin(pin)

    # -- startup --------------------------------------------------------

    def _check_nesting(self):
        root, folder = self.root.resolve(), self.folder.resolve()
        if folder == root or folder.is_relative_to(root) or root.is_relative_to(folder):
            raise RelayError('config', 'The DuetRelay folder and the room runtime folder must not contain each other.')
        tmp = self.tmp_dir.resolve()
        if tmp == folder or tmp.is_relative_to(folder) or folder.is_relative_to(tmp):
            raise RelayError('config', 'The temp folder must be outside the DuetRelay folder.')

    def _prepare_tmp(self):
        self._prepare_dir(self.tmp_dir)
        self.startup['tmp'] = self._dir_identity(self.tmp_dir)

    def _prepare_actor_folder(self, actor):
        top = self._prepare_dir(self.folder)
        requests_dir = self._prepare_dir(top / 'requests')
        responses_dir = self._prepare_dir(top / 'responses')
        mirror_dir = self._prepare_dir(top / 'mirror')
        readme_path = top / 'README.md'
        if readme_path.is_symlink() or readme_path.is_junction():
            raise RelayError('config', str(readme_path) + ' must not be a symlink or junction.')
        if readme_path.exists() and readme_path.is_dir():
            raise RelayError('config', str(readme_path) + ' exists and is a directory.')
        return {'root': top, 'requests': requests_dir, 'responses': responses_dir, 'mirror': mirror_dir,
                'readme': readme_path}

    # -- pinning --------------------------------------------------------

    def _pin(self, actor):
        folder = self.folders[actor]
        chain = [('room', self.root), ('root', folder['root']), ('requests', folder['requests']),
                 ('responses', folder['responses']), ('mirror', folder['mirror'])]
        pin = {'actor': actor, 'chain': [], 'fds': {}, 'paths': {k: p for k, p in chain}}
        path = self.root
        try:
            for key, path in chain:
                ident = self._dir_identity(path)
                if ident != self.startup[key]:
                    raise RelayFault('linked_folder', str(path) + ' is not the folder this relay started with.')
                pin['chain'].append((key, path, ident))
                if DIR_FD and key != 'room':
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

    def _chain_is_plain(self):
        """True when no folder of the chain is a link or junction (a missing one is made again at restart)."""
        folder = self.folders[ACTOR]
        for path in (self.root, folder['root'], folder['requests'], folder['responses'], folder['mirror']):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or Path(path).is_junction():
                return False
        return True

    def _note_linked(self, code):
        if code != 'linked_folder':
            return
        self.linked_cycles += 1
        if self.linked_cycles >= LINKED_RESTART_CYCLES and self._chain_is_plain():
            self.restart_wanted = True

    # -- secrets and writes (everything goes through the filter) -----------------------------

    def _secrets(self):
        """duet's own room key (and session cookie), re-read from its credential file each time."""
        found = []
        try:
            path = self._credential_path(ACTOR)
            if path.stat().st_size <= 4096:
                config = json.loads(path.read_bytes().decode('utf-8'))
                if isinstance(config, dict) and isinstance(config.get('token'), str):
                    found.append(config['token'])
        except (OSError, ValueError):
            pass
        participant = getattr(self.clients.get(ACTOR), '_participant', None)
        for attribute in ('token', 'cookie'):
            value = getattr(participant, attribute, None)
            if isinstance(value, str) and value:
                found.append(value)
                if attribute == 'cookie' and '=' in value:
                    found.append(value.split('=', 1)[1])
        return [s for s in found if s]

    def _emit(self, pin, where, name, value, as_text=False, scrubbed=False):
        """scrub -> canonical JSON (or text) -> guard -> temp file outside DuetRelay -> os.replace.
        Raises BlockedSecret (nothing written). Returns True if written, False if the replace was refused."""
        secrets = self._secrets()
        clean = value if scrubbed else scrub(value, secrets=secrets, keep_sha256=(where == 'responses'))
        data = (clean if as_text else canonical(clean)).encode('utf-8')
        guard(data, secrets)
        return self._put(pin, where, name, data)

    def _envelope_value(self, key, value, secrets):
        """A field of the answer that is Duet's own input, not the room's: kept exactly as it is when it has the shape
        the relay already checked (a request id, a request file name, a sha256, a kind, a flag) and the final guard
        accepts it. Anything else is scrubbed like room text. Scrubbing a good id would rewrite it (note-1730329200
        is not a number to hide) and make the duplicate check treat Drive's copy as a different request."""
        if key in ('ok', 'performed'):
            return bool(value)
        if value is None:
            return None
        shaped = type(value) is str and (
            (key == 'kind' and value in ('command', 'handoff', 'state'))
            or (key == 'request_id' and base.REQUEST_ID.fullmatch(value) is not None)
            or (key == 'request_file' and DUET_REQUEST_NAME.fullmatch(value) is not None)
            or (key == 'request_sha256' and SHA256.fullmatch(value) is not None))
        if shaped:
            try:
                guard(value.encode('utf-8'), secrets)
                return value
            except BlockedSecret:
                pass
        return scrub(value, secrets=secrets)

    def _emit_response(self, pin, name, body):
        """An answer: the room's part (result, error, message) is scrubbed; Duet's own envelope fields are set after
        that (see _envelope_value); guard() then checks the final bytes. Raises BlockedSecret (nothing written)."""
        secrets = self._secrets()
        room = {k: v for k, v in body.items() if k not in ENVELOPE}
        clean = scrub(room, secrets=secrets, keep_sha256=True)
        for key in ENVELOPE:
            if key in body:
                clean[key] = self._envelope_value(key, body[key], secrets)
        data = canonical(clean).encode('utf-8')
        guard(data, secrets)
        return self._put(pin, 'responses', name, data)

    def _key_like(self, text):
        """True when `text` would trip the final guard (a key-shaped run, a private name, duet's own key)."""
        try:
            guard(text.encode('utf-8'), self._secrets())
        except BlockedSecret:
            return True
        return False

    def _write_file(self, pin, where, name, value):    # the base's entry point: still filtered
        return self._emit(pin, where, name, value)

    def _note_write_failure(self):
        self.write_wait = min(WRITE_WAIT_MAX, self.write_wait * 2 if self.write_wait else WRITE_WAIT_FIRST)
        self.backoff_until = self.clock() + self.write_wait

    def _put(self, pin, where, name, data):
        if not DUET_OWNED.fullmatch(name) or where not in pin['paths'] or where == 'room':
            raise ValueError('The relay only writes relay-owned file names.')
        if self._dir_identity(self.tmp_dir) != self.startup['tmp']:
            raise RelayFault('linked_folder', str(self.tmp_dir) + ' is not the folder this relay started with.')
        temp = self.tmp_dir / (name + '.' + uuid.uuid4().hex + '.tmp')
        folder = pin['paths'][where]
        self._recheck(pin)
        if DIR_FD:
            dfd = pin['fds'][where]
            try:
                existing = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
        else:
            target = folder / name
            try:
                existing = os.lstat(target)
            except FileNotFoundError:
                existing = None
            if existing is not None and target.is_junction():
                raise ValueError('Refusing to replace a link or shared file: ' + name)
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
            raise ValueError('Refusing to replace a link or shared file: ' + name)
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            try:
                # DUET-FIX1 F2: os.write may write only part of the bytes; count every write until all are written.
                # No progress is a failure (never a silent short file, never a loop forever). Any failure lands in the
                # except below: no replace, the temp file is removed, and the caller sees False as before.
                view, done = memoryview(data), 0
                while done < len(data):
                    count = os.write(fd, view[done:])
                    if not count:
                        raise OSError(errno.EIO, 'no bytes written')
                    done += count
                os.fsync(fd)
            finally:
                os.close(fd)
            self._recheck(pin)
            if DIR_FD:
                os.replace(str(temp), name, dst_dir_fd=pin['fds'][where])
            else:
                os.replace(temp, folder / name)
            self.write_wait = 0.0
            return True
        except OSError as exc:
            self._log(ACTOR, None, None, None, False, 'replace_failed:' + type(exc).__name__)
            self._note_write_failure()
            return False
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass

    def _respond(self, pin, name, body, performed=False):
        """Write one answer. A body that holds a private value is replaced by the short 'blocked' answer
        (performed true when the room already did the work). Returns False only when the disk refused the write
        (the caller leaves the request for a later cycle)."""
        try:
            return self._emit_response(pin, name, body)
        except BlockedSecret:
            self._log(ACTOR, body.get('request_id'), body.get('kind'), None, False, 'blocked_secret')
        if performed:
            fallback = {'ok': True, 'result': 'withheld', 'performed': True, 'message': PERFORMED_MESSAGE}
        else:
            fallback = {'ok': False, 'error': 'blocked', 'performed': False, 'message': BLOCKED_MESSAGE}
        fallback['request_file'] = body.get('request_file')
        fallback['request_sha256'] = body.get('request_sha256')
        for field in ('request_id', 'kind'):
            if body.get(field) is not None:
                fallback[field] = body[field]
        try:
            return self._emit_response(pin, name, fallback)
        except BlockedSecret:
            self._log(ACTOR, body.get('request_id'), None, None, False, 'blocked_secret')
            return True

    # -- the mirror --------------------------------------------------------

    def _now_text(self):
        return 'relay clock, UTC: ' + datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _write_mirror(self, pin, state):
        """Write mirror/board.json from `state`. True when written. An item (one note, one task) holding duet's own
        key is left out and logged `blocked_item`; BlockedSecret from the final guard propagates (nothing written)."""
        return self._mirror_outcome(pin, state)[0]

    def _mirror_outcome(self, pin, state):
        """_write_mirror's work, with what a `state` answer needs to report it (DUET-FIX1 F1): (written, items left
        out by the filter, why not written: 'error' for an owner role, 'io' for a write that failed, None when
        written). Only the count of left-out items is returned, never their text. BlockedSecret still propagates."""
        if self._role(state) == 'owner':
            self._log(ACTOR, None, 'state', None, False, 'owner_role')
            return False, 0, 'error'
        clean, left_out = scrub_mirror(build_mirror(state, self._now_text()), self._secrets())
        for _ in range(left_out):
            self._log(ACTOR, None, 'state', None, False, 'blocked_item')
        try:
            written = self._emit(pin, 'mirror', 'board.json', clean, scrubbed=True)
        except RelayFault:
            raise
        except (OSError, ValueError) as exc:
            self._log(ACTOR, None, 'state', None, False, 'mirror_write_failed:' + type(exc).__name__)
            self._note_write_failure()
            return False, left_out, 'io'
        if written:
            self.last_mirror = self.clock()
            self.last_state_write[ACTOR] = self.last_mirror
        return written, left_out, None if written else 'io'

    def _persist_state(self, actor, pin, state):    # nothing ever writes raw state: the mirror is the only snapshot
        return self._write_mirror(pin, state)

    # -- one cycle --------------------------------------------------------

    def _process_actor(self, actor):
        if not self._credential_path(actor).exists():
            self._log(actor, None, None, None, False, 'credential_missing')
            return
        try:
            pin = self._pin(actor)
        except RelayFault as exc:
            self._log(actor, None, None, None, False, exc.code)
            self._note_linked(exc.code)
            return
        try:
            self._cycle(actor, pin)
            self.linked_cycles = 0
        except RelayFault as exc:
            self._log(actor, None, None, None, False, exc.code)
            self._note_linked(exc.code)
        finally:
            self._unpin(pin)

    def _cycle(self, actor, pin):
        if self.clock() < self.backoff_until:
            return                       # a write failed lately: wait before sending anything to the room again
        client = self.clients[actor]
        candidates = self._list_requests(pin)
        if candidates:
            preflight, _ = self._fetch_state(client)
            if preflight is not None and self._role(preflight) == 'owner':
                self._log(actor, None, None, None, False, 'owner_role')
                return
            for entry in candidates:
                try:
                    outcome = self._handle_request(actor, client, pin, entry)
                except RelayFault:
                    raise
                except Exception as exc:     # a disk hiccup is tried again; any other fault is logged and not repeated
                    outcome = HOLD if isinstance(exc, OSError) else None
                    if isinstance(exc, OSError):
                        self._note_write_failure()
                    self._log(actor, None, None, None, False, 'request_fault:' + type(exc).__name__)
                if outcome is RETRY:
                    break
                if outcome is HOLD:
                    if self.clock() < self.backoff_until:
                        break            # the disk is refusing writes: no further room calls this cycle
                    continue
                self.handled[actor].add(entry['key'])
        now = self.clock()
        if self.clock() >= self.backoff_until and (self.last_mirror is None or now - self.last_mirror >= self.mirror_every):
            fresh, fresh_error = self._fetch_state(client)
            if fresh is None:
                self._log(actor, None, 'state', None, False, fresh_error)
                return
            try:
                self._write_mirror(pin, fresh)
            except BlockedSecret:
                self._log(actor, None, 'state', None, False, 'blocked_secret')
                self.last_mirror = self.clock()      # try again after mirror_every, not every cycle

    def _list_requests(self, pin):
        requests_dir = pin['paths']['requests']
        self._recheck(pin)
        names = os.listdir(pin['fds']['requests']) if DIR_FD else os.listdir(requests_dir)
        seen, seen_names, fresh, held = set(), set(), [], []
        handled = self.handled[ACTOR]
        for name in names:
            if not name.endswith('.json') or name in IGNORED_NAMES or name.startswith('~$'):
                continue
            try:
                info = self._entry_lstat(pin, name)
            except OSError:
                continue
            key = (name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            seen.add(key)
            seen_names.add(name)
            if key in handled:
                continue
            (held if name in self.waiting else fresh).append({'name': name, 'key': key, 'info': info})
        handled &= seen
        self.retrying[ACTOR] = {k: v for k, v in self.retrying[ACTOR].items() if k in seen}
        self.waiting = {k: v for k, v in self.waiting.items() if k in seen_names}
        fresh.sort(key=lambda e: e['info'].st_mtime_ns)
        held.sort(key=lambda e: e['info'].st_mtime_ns)
        return fresh[:MAX_PER_CYCLE] + held      # held files never use up the cycle's budget

    # -- one request --------------------------------------------------------

    @staticmethod
    def _static_refusal(entry):
        """A refusal that cannot be a half-written file: the name, or what kind of directory entry it is."""
        name, info = entry['name'], entry['info']
        if not DUET_REQUEST_NAME.fullmatch(name):
            return RelayMalformed('invalid', 'Request filenames must end in .json and use only safe characters.')
        if name[:-5].upper() in RESERVED_STEMS:
            return RelayMalformed('invalid', 'Request filename must not be a reserved Windows device name.')
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return RelayMalformed('invalid', 'Request file failed a safety check: links and non-files are refused.')
        if info.st_nlink != 1:
            return RelayMalformed('invalid', 'Request file failed a safety check: shared (hard-linked) files are refused.')
        return None

    def _read_request(self, pin, entry):
        name, pinned = entry['name'], entry['info']
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

    def _read_response(self, pin, response_name):
        """The parsed body of responses/<response_name> if it is a plain relay-written JSON object, else None."""
        self._recheck(pin)
        try:
            if DIR_FD:
                fd = os.open(response_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pin['fds']['responses'])
            else:
                path = pin['paths']['responses'] / response_name
                if os.path.islink(path):
                    return None
                fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > PRESCAN_LIMIT:
                return None
            raw = os.read(fd, PRESCAN_LIMIT + 1)
        finally:
            os.close(fd)
        try:
            body = json.loads(raw.decode('utf-8'))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _answer_invalid(self, pin, entry, request_id, exc, digest, response_name=None):
        """Answer a file as resp-invalid-<stem>.json (or `response_name`), once: the same file and content already
        answered under that name is skipped."""
        name = entry['name']
        stem = name[:-5] if name.endswith('.json') else name
        response_name = response_name or RESPONSE_PREFIX + 'invalid-' + _sanitize_filename(stem) + '.json'
        prior = self._read_response(pin, response_name)
        if prior is not None and prior.get('request_file') == name and prior.get('request_sha256') == digest:
            self._log(ACTOR, request_id, None, None, True, 'already_answered')
            return None
        body = self._error_body(request_id, exc.code, exc.message)
        body['request_file'] = name
        body['request_sha256'] = digest
        written = self._respond(pin, response_name, body)
        self._log(ACTOR, request_id, None, None, False, exc.code)
        return None if written else HOLD

    def _unreadable(self, pin, entry, exc, digest):
        """A file that does not (yet) read as a request: hold it until it has stayed unchanged for SETTLE_SECONDS."""
        name, info = entry['name'], entry['info']
        sig = (info.st_size, info.st_mtime_ns)
        now = self.clock()
        seen = self.waiting.get(name)
        if seen is None or seen[0] != sig:
            self.waiting[name] = (sig, now)
            self._log(ACTOR, None, None, None, False, 'held_unreadable')
            return HOLD
        if now - seen[1] < SETTLE_SECONDS:
            return HOLD
        self.waiting.pop(name, None)
        return self._answer_invalid(pin, entry, None, exc, digest)

    def _task_for(self, client, payload):
        """The visible task a payload names, or None when the payload names none (the room refuses that itself).
        Raises CrewError('forbidden') for a task this seat cannot see."""
        if type(payload.get('task_id')) is not str:
            return None
        return CrewClient.task_in(client.state(), payload['task_id'])

    def _check_recipient(self, client, payload):
        """ack/claim/renew/finish/fail/wait_decision: only on a task addressed to duet. Nothing is sent otherwise."""
        task = self._task_for(client, payload)
        if task is not None and task.get('recipient') != ACTOR:
            raise CrewError('not_yours', 'This task is not addressed to duet.')

    def _check_party(self, client, payload):
        """stage_text/view_text/receipt/task_view: only on a task whose recipient or sender is duet."""
        task = self._task_for(client, payload)
        if task is not None and ACTOR not in (task.get('recipient'), task.get('sender')):
            raise CrewError('not_yours', 'This task is not addressed to or from duet.')

    def _handle_request(self, actor, client, pin, entry):
        name = entry['name']
        refusal = self._static_refusal(entry)
        if refusal is not None:
            self.waiting.pop(name, None)
            return self._answer_invalid(pin, entry, None, refusal, None)
        digest = None
        try:
            data = self._read_request(pin, entry)
            digest = hashlib.sha256(data).hexdigest()
            obj = self._parse_bytes(data)
        except RelayMalformed as exc:
            return self._unreadable(pin, entry, exc, digest)
        self.waiting.pop(name, None)
        request_id, kind = obj['request_id'], obj['kind']
        if request_id.lower().startswith(RESERVED_ID_PREFIXES):
            return self._answer_invalid(pin, entry, request_id, RelayMalformed(
                'invalid', 'request_id must not start with invalid- or qmark-: those names are reserved.'), digest)
        if self._key_like(request_id):
            return self._answer_invalid(pin, entry, None, RelayMalformed(
                'invalid', 'The request_id looks like a key (a long random-looking run of letters and digits) and is '
                           'refused. Use a short readable id and a new file name.'), digest)
        sanitized = _sanitize_filename(request_id)
        response_name = RESPONSE_PREFIX + sanitized + '.json'
        prior = self._read_response(pin, response_name)
        if prior is not None:
            if prior.get('request_sha256') == digest and prior.get('request_id', request_id) == request_id:
                self._log(actor, request_id, kind, None, True, 'duplicate')
                return None
            return self._answer_invalid(pin, entry, request_id, RelayMalformed(
                'request_id_reused', 'This request_id was already used for different content; the first answer stands. '
                                     'Use a new request_id and a new file name.'), digest,
                response_name=RESPONSE_PREFIX + sanitized + '--reused-' + digest[:8] + '.json')
        action = None
        result = None
        ok = False
        error_code = None
        message = None
        try:
            if kind == 'command':
                action = obj['action']
                if action in OWNER_ACTIONS:
                    error_code, message = 'forbidden', 'Owner actions are not permitted through the relay.'
                elif action not in COMMAND_ACTIONS:      # only `review` is left: duet never reviews
                    error_code, message = 'forbidden', 'duet never reviews.'
                else:
                    payload = _task_payload(client, obj['payload'])
                    if action in TASK_BOUND:
                        self._check_recipient(client, payload)
                    result = client.command(action, payload, request_id)
                    ok = True
            elif kind == 'handoff':
                action = obj['operation']
                payload = _task_payload(client, obj['payload'])
                self._check_party(client, payload)
                result = handoff_view(action, client.handoff(action, payload))
                ok = True
            else:   # state
                fresh, fresh_error = self._fetch_state(client)
                read, refused = [], []
                if fresh is not None:
                    read, refused = self._mark_questions_read(actor, client, fresh)
                    if read:
                        fresh, fresh_error = self._fetch_state(client)
                # DUET-FIX1 F1: once a question is marked read the request has done something in the room, so no
                # answer after that may say performed false. A later step that fails gets the partial answer instead.
                where = (pin, response_name, request_id, kind, name, digest, read, refused)
                if fresh is None:
                    if read:
                        return self._partial(*where, reason='error')
                    error_code, message = fresh_error, 'State refresh failed; nothing was written.'
                else:
                    try:
                        written, left_out, why = self._mirror_outcome(pin, fresh)
                    except BlockedSecret:
                        self._log(actor, request_id, kind, None, False, 'blocked_secret')
                        if read:
                            return self._partial(*where, reason='blocked')
                        return self._blocked(pin, response_name, request_id, kind, name, digest)
                    if written:
                        result = {'written': True, 'mirror': 'mirror/board.json'}
                        if read or refused:
                            result['questions_read'] = read
                            if refused:
                                result['questions_not_marked'] = refused
                        if left_out:
                            result['items_left_out'] = left_out
                        ok = True
                    elif read:
                        return self._partial(*where, reason=why)
                    else:
                        error_code, message = 'io', 'The board snapshot could not be written to disk.'
        except CrewError as exc:
            if exc.code == 'unavailable' and self._hold_for_retry(actor, entry):
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
        # A state answer that is ok wrote the mirror (and may have marked questions): if that answer itself is withheld,
        # it says performed true, like a command's (DUET-FIX1 F1).
        written = self._respond(pin, response_name, body, performed=ok)
        self._log(actor, request_id, kind, action, ok, None if ok else error_code)
        return None if written else HOLD

    def _partial(self, pin, response_name, request_id, kind, name, digest, read, refused, reason):
        """DUET-FIX1 F1: a `state` request marked questions read (a room write), then could not publish the mirror.
        The answer says what was done (the question ids), that mirror/board.json was not written, why (blocked: the
        final guard refused it; io: the write failed; error: anything else, such as the board not readable again) and
        how to retry. It carries no text from the board, so nothing the filter refused can ride along. If even this
        answer is refused, a shorter one with only the count is tried; it never says performed false."""
        why = PARTIAL_REASONS.get(reason, PARTIAL_REASONS['error'])
        envelope = {'request_id': request_id, 'kind': kind, 'request_file': name, 'request_sha256': digest}
        body = dict(envelope, ok=False, error=reason if reason in PARTIAL_REASONS else 'error', performed=True,
                    partial=True, done={'questions_read': list(read)}, mirror_written=False,
                    message='The room marked %d question(s) read for this request, but mirror/board.json was not '
                            'written: %s. The marks stay done.' % (len(read), why), retry=PARTIAL_RETRY)
        if refused:
            body['done']['questions_not_marked'] = refused
        self._log(ACTOR, request_id, kind, None, False, 'partial_' + body['error'])
        try:
            written = self._emit_response(pin, response_name, body)
        except BlockedSecret:
            self._log(ACTOR, request_id, kind, None, False, 'blocked_secret')
            short = dict(envelope, ok=False, error=body['error'], performed=True, partial=True,
                         done={'questions_read_count': len(read)}, mirror_written=False, message=PARTIAL_SHORT,
                         retry=PARTIAL_RETRY)
            try:
                written = self._emit_response(pin, response_name, short)
            except BlockedSecret:
                self._log(ACTOR, request_id, kind, None, False, 'blocked_secret')
                return None
        return None if written else HOLD

    def _blocked(self, pin, response_name, request_id, kind, name, digest):
        body = {'ok': False, 'error': 'blocked', 'performed': False, 'message': BLOCKED_MESSAGE,
                'request_id': request_id, 'kind': kind, 'request_file': name, 'request_sha256': digest}
        try:
            written = self._emit_response(pin, response_name, body)
        except BlockedSecret:
            self._log(ACTOR, request_id, kind, None, False, 'blocked_secret')
            return None
        return None if written else HOLD


def load_config(path):
    """crew/duet.json -> (folder, mirror_every, tmp_dir). Raises RelayError (exit 2) with a plain message."""
    try:
        path = Path(path)
        if path.stat().st_size > 4096:
            raise RelayError('config', str(path) + ' is too large for a duet config.')
        config = json.loads(path.read_bytes().decode('utf-8'))
    except RelayError:
        raise
    except (OSError, ValueError) as exc:
        raise RelayError('config', 'The duet config ' + str(path) + ' is missing or is not valid JSON.') from exc
    if not isinstance(config, dict) or not set(config) <= {'folder', 'mirror_every', 'tmp_dir'}:
        raise RelayError('config', 'The duet config may only hold folder, mirror_every and tmp_dir.')
    folder = config.get('folder')
    if type(folder) is not str or not folder.strip():
        raise RelayError('config', 'The duet config needs "folder": the path of the DuetRelay folder.')
    every = config.get('mirror_every', MIRROR_EVERY)
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not 1 <= every <= 86400:
        raise RelayError('config', '"mirror_every" must be a number of seconds from 1 to 86400.')
    tmp = config.get('tmp_dir')
    if tmp is not None and (type(tmp) is not str or not tmp.strip()):
        raise RelayError('config', '"tmp_dir" must be a folder path.')
    return folder, float(every), tmp


def run_loop(relay, interval, sleep=time.sleep):
    """Cycle forever. A relay whose folders were replaced (not linked) asks for a restart: exit 3, and the keepalive
    window starts it again so it pins the folders as they are now."""
    while True:
        try:
            relay.process_once()
        except Exception as exc:
            print(json.dumps({'fault': type(exc).__name__}), file=sys.stderr, flush=True)
        if relay.restart_wanted:
            print('common_room.duet_relay: a DuetRelay folder was replaced by another plain folder; restarting to '
                  'pin the new one.', file=sys.stderr, flush=True)
            raise SystemExit(3)
        sleep(interval)


def main(argv=None):
    package = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, default=package / 'crew' / 'duet.json')
    parser.add_argument('--root', type=Path, default=package / '.runtime' / 'handoffs')
    parser.add_argument('--interval', type=float, default=5.0)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    try:
        folder, mirror_every, tmp_dir = load_config(args.config)
    except RelayError as exc:
        print('common_room.duet_relay: ' + exc.message, file=sys.stderr)
        raise SystemExit(2)
    lock = DuetRelayLock(args.root)
    if not lock.acquire():
        if lock.reason == 'linked':
            print('common_room.duet_relay: ' + str(lock.path) + ' is not a plain file (a link, junction or shared '
                  'file). No duet relay started. Tell Claude; do not delete anything by hand.', file=sys.stderr)
        else:
            print('common_room.duet_relay: another duet relay is already running for this room.', file=sys.stderr)
        raise SystemExit(2)
    try:
        relay = DuetRelay(args.root, folder, tmp_dir=tmp_dir, mirror_every=mirror_every)
    except (RelayError, RelayFault) as exc:
        lock.release()
        print('common_room.duet_relay: ' + exc.message, file=sys.stderr)
        raise SystemExit(2)
    try:
        if args.once:
            relay.process_once()
            return
        run_loop(relay, args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


if __name__ == '__main__':
    main()
