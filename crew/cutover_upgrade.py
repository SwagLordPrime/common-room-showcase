#!/usr/bin/env python3
"""crew/cutover_upgrade.py: Shane's one switch-over command for the Common Room upgrade (core-cutover).

Run through crew\\cutover-upgrade.cmd, from the integration tree (tree-next). It moves the LIVE tree's branch to
the tip of the upgrade branch, runs the full test suite there and, if anything fails, puts everything back the way
it was. Nothing in this file touches a room or a database. Under the live `.runtime\\` folder the before and after
check lists names and opens only the `*-credential.json` files, read-only, to hash them (the hashes stay in record.json;
no key or hash is ever printed); the enrollment file below is the only other one it opens. One more file there is copied out and, on a rollback, read and perhaps put back:
`handoffs\\enrollment.json` (turnons integration, rulings #1214/#1215; SW3-RB). A started new crew that only appended
seats or scope members to it has already granted them in room.sqlite, and the old code (with the seat-dot start check)
refuses the older file then, so that growth is kept; any other change, or a change with no new crew started, is undone
from the copy saved before the switch, before the old crew starts, and only if it differs. The outcome is written to
enrollment-rollback.json in the backup folder. Nothing else under `.runtime\\` is ever written. `git clean` is never run, and no
untracked file outside the baseline manifest is ever deleted or overwritten.

What a real run does, in order:
  0. Refuses (nothing changed, the crew keeps running) unless the live tree is a git worktree root on its expected
     branch, the upgrade tip descends from the baseline commit, the tip tracks nothing under `.runtime\\`, and no
     file of the tip would overwrite an untracked file outside the manifest. Shows what it will do; needs YES.
  1. Stops the crew (Stop Common Room Crew.cmd) and waits until its windows are gone.
  2. Checks every BASELINE_MANIFEST path in the live tree against its hash. Any difference aborts here with the
     list, changing nothing, and starts the old crew again.
  3. Records the live HEAD, the branch, `git status`, a hash of every live file (not `.runtime`, `.git`, `.checks`,
     `__pycache__`), a name/size/time list of `.runtime` and a hash of each `.runtime\\*-credential.json`.
  4. Copies every file the switch could change to a timestamped folder OUTSIDE the tree and verifies each copy by
     hash, and saves `.runtime\\handoffs\\enrollment.json` there as runtime-enrollment.json (kept after a success; it
     is small and named in record.json and the last message; a rollback puts it back unless the started new crew only
     added to it). A failed copy aborts with nothing changed.
  5. Switches: `git reset --mixed <tip>` (moves the branch and the index, not the files), `git checkout-index -f -a`
     (writes the tip's files), then removes the tracked files the tip deleted.
  6. Runs the full suite (crew\\windows-tests.cmd, output into the backup folder). A new crew that does not start and
     verify as running after a passing suite counts as a failure too: whatever it launched is stopped and
     proven gone first (if that cannot be proven the tree is left alone and the message says what to do), then it rolls back.
     Any failure that is not named in
     crew\\CUTOVER_ALLOWED.txt (empty by default), a crashed or empty run, or a timeout rolls back: `git reset
     --mixed <recorded HEAD>`, the backed-up files restored and checked by hash, files the switch added removed,
     the saved enrollment.json put back if it changed (kept if the started new crew only appended to it),
     the before and after records compared (for `.runtime`: nothing that was there is missing, and the key files are
     unchanged unless the new crew was started, since every room start rewrites them), old crew started. Success
     starts the new crew. Every Start/Stop Common Room Crew.cmd call runs with CR_UNATTENDED=1 and its output in a
     temp file, so neither a pause nor the crew windows Start opens can hold the tool; Stop's stdin is NUL, Start's is
     the console (its room wait needs one), and the whole start is limited to 180 s.
  --dry-run   does every step on a scratch clone (fake `.runtime`), starts and stops nothing, touches nothing live,
              then also proves --rollback on the result.
  --rollback FOLDER   the manual undo, from a backup folder.
Exit codes: 0 switched (or dry run passed), 1 aborted with nothing changed, 2 rolled back after a failure, 3 the
rollback did not fully restore or an unexpected error stopped the run (the message says what to check), 4 refused
before anything started, 5 the tree is as the message says but the crew did NOT start (start it by hand).

Standard library only; it never imports common_room, the code being swapped.
"""
import argparse, base64, datetime, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

DEFAULT_LIVE = Path(r'D:\Work Area\Artifacts\common-room\tree')
LIVE_BRANCH = 'astra/common-room-2026-09-11'
TARGET_BRANCH = 'upgrade-2026-10'
# ROOM next uses the accepted 223eda6 baseline. The manifest lists all 2115 tracked paths
# with their committed blob hashes in both columns; generation does not inspect live files.
# Step 2 independently checks the current files against those hashes before a switch.
MANIFEST_RELATIVE = ('Artifacts', 'orchestra', 'intake-2026-10-05', 'results', 'room-next', 'build', 'BASELINE_MANIFEST_223eda6.txt')
SKIP_DIRS = frozenset(['.git', '.runtime', '.checks', '__pycache__'])    # never hashed, never copied, never deleted
BACKUP_PATH_LIMIT = 250        # characters; Windows' own limit is 260 before long paths are enabled
STOP_WAIT_SECONDS = 60
SUITE_TIMEOUT_SECONDS = 3 * 3600
EXIT_OK, EXIT_ABORTED, EXIT_ROLLED_BACK, EXIT_ROLLBACK_INCOMPLETE, EXIT_REFUSED, EXIT_CREW_NOT_STARTED = 0, 1, 2, 3, 4, 5
SHA = re.compile(r'[0-9a-f]{64}')


class Refused(Exception):
    """Preflight said no; nothing has been changed and the crew was not stopped."""


class Aborted(Exception):
    """A step before the switch failed; nothing in the live tree was changed."""


def describe(exc):
    return type(exc).__name__ + ': ' + str(exc) if str(exc) else type(exc).__name__


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def git(repo, *args, check=True):
    """One git command, bytes out. Line-ending conversion is switched off so the hashes mean what they say."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    done = subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false', '-c', 'core.longpaths=true',
                           '-C', str(repo)] + list(args), capture_output=True, env=env)
    if check and done.returncode != 0:
        raise RuntimeError('git ' + ' '.join(args) + ' failed: ' + done.stderr.decode('utf-8', 'replace').strip())
    return done


def git_text(repo, *args):
    return git(repo, *args).stdout.decode('utf-8', 'replace').strip()


def git_paths(repo, *args):
    """Paths from a `-z` listing, as forward-slash strings."""
    return set(p.decode('utf-8', 'replace') for p in git(repo, *args).stdout.split(b'\0') if p)


def inventory(root):
    """{relative path: sha256} of every file under root except the SKIP_DIRS and the worktree's own `.git` file."""
    found = {}
    root = Path(root)
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if rel == '.git' or path.is_symlink():
                continue
            found[rel] = sha256_file(path)
    return found


def runtime_listing(root):
    """{relative name: [size, mtime_ns]} for `.runtime`. Names, sizes and times only: no file is ever opened."""
    base = Path(root) / '.runtime'
    listing = {}
    if base.is_dir():
        for folder, dirs, files in os.walk(base):
            for name in files:
                path = Path(folder) / name
                stat = os.stat(path)
                listing[path.relative_to(base).as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return listing


# .runtime names whose disappearance after a rollback is not a loss (claude-room ruling #1071):
# - SQLite's journal files, but only once the new crew was started. The store keeps the default rollback journal
#   (common_room/store.py:153); a room killed by the Stop script's taskkill /F mid-write leaves a hot journal that the
#   next open of the store rolls back and deletes (-wal and -shm are the same files in WAL mode). Without a start nothing
#   opens the store, so a vanished journal then still fails.
# - Temp files, always: they are written and then renamed away (store.py:250 STOP.tmp; crew/relay_ask.py:63-66, which
#   Cowork seats keep running while the crew is down), so one caught by the snapshot can be gone a moment later.
RUNTIME_JOURNAL_SUFFIXES = ('-journal', '-wal', '-shm')
RUNTIME_TEMP_SUFFIX = '.tmp'

# The room's enrollment (turnons integration, rulings #1214 and #1215). A new crew whose code grows a scope (seat-dot,
# notify, duet) rewrites it, and its start appends the members to the grant in room.sqlite. #1243 measured that putting
# the pre-switch file back let the old code start; that was before the seat-dot start check, which every old code from
# 5d5ee83 on has (activation.py:244-253): it refuses any file whose scope lists fewer members than room.sqlite granted.
# Measured in SW3I (2026-10-31): after the candidate's start appended duet, the restored file made 5d5ee83 refuse
# (3.3); keeping the grown file (V1) or passing the shipped file plus duet (V2) let it start. So on a rollback after a
# new crew start, an append-only growth that the room's read path accepts is kept; anything else is restored.
RUNTIME_ENROLLMENT = 'handoffs/enrollment.json'      # relative to .runtime
SAVED_ENROLLMENT = 'runtime-enrollment.json'          # beside record.json in the backup folder
ENROLLMENT_OUTCOME = 'enrollment-rollback.json'       # beside record.json: kept or restored, and why


# ---- the room's enrollment read path, mirrored (SW3-RB) ---------------------------------------------------------------
# The room reads .runtime/handoffs/enrollment.json through activation.read_json (safe_path, 16384 bytes, decode_request)
# and validate_enrollment, and a start accepts it over the saved file only through merge_enrollment (activation.py:15-64,
# :102-110; protocol.py:30-58; store.py:117-133 at 5d5ee83). This tool may not import common_room, so the rules are
# copied here. Where a copy could differ, it refuses (and the caller restores): stricter, never looser.
ENROLLMENT_LEGACY = frozenset(['keeper', 'astra', 'reviewer', 'claude-estate'])
ENROLLMENT_MAX_BYTES = 16384


class NotKept(Exception):
    pass


def _room_canonical(value):
    try:
        json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise NotKept('content is not finite, valid JSON')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _room_decode(raw):
    if not isinstance(raw, bytes) or len(raw) > ENROLLMENT_MAX_BYTES:
        raise NotKept('over ' + str(ENROLLMENT_MAX_BYTES) + ' bytes')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NotKept('duplicate JSON keys')
            result[key] = value
        return result
    def constant(value):
        raise NotKept('non-finite JSON values')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise NotKept('not unambiguous UTF-8 JSON')
    if not isinstance(result, dict):
        raise NotKept('not a JSON object')
    _room_canonical(result)
    return result


def _room_text(value, limit):
    if type(value) is not str or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise NotKept('a label, session or scope text the room refuses')
    _room_canonical(value)


def _room_enrollment(value):
    """activation.validate_enrollment, copied: the same checks in the same order, NotKept for any refusal."""
    if type(value) is not dict or set(value) != {'version', 'actors', 'authorities'} or type(value['version']) is not int or value['version'] != 1:
        raise NotKept('not the enrollment schema')
    if type(value['actors']) is not list or len(value['actors']) > 24 or type(value['authorities']) is not list or len(value['authorities']) > 24:
        raise NotKept('too many rows')
    ids = set(ENROLLMENT_LEGACY); aids = {'handoffs'}
    for row in value['actors']:
        if type(row) is not dict or set(row) != {'id', 'role', 'label', 'session'}:
            raise NotKept('an actor row without exactly id, role, label and session')
        if (type(row['id']) is not str or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', row['id']) or type(row['role']) is not str
                or row['role'] not in {'owner', 'worker', 'reviewer', 'observer'}):
            raise NotKept('an actor identity the room refuses')
        if row['id'] in ids or row['role'] not in {'worker', 'reviewer', 'observer'}:
            raise NotKept('a duplicate or owner actor')
        _room_text(row['label'], 100); _room_text(row['session'], 200); ids.add(row['id'])
    for row in value['authorities']:
        if type(row) is not dict or set(row) != {'id', 'members', 'scope'}:
            raise NotKept('a scope row without exactly id, members and scope')
        aid = row['id']
        if type(aid) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', aid):
            raise NotKept('a scope id the room refuses')
        if aid in aids or aid.lower() in {x.lower() for x in aids}:
            raise NotKept('a duplicate scope')
        aids.add(aid); _room_text(row['scope'], 2000)
        members = row['members']
        if (type(members) is not list or not 1 <= len(members) <= 32 or any(type(x) is not str or x not in ids for x in members)
                or len(set(members)) != len(members) or 'keeper' not in members):
            raise NotKept('scope members the room refuses')
    if len(_room_canonical(value).encode()) > ENROLLMENT_MAX_BYTES:
        raise NotKept('over ' + str(ENROLLMENT_MAX_BYTES) + ' bytes in canonical form')
    return json.loads(_room_canonical(value))


def enrollment_growth(saved_raw, path):
    """(added actor ids, {scope id: added members}) when the file at `path` passes the room's read path (a plain file,
    no link or junction on it or its folder, one hard link, at most 16384 bytes, decode_request, validate_enrollment)
    and the room's merge_enrollment(saved, it) would accept it: every saved actor present by id and identical, every
    saved scope identical or the same id and text with the saved members as an ordered prefix; new rows allowed.
    NotKept (or any other exception) otherwise: the caller then restores."""
    path = Path(path)
    for part in (path, path.parent):
        if part.is_symlink() or part.is_junction():
            raise NotKept('a linked file or folder')
    if not path.is_file() or path.stat().st_nlink != 1:
        raise NotKept('not a plain file with one link')
    saved = _room_enrollment(_room_decode(saved_raw))
    current = _room_enrollment(_room_decode(path.read_bytes()))
    actors = {row['id']: row for row in current['actors']}
    for row in saved['actors']:
        if actors.get(row['id']) != row:
            raise NotKept('actor ' + row['id'] + ' was changed or removed')
    scopes = {row['id']: row for row in current['authorities']}
    added_members = {}
    for row in saved['authorities']:
        new = scopes.get(row['id'])
        if new == row:
            continue
        if (new is None or new['scope'] != row['scope'] or len(new['members']) <= len(row['members'])
                or new['members'][:len(row['members'])] != row['members']):
            raise NotKept('scope ' + row['id'] + ' was changed, reordered or removed')
        added_members[row['id']] = new['members'][len(row['members']):]
    saved_actor_ids = {row['id'] for row in saved['actors']}
    saved_scope_ids = {row['id'] for row in saved['authorities']}
    for row in current['authorities']:
        if row['id'] not in saved_scope_ids:
            added_members[row['id']] = list(row['members'])
    return [row['id'] for row in current['actors'] if row['id'] not in saved_actor_ids], added_members


def runtime_credentials(root):
    """{relative name: sha256} of every `*-credential.json` under `.runtime`. These are the only `.runtime` files the
    tool ever opens, read-only and only to hash them; the hashes go into record.json and are never printed or logged."""
    base = Path(root) / '.runtime'
    hashes = {}
    if base.is_dir():
        for folder, dirs, files in os.walk(base):
            for name in files:
                if name.endswith('-credential.json'):
                    path = Path(folder) / name
                    hashes[path.relative_to(base).as_posix()] = sha256_file(path)
    return hashes


def parse_manifest(path):
    """(baseline commit or None, {path: (sha256 in the integration tree, sha256 in the live tree)}) from the COPIED section."""
    text = Path(path).read_text(encoding='utf-8-sig')
    baseline, section, copied = None, None, {}
    for line in text.splitlines():
        if line.startswith('# baseline commit '):
            baseline = line.split()[3]
        if line.startswith('## '):
            section = line[3:].strip().upper()
            continue
        if not line.strip() or line.startswith('#') or section is None or not section.startswith('COPIED'):
            continue
        parts = [p.strip() for p in line.split(' | ')]
        if len(parts) >= 3 and SHA.fullmatch(parts[1]) and SHA.fullmatch(parts[2]):
            copied[parts[0].replace('\\', '/')] = (parts[1], parts[2])
    return baseline, copied


def parse_allowed(path):
    """The failing tests a real cutover may tolerate: one dotted test id (or bare test name) per line; # comments."""
    allowed = set()
    p = Path(path)
    if p.is_file():
        for line in p.read_text(encoding='utf-8-sig').splitlines():
            line = line.split('#', 1)[0].strip()
            if line:
                allowed.add(line)
    return allowed


def judge_suite(exit_code, text, allowed):
    """(ok, reasons) for one full-suite run: unittest's own output plus its exit code, read strictly."""
    marker = '=== COMBINED FINAL RESULT (reruns replace initial attempts) ==='
    markers = list(re.finditer(r'^' + re.escape(marker) + r'[ \t]*\r?$', text, re.M))
    if markers:
        if len(markers) != 1:
            return False, ['the fast suite has duplicate final summaries']
        final = text[markers[0].end():]
        # Only the coordinator's final aggregate determines a fast run. Earlier
        # failed attempts are retained for diagnosis and may have passed alone.
        matched = re.fullmatch(r'\s*Ran (\d+) tests?(?: in [0-9.]+s)?\s*\n\s*(OK|FAILED)(?: \(([^\n]*)\))?\s*', final)
        if not matched or int(matched.group(1)) == 0:
            return False, ['the fast suite final summary is missing, incomplete or empty']
        if matched.group(2) != 'OK' or exit_code != 0:
            return False, ['the fast suite final result failed (exit ' + str(exit_code) + ')']
        details = matched.group(3)
        counts = {}
        if details is not None:
            for field in details.split(','):
                detail = re.fullmatch(r'(failures|errors|skipped|expected failures|unexpected successes)=(\d+)', field.strip())
                if not detail or detail.group(1) in counts:
                    return False, ['the fast suite final details are malformed or duplicated']
                counts[detail.group(1)] = int(detail.group(2))
        if any(counts.get(label, 0) for label in ('failures', 'errors', 'unexpected successes')):
            return False, ['the fast suite final summary contradicts OK']
        return True, []
    if re.match(r'\s*Fast unittest:', text):
        return False, ['the fast suite did not report a complete final summary']
    reasons = []
    ran = re.search(r'^Ran (\d+) tests?', text, re.M)
    if not ran or int(ran.group(1)) == 0:
        return False, ['the suite did not report any tests run']
    failing = re.findall(r'^(?:FAIL|ERROR): (\S+) \(([^)]+)\)', text, re.M)
    summary = re.search(r'^FAILED \((.*)\)', text, re.M)
    expected = sum(int(n) for k, n in re.findall(r'(failures|errors)=(\d+)', summary.group(1))) if summary else 0
    # one failing test can print once per sub-test, so count distinct ids against the summary only as a floor
    if exit_code == 0 and not failing:
        return True, []
    if not failing:
        return False, ['the suite exited with code ' + str(exit_code) + ' without naming a failing test']
    if len(failing) < expected:
        reasons.append('the suite reported ' + str(expected) + ' failure(s) but only ' + str(len(failing)) + ' were named')
    for name, dotted in failing:
        if dotted not in allowed and name not in allowed and dotted.rsplit('.', 1)[-1] not in allowed:
            reasons.append('failing test not allowed by CUTOVER_ALLOWED.txt: ' + dotted)
    if exit_code == 0:
        reasons.append('the suite printed failures but exited 0')
    return (not reasons), reasons


class Cutover:
    """One switch-over (or dry run) of one live tree. Every outside effect is a callable, so tests stub them."""

    def __init__(self, live, manifest, backup_root, allowed_file, *, target=TARGET_BRANCH, live_branch=LIVE_BRANCH,
                 stop_crew, windows_gone, start_crew, run_suite, confirm, say=print, dry_run=False,
                 stop_wait=STOP_WAIT_SECONDS, sleep=time.sleep, now=time.time):
        self.live = Path(live)
        self.manifest = Path(manifest)
        self.backup_root = Path(backup_root)
        self.allowed_file = Path(allowed_file)
        self.target, self.live_branch = target, live_branch
        self.stop_crew, self.windows_gone, self.start_crew = stop_crew, windows_gone, start_crew
        self.run_suite_cb, self.confirm_cb, self.dry_run = run_suite, confirm, dry_run
        self.stop_wait, self.sleep, self.now = stop_wait, sleep, now
        self._say = say
        self.folder = None
        self.log = []
        self.check_error = None
        self.record_made = None
        self.switch_started = False
        self.new_start_attempted = False   # once the new crew was started, its room has rewritten the seats' key files
        self.rewritten_keys = []           # key files that changed for that reason, repeated in the final message
        self.extra_allowed = []      # dry run only: more tolerated failures than CUTOVER_ALLOWED.txt names

    def say(self, line=''):
        self.log.append(line)
        self._say(line)

    # ---- step 0: preflight --------------------------------------------------------------------------------------
    def preflight(self):
        live = self.live
        if not live.is_dir():
            raise Refused('The live tree ' + str(live) + ' is not a folder.')
        try:
            top = git_text(live, 'rev-parse', '--show-toplevel')
        except RuntimeError as exc:
            raise Refused(str(live) + ' is not a git worktree (' + str(exc) + ').')
        if Path(top).resolve() != live.resolve():
            raise Refused(str(live) + ' is not the root of a git worktree (git says ' + top + ').')
        branch = git_text(live, 'rev-parse', '--abbrev-ref', 'HEAD')
        if branch != self.live_branch:
            raise Refused('The live tree is on branch "' + branch + '", not "' + self.live_branch + '". Nothing was changed.')
        head = git_text(live, 'rev-parse', 'HEAD')
        tip_done = git(live, 'rev-parse', '--verify', '--quiet', self.target + '^{commit}', check=False)
        tip = tip_done.stdout.decode().strip()
        if not tip:
            raise Refused('The upgrade branch "' + self.target + '" does not exist in the live tree\'s repository.')
        if tip == head:
            raise Refused('The live tree is already at ' + tip[:7] + ', the tip of ' + self.target + '. Nothing to do.')
        if not self.manifest.is_file():
            raise Refused('The baseline manifest ' + str(self.manifest) + ' is missing.')
        baseline, copied = parse_manifest(self.manifest)
        if not copied:
            raise Refused('The baseline manifest lists no COPIED files.')
        if baseline and git(live, 'merge-base', '--is-ancestor', baseline, tip, check=False).returncode != 0:
            raise Refused('The tip of ' + self.target + ' (' + tip[:7] + ') does not contain the baseline commit ' + baseline[:7] + '.')
        h_paths = git_paths(live, 'ls-tree', '-r', '--name-only', '-z', head)
        t_paths = git_paths(live, 'ls-tree', '-r', '--name-only', '-z', tip)
        runtime = sorted(p for p in t_paths if p == '.runtime' or p.startswith('.runtime/'))
        if runtime:
            raise Refused('The tip tracks files under .runtime/ (room state and keys): ' + ', '.join(runtime[:5]) + '. Refusing.')
        clobber = sorted(p for p in t_paths if (live / p).exists() and p not in h_paths and p not in copied)
        if clobber:
            raise Refused('The tip would overwrite untracked files that are not in the baseline manifest: ' +
                          ', '.join(clobber[:10]) + (' ...' if len(clobber) > 10 else '') + '. Nothing was changed.')
        backup_root = self.backup_root.resolve()
        if backup_root == live.resolve() or live.resolve() in backup_root.parents:
            raise Refused('The backup folder must be outside the live tree.')
        # Windows stops at about 260 characters per path unless long paths are switched on: a backup copy that cannot be
        # written would only be found out after the crew is stopped, so the longest backup path is checked up front.
        longest = max((len(p) for p in (t_paths | h_paths | set(copied))), default=0)
        projected = len(str(backup_root)) + 1 + len('cutover-YYYYMMDD-HHMMSS-99') + len('\\files\\') + longest
        if projected > BACKUP_PATH_LIMIT:
            raise Refused('The longest backup path would be about ' + str(projected) + ' characters, over the ' + str(BACKUP_PATH_LIMIT) +
                          ' Windows allows without long paths enabled. Use a shorter --backup-root (now ' + str(backup_root) + '). '
                          'Nothing was changed.')
        return {'head': head, 'tip': tip, 'branch': branch, 'baseline': baseline, 'copied': copied,
                'h_paths': h_paths, 't_paths': t_paths, 'removed': h_paths - t_paths}

    # ---- the real thing -----------------------------------------------------------------------------------------
    def run(self):
        try:
            plan = self.preflight()
        except Refused as exc:
            self.say('REFUSED: ' + str(exc))
            return EXIT_REFUSED
        self.show_plan(plan)
        try:
            if not self.dry_run and not self.confirm_cb():
                self.say('Not confirmed. Nothing was changed and the crew was not touched.')
                return EXIT_ABORTED
        except KeyboardInterrupt:
            self.say('Cancelled with Ctrl+C. Nothing was changed and the crew was not touched.')
            return EXIT_ABORTED
        self.folder = self.new_folder()
        self.say('Backup folder: ' + str(self.folder))
        self.record_made = None
        self.switch_started = False
        self.new_start_attempted = False
        self.rewritten_keys = []
        try:
            return self._execute(plan)
        except (Exception, KeyboardInterrupt) as exc:   # anything not handled step by step below, Ctrl+C included
            return self._recover(exc)

    def _recover(self, exc):
        """The one place every escaped error and every Ctrl+C ends: before the switch began nothing in the live tree was
        changed, so the old crew is restarted; once it began (even half done) the switch is rolled back first."""
        what = 'Interrupted with Ctrl+C' if isinstance(exc, KeyboardInterrupt) else 'Unexpected error (' + describe(exc) + ')'
        if self.switch_started and self.record_made is not None:
            self.say(what + ' after the switch began. Rolling back.')
            return self.roll_back(self.record_made, EXIT_ROLLED_BACK)
        self.say(what + ' before the switch. NOTHING WAS CHANGED.')
        return self.restart_old(EXIT_ABORTED)

    def _execute(self, plan):
        self.say('Step 1: stopping the crew.' if not self.dry_run else 'Step 1 (dry run): the crew is not touched.')
        try:
            self.stop_crew()
            gone = self.wait_until_gone()
        except Exception as exc:                       # the stop itself failed: say so, and bring the crew back
            self.say('Stopping the crew failed: ' + describe(exc) + '. NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        if not gone:
            self.say('The crew windows were not proven closed within ' + str(self.stop_wait) + ' seconds' +
                     (' (' + describe(self.check_error) + ')' if self.check_error else '') + '. NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        try:
            bad = self.check_manifest(plan)
            if bad:
                self.say('Step 2: the live tree does not match the baseline manifest. NOTHING WAS CHANGED. Differences:')
                for line in bad[:40]:
                    self.say('  ' + line)
                return self.restart_old(EXIT_ABORTED)
            self.say('Step 2: all ' + str(len(plan['copied'])) + ' manifest files match their hashes.')
            before = self.snapshot()
            self.make_backup(plan, before)
            self.save_enrollment(plan)
        except Aborted as exc:
            self.say('Aborted: ' + str(exc) + ' NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        except Exception as exc:                       # nothing is written to the live tree before step 5, so this is safe
            self.say('Aborted by an unexpected error before the switch: ' + describe(exc) + '. NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        try:
            record = self.record(plan, before)
        except Exception as exc:
            self.say('Aborted: the backup record could not be written (' + describe(exc) + '). NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        self.record_made = record
        self.say('Step 5: switching ' + self.live_branch + ' from ' + plan['head'][:7] + ' to ' + plan['tip'][:7] + '.')
        self.switch_started = True                      # from here a failure or Ctrl+C means "roll back", even if the switch is half done
        try:
            self.switch(plan)
        except Exception as exc:                       # the switch is the one step that can leave a mess: undo it
            self.say('The switch failed (' + str(exc) + '). Rolling back.')
            return self.roll_back(record, EXIT_ROLLED_BACK)
        self.say('Step 6: running the full suite (this takes a while). Output: ' + str(self.folder / 'SUITE_OUTPUT.txt'))
        try:
            code, text = self.run_suite_cb(self.live, self.folder / 'SUITE_OUTPUT.txt')
        except (Exception, KeyboardInterrupt) as exc:   # a suite that cannot run, or Ctrl+C, is a failed suite: roll back
            code, text = 125, 'the suite could not be run or was interrupted: ' + describe(exc)
        allowed = parse_allowed(self.allowed_file) | set(self.extra_allowed)
        ok, reasons = judge_suite(code, text, allowed)
        if not ok:
            self.say('The suite FAILED: ' + '; '.join(reasons) + '. Rolling back.')
            return self.roll_back(record, EXIT_ROLLED_BACK)
        self.say('The suite passed.')
        if not self.dry_run:
            self.say('Starting the new crew. This can take up to about 90 seconds.')
        self.new_start_attempted = True
        if not self._start():
            self.say('The suite passed, but the new crew did not start or could not be verified running. A cutover whose new crew is '
                     'not running is not a success: rolling back.')
            if not self._stop_partial_crew():
                self.say('THE LIVE TREE WAS NOT ROLLED BACK: the new crew may be partly running and could not be proven stopped, and '
                         'rewriting files under a running crew is unsafe. The tree is at ' + plan['tip'][:7] + '. What to do: run '
                         '"Stop Common Room Crew.cmd" in ' + str(self.live) + ' until no window titled CRCREW remains, then run: '
                         'crew\\cutover-upgrade.cmd --rollback "' + str(self.folder) + '"')
                self.write_log()
                return EXIT_ROLLBACK_INCOMPLETE
            return self.roll_back(record, EXIT_ROLLED_BACK)
        self.say('Switched. Rollback folder: ' + str(self.folder))
        if plan.get('enrollment'):
            self.say("The room's enrollment from before the switch is kept there as " + SAVED_ENROLLMENT +
                     ' (only a rollback uses it).')
        self.write_log()
        return EXIT_OK

    def show_plan(self, plan):
        self.say('Common Room upgrade cutover' + (' (DRY RUN on a scratch copy)' if self.dry_run else ''))
        self.say('  live tree : ' + str(self.live))
        self.say('  branch    : ' + plan['branch'] + '  now ' + plan['head'][:7] + '  ->  ' + plan['tip'][:7] + ' (' + self.target + ')')
        self.say('  it will   : stop the crew; check ' + str(len(plan['copied'])) + ' baseline files by hash; back up every file the switch can change to a')
        self.say('              folder outside the tree; switch the branch; run the full test suite; on any failure put everything')
        self.say('              back and start the old crew; on success start the new crew.')
        self.say('  it will NOT: write to .runtime\\ (room state and keys; it only lists it, hashes the key files to check them'
                 ' and reads and saves enrollment.json),')
        self.say('              except to put enrollment.json back on a rollback, exactly as it was before the switch, unless')
        self.say('              the started new crew only added seats or members to it (then it is kept as the new crew left it);')
        self.say('              run git clean, or delete an untracked file.')

    def new_folder(self):
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        folder = self.backup_root / ('cutover-' + stamp)
        n = 1
        while folder.exists():
            n += 1
            folder = self.backup_root / ('cutover-' + stamp + '-' + str(n))
        (folder / 'files').mkdir(parents=True)
        return folder

    def wait_until_gone(self):
        """True once the crew windows are PROVEN gone. A check that fails or raises counts as not gone, and its error is
        kept in self.check_error for the message."""
        deadline = self.now() + self.stop_wait
        self.check_error = None
        while True:
            try:
                if self.windows_gone():
                    return True
            except Exception as exc:
                self.check_error = exc
            if self.now() >= deadline:
                return False
            self.sleep(2)

    def _start(self):
        """Start the crew and say plainly if that did not work. True only when the start was verified."""
        try:
            self.start_crew()
            return True
        except (Exception, KeyboardInterrupt) as exc:
            self.say('THE CREW DID NOT START' + (' (interrupted with Ctrl+C)' if isinstance(exc, KeyboardInterrupt) else '') + ': ' +
                     describe(exc) + '. Start it by hand: run "Start Common Room Crew.cmd" in ' + str(self.live) + '.')
            return False

    def _stop_partial_crew(self):
        """A new-crew start that failed or could not be verified may still have launched windows (the room, the relay, workers).
        They are stopped and proven gone BEFORE the tree is rolled back underneath them and before the old crew starts.
        True only when the stop ran and the windows are proven gone; anything else (an error, a check that fails, Ctrl+C)
        is False, and the caller then leaves the tree alone."""
        self.say('Stopping whatever the new crew launched before rolling back.')
        try:
            self.stop_crew()
            if self.wait_until_gone():
                return True
            self.say('The new crew\'s windows were not proven closed within ' + str(self.stop_wait) + ' seconds' +
                     (' (' + describe(self.check_error) + ')' if self.check_error else '') + '.')
        except (Exception, KeyboardInterrupt) as exc:
            self.say('Stopping the new crew failed or was interrupted: ' + describe(exc) + '.')
        return False

    def restart_old(self, code):
        self.say('Starting the old crew again.' if not self.dry_run else 'Dry run: the crew was never stopped.')
        ok = self._start()
        self.write_log()
        return code if ok else EXIT_CREW_NOT_STARTED

    def check_manifest(self, plan):
        bad = []
        for rel, (_tree_next, live_sha) in sorted(plan['copied'].items()):
            path = self.live / rel
            if not path.is_file():
                bad.append(rel + ': missing from the live tree')
            elif sha256_file(path) != live_sha:
                bad.append(rel + ': content differs from the manifest')
        return bad

    # ---- steps 3 and 4: record and back up ----------------------------------------------------------------------
    def snapshot(self):
        return {'inventory': inventory(self.live), 'runtime': runtime_listing(self.live),
                'credentials': runtime_credentials(self.live),
                'status':git(self.live, 'status', '--porcelain=v1', '-z').stdout,
                'head': git_text(self.live, 'rev-parse', 'HEAD'),
                'branch': git_text(self.live, 'rev-parse', '--abbrev-ref', 'HEAD')}

    def make_backup(self, plan, before):
        wanted = (plan['t_paths'] | plan['h_paths'] | set(plan['copied'])) & set(before['inventory'])
        self.say('Step 4: backing up ' + str(len(wanted)) + ' files outside the tree and verifying each copy by hash.')
        for rel in sorted(wanted):
            target = self.folder / 'files' / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.live / rel, target)
            if sha256_file(target) != before['inventory'][rel]:
                raise Aborted('the backup copy of ' + rel + ' does not match its original.')
        plan['backed_up'] = sorted(wanted)

    def save_enrollment(self, plan):
        """Copy .runtime/handoffs/enrollment.json out (read-only on the live side) and check the copy by hash. The crew is
        already stopped, so the file is still. No file there: nothing to save, and a rollback then puts nothing back."""
        source = self.live / '.runtime' / RUNTIME_ENROLLMENT
        if not source.is_file():
            plan['enrollment'] = None
            self.say('Step 4: no .runtime\\handoffs\\enrollment.json to save (a rollback will put none back).')
            return
        digest = sha256_file(source)
        target = self.folder / SAVED_ENROLLMENT
        shutil.copy2(source, target)
        if sha256_file(target) != digest:
            raise Aborted('the saved copy of .runtime\\handoffs\\enrollment.json does not match its original.')
        plan['enrollment'] = {'runtime_path': RUNTIME_ENROLLMENT, 'saved_as': SAVED_ENROLLMENT, 'sha256': digest}
        self.say('Step 4: saved .runtime\\handoffs\\enrollment.json as ' + str(target) + ' (a rollback puts it back, unless the '
                 'started new crew only added to it).')

    def restore_enrollment(self, record, manual=False):
        """Put the pre-switch enrollment.json back if it changed, unless a new crew was started before this rollback
        (manual, or new_start_attempted) and the file is an append-only growth of the saved copy that the room's own read
        path accepts (enrollment_growth): then it is kept as the new crew left it. Callers stop the crew first, and
        _recover may roll back after a verified new start. Returns a list of problems. A record made before this change
        has no 'enrollment' key: nothing to do. The outcome goes to enrollment-rollback.json in the backup folder."""
        saved = record.get('enrollment')
        if not saved:
            return []
        source = self.folder / saved['saved_as']
        target = self.live / '.runtime' / saved['runtime_path']
        try:
            if target.is_file() and sha256_file(target) == saved['sha256']:
                return []                                  # unchanged: not written, so nothing under .runtime moves
            if not source.is_file() or sha256_file(source) != saved['sha256']:
                return ['the saved ' + saved['saved_as'] + ' in ' + str(self.folder) + ' is missing or does not match its recorded hash']
            why = None
            if not (manual or self.new_start_attempted):
                why = 'no new crew was started before this rollback, so the change did not come from a room start'
            else:
                try:
                    live_sha = sha256_file(target)
                    added = enrollment_growth(source.read_bytes(), target)
                except Exception as exc:                   # any doubt: restore, as before this change
                    why = 'not an append-only growth the room reads (' + describe(exc) + ')'
            if why is None:
                outcome = {'version': 1, 'outcome': 'kept', 'live_sha256': live_sha, 'saved_sha256': saved['sha256'],
                           'added_actors': added[0], 'added_members': added[1]}
                written = self.write_enrollment_outcome(outcome)
                parts = (['actors ' + ', '.join(added[0])] if added[0] else []) + \
                        ['members of ' + scope + ': ' + ', '.join(names) for scope, names in sorted(added[1].items())]
                self.say('Kept .runtime\\handoffs\\enrollment.json as the new crew left it (it only adds ' + ('; '.join(parts) or 'nothing') +
                         '): the old code starts on it and would refuse the older file, since room.sqlite already granted them '
                         '(activation.py:250). ' + written)
                return []
            before_sha = sha256_file(target) if target.is_file() else None
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(target.name + '.cutover-restore')
            shutil.copy2(source, staging)
            os.replace(staging, target)
            if sha256_file(target) != saved['sha256']:
                return ['restored .runtime\\handoffs\\enrollment.json does not match its recorded hash']
        except OSError as exc:
            return ['could not restore .runtime\\handoffs\\enrollment.json: ' + str(exc)]
        written = self.write_enrollment_outcome({'version': 1, 'outcome': 'restored', 'live_sha256': before_sha,
                                                 'saved_sha256': saved['sha256'], 'why_not_kept': why})
        self.say('Restored .runtime\\handoffs\\enrollment.json from the copy saved before the switch (not kept: ' + why + '). ' + written)
        return []

    def write_enrollment_outcome(self, outcome):
        """enrollment-rollback.json beside record.json, staged then replaced; record.json is never rewritten. A failure
        here is said and is not a rollback problem. Returns the words for the log line."""
        target = self.folder / ENROLLMENT_OUTCOME
        try:
            staging = target.with_name(target.name + '.tmp')
            staging.write_text(json.dumps(outcome, indent=1, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(staging, target)
            return 'Outcome written to ' + str(target) + '.'
        except Exception as exc:
            return 'The outcome could not be written to ' + str(target) + ' (' + describe(exc) + ').'


    def record(self, plan, before):
        record = {'version': 1, 'created': datetime.datetime.now().isoformat(timespec='seconds'), 'live': str(self.live),
                  'branch': plan['branch'], 'head': plan['head'], 'target': self.target, 'tip': plan['tip'],
                  'baseline': plan['baseline'], 'backed_up': plan['backed_up'],
                  'removed': sorted(plan['removed']), 't_paths': sorted(plan['t_paths']),
                  'status_b64': base64.b64encode(before['status']).decode('ascii'),
                  'inventory': before['inventory'], 'runtime': before['runtime'],
                  'runtime_credentials': before['credentials'], 'enrollment': plan.get('enrollment')}
        (self.folder / 'record.json').write_text(json.dumps(record, indent=1, sort_keys=True), encoding='utf-8')
        return record

    # ---- step 5: the switch -------------------------------------------------------------------------------------
    def switch(self, plan):
        live = self.live
        git(live, 'reset', '--mixed', '--quiet', plan['tip'])      # the branch and the index move; no file does
        git(live, 'checkout-index', '-f', '-a')                      # every tracked file written as the tip has it
        for rel in sorted(plan['removed']):
            path = live / rel
            if path.is_file():
                path.unlink()
        dirty = [line for line in git(live, 'status', '--porcelain=v1', '-z').stdout.split(b'\0')
                 if line and not line.startswith(b'?? ')]
        if dirty:
            raise RuntimeError('after the switch these tracked paths still differ from the tip: ' +
                               ', '.join(d.decode('utf-8', 'replace') for d in dirty[:10]))

    # ---- rollback -----------------------------------------------------------------------------------------------
    def roll_back(self, record, code, manual=False):
        try:
            return self._roll_back(record, code, manual)
        except (Exception, KeyboardInterrupt) as exc:
            self.say('ROLLBACK DID NOT FULLY RESTORE THE LIVE TREE: an unexpected error stopped it (' + describe(exc) + '). The crew '
                     'was NOT started. Everything the cutover changed is in ' + str(self.folder) + ' (record.json and files\\); '
                     'fix the cause and run --rollback on that folder.')
            self.write_log()
            return EXIT_ROLLBACK_INCOMPLETE

    def _roll_back(self, record, code, manual=False):
        self.say('Rolling back: branch back to ' + record['head'][:7] + ', files restored from ' + str(self.folder) + '.')
        problems = []
        try:
            git(self.live, 'reset', '--mixed', '--quiet', record['head'])
        except RuntimeError as exc:
            problems.append(str(exc))
        files = self.folder / 'files'
        for rel in record['backed_up']:
            source, target = files / rel, self.live / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if sha256_file(target) != record['inventory'][rel]:
                    problems.append('restored ' + rel + ' does not match its recorded hash')
            except OSError as exc:
                problems.append('could not restore ' + rel + ': ' + str(exc))
        # files the switch added: tracked by the tip, absent before. Only those are deleted; nothing else is.
        for rel in record['t_paths']:
            if rel not in record['inventory'] and (self.live / rel).is_file():
                try:
                    (self.live / rel).unlink()
                except OSError as exc:
                    problems.append('could not remove ' + rel + ': ' + str(exc))
        problems += self.restore_enrollment(record, manual)   # before the check below and before the old crew starts
        problems += self.compare_with(record, strict=not manual)
        if problems:
            self.say('ROLLBACK DID NOT FULLY RESTORE THE LIVE TREE. The old crew was NOT started. Check:')
            for line in problems[:40]:
                self.say('  ' + line)
            self.say('Everything the cutover changed is in ' + str(self.folder) + ' (record.json and files\\). Fix the cause, then run: '
                     'crew\\cutover-upgrade.cmd --rollback "' + str(self.folder) + '"')
            self.write_log()
            return EXIT_ROLLBACK_INCOMPLETE
        self.say('Rollback verified: HEAD, git status and every file match the state before the cutover.')
        started = manual or self._start()
        if self.rewritten_keys:
            self.say(self.rewritten_keys_note())      # repeated as the last word, so it is not lost above
        self.write_log()
        return code if started else EXIT_CREW_NOT_STARTED

    def compare_with(self, record, strict):
        """Differences between the live tree now and the recorded state before the switch."""
        problems = []
        head = git_text(self.live, 'rev-parse', 'HEAD')
        if head != record['head']:
            problems.append('HEAD is ' + head[:7] + ', expected ' + record['head'][:7])
        now = inventory(self.live)
        extra = sorted(set(now) - set(record['inventory']))
        status = git(self.live, 'status', '--porcelain=v1', '-z').stdout
        if not strict:        # a manual undo days later: files made since are left alone, so they may show as untracked
            status = b'\0'.join(e for e in status.split(b'\0') if e.decode('utf-8', 'replace')[3:] not in extra)
            expected = b'\0'.join(e for e in base64.b64decode(record['status_b64']).split(b'\0') if e)
            status = b'\0'.join(e for e in status.split(b'\0') if e)
        else:
            expected = base64.b64decode(record['status_b64'])
        if status != expected:
            problems.append('git status differs from before the cutover')
        for rel, sha in record['inventory'].items():
            if rel not in now:
                problems.append('missing: ' + rel)
            elif now[rel] != sha:
                problems.append('content differs: ' + rel)
        if extra and strict:
            problems.append('files that were not there before: ' + ', '.join(extra[:10]))
        elif extra:
            self.say('Note: files that were not in the backup record were left alone: ' + ', '.join(extra[:10]))
        if strict:
            problems += self.compare_runtime(record)
        return problems

    def compare_runtime(self, record):
        """The .runtime check after an automatic rollback (core-cutover-fix). Sizes and times are NOT compared: a room
        changes them whenever it runs, which made the 2026-10-02 rollback report a false failure. What is checked: every
        .runtime file that existed before the cutover still exists (a vanished temp file excepted), and every
        *-credential.json is byte-identical by hash. If the new crew was started before this rollback, its room opened the
        store and rewrote the seats' key files itself (common_room/activation.py, run_renewing: each start issues fresh
        keys), so then changed key files are reported, not counted as a failure, and vanished SQLite journal files are not
        losses (RUNTIME_JOURNAL_SUFFIXES). Without a start both still fail. Names only, never a key or a hash, appear."""
        problems = []
        now = runtime_listing(self.live)
        excused = (RUNTIME_TEMP_SUFFIX,) + (RUNTIME_JOURNAL_SUFFIXES if self.new_start_attempted else ())   # ruling #1071
        missing = sorted(n for n in record['runtime'] if n not in now and not n.endswith(excused))
        for name in missing[:20]:
            problems.append('.runtime file missing (it existed before the cutover): ' + name)
        if len(missing) > 20:
            problems.append('... and ' + str(len(missing) - 20) + ' more missing .runtime files')
        before = record.get('runtime_credentials', {})
        after = runtime_credentials(self.live)
        changed = sorted(n for n, digest in before.items() if n in after and after[n] != digest)
        if changed and self.new_start_attempted:
            self.rewritten_keys = changed
            self.say(self.rewritten_keys_note())
        elif changed:
            for name in changed[:20]:
                problems.append('.runtime key file changed although no crew was started: ' + name)
        return problems

    def rewritten_keys_note(self):
        return ('Note: these .runtime key files changed because the new crew was started before the rollback, and every room '
                'start rewrites the seats\' key files with fresh keys (expected, not a failure): ' + ', '.join(self.rewritten_keys[:20]))

    def write_log(self):
        if self.folder is not None:
            try:
                (self.folder / 'cutover.log').write_text('\n'.join(self.log) + '\n', encoding='utf-8')
            except OSError:
                pass

    # ---- the manual undo ----------------------------------------------------------------------------------------
    def rollback_from(self, folder):
        folder = Path(folder)
        try:
            record = json.loads((folder / 'record.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            self.say('REFUSED: ' + str(folder) + ' is not a cutover backup folder (' + str(exc) + ').')
            return EXIT_REFUSED
        if Path(record['live']).resolve() != self.live.resolve():
            self.say('REFUSED: that backup was made for ' + record['live'] + ', not ' + str(self.live) + '.')
            return EXIT_REFUSED
        branch = git_text(self.live, 'rev-parse', '--abbrev-ref', 'HEAD')
        head = git_text(self.live, 'rev-parse', 'HEAD')
        if branch != record['branch'] or head != record['tip']:
            self.say('REFUSED: the live tree is at ' + branch + ' ' + head[:7] + ', but this backup undoes a switch to ' +
                     record['branch'] + ' ' + record['tip'][:7] + '.')
            return EXIT_REFUSED
        self.say('Manual rollback from ' + str(folder) + ': branch ' + branch + ' ' + head[:7] + ' back to ' + record['head'][:7] + '.')
        if not self.dry_run and not self.confirm_cb():
            self.say('Not confirmed. Nothing was changed.')
            return EXIT_ABORTED
        self.folder = folder
        try:
            self.stop_crew()
            gone = self.wait_until_gone()
        except (Exception, KeyboardInterrupt) as exc:
            self.say('Stopping the crew failed or was interrupted: ' + describe(exc) + '. NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        if not gone:
            self.say('The crew windows were not proven closed. NOTHING WAS CHANGED.')
            return self.restart_old(EXIT_ABORTED)
        code = self.roll_back(record, EXIT_OK, manual=True)
        if code == EXIT_OK:
            if not self._start():
                self.write_log()
                return EXIT_CREW_NOT_STARTED
            self.say('Rolled back. The old code is in place.')
        return code


# ---------------------------------------------------------------------------------------------------------------
# The real outside world: the crew's windows, the suite, the prompt. Tests never call these.
# ---------------------------------------------------------------------------------------------------------------

def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')}


class ControlError(Exception):
    """A real crew-control step (stop, window check, start, suite) did not do what it must. The message says why;
    `output` holds what the script printed, when there was a script."""
    def __init__(self, message, output=''):
        super().__init__(message)
        self.output = output


START_OUTPUT_LINES = 40         # how much of a failed Start's own output goes on screen and into cutover.log


def last_lines(text, count=START_OUTPUT_LINES):
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return '\n'.join('    ' + line for line in lines[-count:])


def cmd_line(*parts):
    """One command line for `cmd /d /s /c`, every part quoted and the whole thing wrapped in one more pair of quotes
    that /s strips. This is what makes a script path AND an argument that both contain spaces work (the earlier
    `cmd /c <path> <path>` form failed with 'D:\\AI' is not recognized)."""
    for part in parts:
        if '"' in str(part):
            raise ControlError('a path with a double quote in it cannot be passed to cmd: ' + str(part))
    return 'cmd /d /s /c "' + ' '.join('"' + str(p) + '"' for p in parts) + '"'


def read_text(path):
    try:
        return Path(path).read_bytes().decode('utf-8', 'replace')
    except OSError:
        return ''


def kill_tree(process):
    """Kill a script and everything it started (taskkill /T), then reap it. Never raises."""
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def run_command(parts, cwd, timeout, env=None, stdin=subprocess.DEVNULL):
    """(exit code, output text) of one script run through cmd; stdin from NUL unless the caller passes stdin=None (the
    console, for the Start script only). On a timeout the whole process tree is killed (a plain subprocess timeout only
    stops cmd, and the tests it started would keep running in the tree) and ControlError is raised, carrying the output.

    The output goes to a temporary FILE, never a pipe (core-cutover-fix): 'Start Common Room Crew.cmd' opens the crew's
    windows with `start`, and every process started that way inherits cmd's handles, so a pipe's reader waits until the
    last crew window closes, which for a running crew is never. That was the 300 s timeout at the real switch-over's
    step 7 on 2026-10-02; the script itself had finished. A file has no reader to block. The file is outside every tree
    (the temp folder), and it is removed afterwards unless a crew window still holds it open."""
    handle, name = tempfile.mkstemp(prefix='cutover-output-', suffix='.txt')
    try:
        with os.fdopen(handle, 'wb') as out_file:
            try:
                process = subprocess.Popen(cmd_line(*parts), cwd=str(cwd), env=env, stdin=stdin,
                                           stdout=out_file, stderr=subprocess.STDOUT)
            except OSError as exc:
                raise ControlError('could not start ' + str(parts[0]) + ': ' + type(exc).__name__ + ': ' + str(exc))
            try:
                # polled, not waited: a Ctrl+C lands in time.sleep at once. Popen.wait(timeout) must not be used here:
                # on a Ctrl+C it waits again for the "remaining" time, which can be negative after a short wait, and a
                # negative wait on Windows never ends; with a Start script sitting at "Terminate batch job (Y/N)?" the
                # two then wait for each other forever (MEASURED in the core-cutover-fix Ctrl+C probe).
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    time.sleep(0.2)
            except subprocess.TimeoutExpired:
                kill_tree(process)
                timed_out = True
            except BaseException:
                # Ctrl+C (or anything else) while waiting: the script must not outlive the tool. With the console as its
                # stdin, a Start script that got the same Ctrl+C would sit at "Terminate batch job (Y/N)?" and could go
                # on launching crew windows after the rollback, so its whole tree is killed before the error goes on.
                kill_tree(process)
                raise
            else:
                timed_out = False
        text = read_text(name)
        if timed_out:
            raise ControlError('timed out after ' + str(timeout) + ' seconds (its process tree was killed)', text)
        return process.returncode, text
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass                       # a crew window started with `start` may still hold it; it is only a temp file


def unattended_env():
    """The environment of every call to 'Start Common Room Crew.cmd' and 'Stop Common Room Crew.cmd': CR_UNATTENDED=1
    turns off every pause and prompt the Start script guards with it. Stop also gets stdin from NUL, so its unguarded
    last-line `pause` returns at once instead of waiting for a key."""
    return dict(os.environ, CR_UNATTENDED='1')


WINDOW_COUNT_SCRIPT = "(Get-Process | Where-Object { $_.MainWindowTitle -like 'CRCREW *' } | Measure-Object).Count"


def crew_window_count(runner=subprocess.run, timeout=60):
    """How many windows titled `CRCREW *` exist. ControlError when that cannot be established: an unknown count is never
    read as zero. (`tasklist /v` was too slow for this: it exceeded 60 s on this PC under load; this takes about a second.)"""
    try:
        done = runner(['powershell', '-NoProfile', '-NonInteractive', '-Command', WINDOW_COUNT_SCRIPT],
                      capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlError('the window check could not run (' + type(exc).__name__ + ')')
    if done.returncode != 0:
        raise ControlError('the window check failed (exit code ' + str(done.returncode) + ')')
    text = done.stdout.decode('utf-8', 'replace').strip()
    if not re.fullmatch(r'\d+', text):
        raise ControlError('the window check printed something unexpected')
    return int(text)


def real_stop(live, timeout=120):
    script = Path(live) / 'Stop Common Room Crew.cmd'

    def stop():
        if not script.is_file():
            raise ControlError(str(script) + ' is missing')
        code, out = run_command([script], live, timeout, unattended_env())
        if code != 0:
            raise ControlError('Stop Common Room Crew.cmd exited with code ' + str(code) + ': ' + out.strip()[-200:])
    return stop


def real_windows_gone(count=crew_window_count):
    """True only when the check ran and found no crew window. A failed or slow check is NOT 'gone'."""
    try:
        return count() == 0
    except ControlError:
        return False


START_LIMIT_SECONDS = 180      # the whole start, script and window check together (claude-room ruling #1055)


def with_output(message, out):
    tail = last_lines(out)
    return message + ('. Its last lines:\n' + tail if tail else '')


def real_start(live, count=crew_window_count, wait=90, sleep=time.sleep, now=time.time, timeout=START_LIMIT_SECONDS):
    """Start the crew and prove a crew window exists, all within `timeout` seconds. The script gets CR_UNATTENDED=1 and
    the CONSOLE as stdin (ruling #1055: the script's own 45 s room wait uses `timeout /t 3`, which refuses NUL and exits
    at once, cutting the wait to about 28 s); its output goes to a temp file. Every pause and prompt in the Start script
    is gated by CR_UNATTENDED, and crew\\find-python.cmd, the only script it calls, has none. On any failure the error
    carries the script's last 40 lines, for the screen and cutover.log."""
    script = Path(live) / 'Start Common Room Crew.cmd'

    def start():
        if not script.is_file():
            raise ControlError(str(script) + ' is missing')
        started = now()
        try:
            code, out = run_command([script], live, timeout, unattended_env(), stdin=None)
        except ControlError as exc:
            raise ControlError(with_output('Start Common Room Crew.cmd did not finish within ' + str(timeout) +
                                           ' seconds (its process tree was killed)', exc.output), exc.output)
        if code != 0:
            raise ControlError(with_output('Start Common Room Crew.cmd exited with code ' + str(code), out), out)
        deadline, last = min(now() + wait, started + timeout), 'no check ran'
        while True:                                       # the script returning is not proof: a crew window must exist
            try:
                if count() > 0:
                    return
                last = 'no CRCREW window yet'
            except ControlError as exc:
                last = str(exc)
            if now() >= deadline:
                raise ControlError(with_output('the start script finished but no crew window appeared within ' + str(wait) +
                                               ' seconds (' + last + ')', out), out)
            sleep(3)
    return start


def real_suite(live, output, timeout=SUITE_TIMEOUT_SECONDS, *, fast=True):
    try:
        command = [Path(live) / 'crew' / 'windows-tests.cmd', output]
        if fast:
            command.extend(['--fast', '8'])
        code, _text = run_command(command, live, timeout, clean_env())
    except ControlError as exc:
        return 124, str(exc)
    text = Path(output).read_text(encoding='utf-8', errors='replace') if Path(output).is_file() else ''
    return code, text


def ask_yes():
    if not sys.stdin.isatty():
        print('This needs a person at the keyboard to type YES; there is no flag that skips it.')
        return False
    return input('Type YES to go ahead (anything else cancels): ').strip().upper() == 'YES'


def find_estate_root(start):
    for folder in [Path(start)] + list(Path(start).parents):
        if (folder / 'Artifacts').is_dir():
            return folder
    return None


# ---------------------------------------------------------------------------------------------------------------
# --dry-run: the same steps on a scratch clone, with a fake .runtime. Nothing live is written.
# ---------------------------------------------------------------------------------------------------------------

def make_scratch(live, copied, target, scratch_root, files_from=None, baseline=None):
    """A scratch clone dressed like the live tree: at its recorded HEAD, with the baseline files laid over it and a fake
    `.runtime`. The baseline files come from the baseline COMMIT's own objects (or from --files-from), never from the
    live tree's files, so the live tree is not read at all beyond its git metadata."""
    scratch = Path(scratch_root) / 'tree'
    common = git_text(live, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    head = git_text(live, 'rev-parse', 'HEAD')
    branch = git_text(live, 'rev-parse', '--abbrev-ref', 'HEAD')
    subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false', 'clone', '--quiet', '--no-hardlinks', common,
                    str(scratch)], check=True, capture_output=True)      # autocrlf off, like the live tree, so file hashes match
    git(scratch, 'checkout', '--quiet', '-B', branch, head)
    git(scratch, 'branch', '--quiet', '-f', target, 'origin/' + target)
    for rel, (_tree_next_sha, live_sha) in copied.items():
        if files_from:
            source = Path(files_from) / rel
            data = source.read_bytes() if source.is_file() else None
        else:
            shown = git(scratch, 'show', baseline + ':' + rel, check=False) if baseline else None
            data = shown.stdout if shown is not None and shown.returncode == 0 else None
        if data is not None and hashlib.sha256(data).hexdigest() == live_sha:   # a file that is not the live version (e.g. .gitignore) keeps the clone's
            (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
            (scratch / rel).write_bytes(data)                       # written now: a copied-over old mtime can match git's stat cache and hide a change
    (scratch / '.runtime' / 'handoffs').mkdir(parents=True, exist_ok=True)
    (scratch / '.runtime' / 'handoffs' / 'fake-room.sqlite').write_bytes(b'not a real database\n')
    (scratch / '.runtime' / 'handoffs' / 'fake-credential.json').write_text('{"fake": true}\n', encoding='utf-8')
    return scratch


def dry_run(args, say):
    began = time.perf_counter()
    live = Path(args.live)
    manifest = Path(args.manifest)
    baseline, copied = parse_manifest(manifest)
    if args.scratch_root:
        Path(args.scratch_root).mkdir(parents=True, exist_ok=True)
    scratch_root = Path(tempfile.mkdtemp(prefix='cutover-dryrun-', dir=args.scratch_root or None))
    scratch = make_scratch(live, copied, args.target, scratch_root, files_from=args.files_from, baseline=baseline)
    say('Dry run: scratch copy of the live tree at ' + str(scratch))
    started = []

    def fake_suite(tree, output):
        if args.skip_suite:
            Path(output).write_text('suite skipped by --skip-suite (dry run only)\nRan 1 test\nOK\n', encoding='utf-8')
            return 0, 'Ran 1 test\nOK\n'
        return real_suite(tree, output, fast=False)

    cut = Cutover(scratch, manifest, scratch_root / 'backups', scratch / 'crew' / 'CUTOVER_ALLOWED.txt', target=args.target,
                  live_branch=args.live_branch, stop_crew=lambda: started.append('stop'), windows_gone=lambda: True,
                  start_crew=lambda: started.append('start'), run_suite=fake_suite, confirm=lambda: True,
                  say=say, dry_run=True)
    cut.extra_allowed = list(args.allow or [])
    code = cut.run()
    if code == EXIT_ROLLED_BACK:
        say('Dry run: the suite failed and the rollback was verified, which is the designed behaviour.')
    if code == EXIT_OK:
        say('Dry run: the switch and the suite passed. Now proving --rollback on the result.')
        undo = Cutover(scratch, manifest, scratch_root / 'backups', scratch / 'crew' / 'CUTOVER_ALLOWED.txt', target=args.target,
                       live_branch=args.live_branch, stop_crew=lambda: None, windows_gone=lambda: True, start_crew=lambda: None,
                       run_suite=fake_suite, confirm=lambda: True, say=say, dry_run=True)
        undo_code = undo.rollback_from(cut.folder)
        say('Dry run: manual rollback ' + ('verified.' if undo_code == EXIT_OK else 'FAILED (exit ' + str(undo_code) + ').'))
        code = EXIT_OK if undo_code == EXIT_OK else EXIT_ROLLBACK_INCOMPLETE
    say('Dry run result: exit ' + str(code) + '. The crew was never stopped or started; the live tree was only read.')
    say('Dry run elapsed: ' + format(time.perf_counter() - began, '.3f') + ' seconds.')
    say('The scratch copy is left at ' + str(scratch_root) + ' for inspection; delete it when you like.')
    return code


def guarded(action, cut, fallback_folder):
    """Run one cutover action; whatever escapes it (an unexpected error, Ctrl+C) still ends with a plain message."""
    try:
        return action()
    except (Exception, KeyboardInterrupt) as exc:
        what = 'INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'UNEXPECTED ERROR'
        print(what + ': ' + describe(exc) + '. The cutover did not finish cleanly: the crew may be stopped and the live tree may be half '
              'switched. Look at the backup folder (' + str(cut.folder or fallback_folder) + '): record.json and files\\ hold the before '
              'state; run "crew\\cutover-upgrade.cmd --rollback <that folder>" and then start the crew.')
        cut.write_log()
        return EXIT_ROLLBACK_INCOMPLETE


def build_parser():
    p = argparse.ArgumentParser(prog='cutover-upgrade', description='Switch the live Common Room tree to the upgrade '
                                'branch with automatic rollback. Run it from the integration tree (tree-next).')
    p.add_argument('--dry-run', action='store_true', help='Do every step on a scratch clone; touch nothing live.')
    p.add_argument('--rollback', metavar='FOLDER', help='Undo a cutover from its backup folder.')
    p.add_argument('--live', default=str(DEFAULT_LIVE), help='The live tree (a real run only accepts the default).')
    p.add_argument('--manifest', help='The baseline manifest (default: results/room-next/build/BASELINE_MANIFEST_223eda6.txt of intake-2026-10-05).')
    p.add_argument('--target', default=TARGET_BRANCH, help='The branch to switch to (default ' + TARGET_BRANCH + ').')
    p.add_argument('--live-branch', default=LIVE_BRANCH, help='Dry run only: the branch the live tree is expected to be on.')
    p.add_argument('--backup-root', help='Where the timestamped backup folder goes (default: cutover-backups beside the live tree).')
    p.add_argument('--skip-suite', action='store_true', help='Dry run only: skip the long test suite.')
    p.add_argument('--scratch-root', metavar='FOLDER', help='Dry run only: make the scratch copy inside this folder (default: the temp folder).')
    p.add_argument('--files-from', metavar='TREE', help='Dry run only: lay the baseline files from this folder over the scratch clone instead of from the baseline commit.')
    p.add_argument('--allow', action='append', metavar='TEST', help='Dry run only: tolerate this failing test id, like CUTOVER_ALLOWED.txt.')
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            reconfigure(encoding='utf-8', errors='replace')
    args = build_parser().parse_args(argv)
    here = Path(__file__).resolve().parent
    if args.manifest is None:
        estate = find_estate_root(here)
        args.manifest = str(estate.joinpath(*MANIFEST_RELATIVE)) if estate else ''
    if (args.skip_suite or args.allow or args.files_from or args.scratch_root or args.live_branch != LIVE_BRANCH) and not args.dry_run:
        print('--skip-suite, --allow, --files-from, --scratch-root and --live-branch exist for --dry-run only; a real cutover takes its allowed failures '
              'from crew\\CUTOVER_ALLOWED.txt.')
        return EXIT_REFUSED
    live = Path(args.live)
    if args.dry_run:
        return dry_run(args, print)
    if live.resolve() != DEFAULT_LIVE.resolve():
        print('REFUSED: a real cutover only targets ' + str(DEFAULT_LIVE) + ' (use --dry-run to rehearse on another tree).')
        return EXIT_REFUSED
    if live.resolve() in here.parents or here == live.resolve():
        print('REFUSED: run this from the integration tree (tree-next), not from the live tree it is about to switch.')
        return EXIT_REFUSED
    backup_root = Path(args.backup_root) if args.backup_root else live.parent / 'cutover-backups'
    cut = Cutover(live, args.manifest, backup_root, live / 'crew' / 'CUTOVER_ALLOWED.txt', target=args.target,
                  stop_crew=real_stop(live), windows_gone=real_windows_gone, start_crew=real_start(live),
                  run_suite=real_suite, confirm=ask_yes)
    if args.rollback:
        return guarded(lambda: cut.rollback_from(args.rollback), cut, backup_root)
    # the allowed list is read from the tip's copy after the switch (it ships with the upgrade)
    return guarded(cut.run, cut, backup_root)


if __name__ == '__main__':
    sys.exit(main())
