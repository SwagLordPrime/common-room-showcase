"""Static checks of the autonomy build's Windows-only integration (gate E2E3's companion).

Nothing here runs cmd.exe, Windows Credential Manager, the Startup folder or a scheduled task:
this container cannot. These tests READ the launcher and wrapper .cmd files, the schedule specs
and AUTONOMY_2026-09-24.md, and check what they say:

- "Start Common Room Crew.cmd": CRLF and ASCII; AGENT_SEATS set empty; every long-running
  `start "CRCREW ..."` line runs common_room.keepalive with --name and a single `--` before the
  module; the room line carries --keeper-store wincred and --plans-root "%PLANS_ROOT%"; every
  pause is guarded by CR_UNATTENDED; the :oldroom path exits 0 unattended and otherwise closes
  the CRCREW windows before killing the old PID.
- No schtasks and no Startup-folder write in any .cmd file of the tree except
  crew/autostart-install.cmd and crew/autostart-remove.cmd.
- "Stop Common Room Crew.cmd", "Start Common Room Relay.cmd", crew/agent-once.cmd and
  crew/windows-tests.cmd do what the AUTONOMY guide says.
- Every crew/schedules/*.json is still "not created"; every tree path the AUTONOMY guide names
  exists.
- crew/autostart-install.cmd and crew/autostart-remove.cmd (fix-board 2026-09-24): CONFIRM is
  cleared right before `set /p`, the typed "yes" is checked right after it and before the one
  change each script makes, and every path ends at one `pause` then `exit /b %RC%`.
- LauncherTransliterationTests: a small Python model of the launcher's control flow, written by
  reading the .cmd line by line. It is a check on READING, not on execution: it can only be as
  right as the reading, and says nothing about how cmd.exe really behaves. Each branch names the
  launcher line it mirrors, and ANCHORS pins those lines' exact text, so any edit to the launcher
  fails here until the model is read again.

TREE is a module global so a mutant driver (evidence/autonomy-2026-09-24/e2e3/mutants_static.py)
can point these tests at a mutated copy of the tree; normally it is the package root.
Fixtures (none are needed beyond the files read) would go under tests.helpers.case_dir.
"""
import fnmatch, itertools, json, ntpath, re, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets tools/evidence.py run this file as a script

from tests.helpers import ROOT

TREE = ROOT
LAUNCHER = 'Start Common Room Crew.cmd'
STOP = 'Stop Common Room Crew.cmd'
RELAY = 'Start Common Room Relay.cmd'
AGENT_ONCE = 'crew/agent-once.cmd'
WINDOWS_TESTS = 'crew/windows-tests.cmd'
AUTOSTART_WRITERS = {'crew/autostart-install.cmd', 'crew/autostart-remove.cmd'}
GUIDE = 'AUTONOMY_2026-09-24.md'
TREE_ABS = 'D:\\Work Area\\Artifacts\\common-room\\tree'
TOKEN = re.compile(r'"[^"]*"|\S+')


# -- reading .cmd files ------------------------------------------------------------------------
def raw(rel):
    return (TREE / rel).read_bytes()


def lines(rel):
    """The file's lines, split on CRLF only (a lone LF stays inside a line and fails the CRLF test)."""
    data = raw(rel).decode('ascii')
    parts = data.split('\r\n')
    if parts and parts[-1] == '':
        parts.pop()
    return parts


def is_comment(line):
    stripped = line.strip().lower()
    return stripped.startswith('rem ') or stripped == 'rem' or stripped.startswith('::')


def code(rel):
    """(1-based line number, stripped text) for every non-comment, non-blank line."""
    return [(n, line.strip()) for n, line in enumerate(lines(rel), 1) if line.strip() and not is_comment(line)]


def settings(rel):
    """NAME -> value for every `set "NAME=value"` line (the last one wins, as in cmd)."""
    found = {}
    for _, line in code(rel):
        match = re.fullmatch(r'set "([A-Z_]+)=(.*)"', line)
        if match:
            found[match[1]] = match[2]
    return found


def expand(text, values):
    return re.sub(r'%([A-Z_]+)%', lambda m: values.get(m[1], m[0]), text)


def all_cmd_files():
    found = []
    for path in TREE.rglob('*.cmd'):
        rel = path.relative_to(TREE).as_posix()
        if rel.split('/')[0] in ('.git', '.checks'):
            continue
        found.append(rel)
    return sorted(found)


CRCREW_START = re.compile(r'(?:if defined AGENT_SEATS for %%S in \(%AGENT_SEATS%\) do |if exist "crew\\notify\.json" |if exist "crew\\duet\.json" )?'
                          r'start "CRCREW (?P<title>[^"]+)" /min "%PY%" (?P<rest>.+)')   # notify: its guard, 2026-10-05; duet: its guard, 2026-10-30


def crcrew_starts(rel):
    """(line number, title, keepalive argv, child argv) for every `start "CRCREW ..."` line."""
    starts = []
    for n, line in code(rel):
        if 'start "CRCREW' not in line:
            continue
        match = CRCREW_START.fullmatch(line)
        if not match:
            raise AssertionError('%s:%d is a CRCREW start line in an unexpected form: %s' % (rel, n, line))
        tokens = TOKEN.findall(match['rest'])
        starts.append((n, match['title'], tokens))
    return starts


class CmdFormatTests(unittest.TestCase):
    NAMED = (LAUNCHER, STOP, RELAY, AGENT_ONCE, WINDOWS_TESTS)

    def test_every_cmd_file_is_crlf_and_ascii(self):
        files = all_cmd_files()
        for name in self.NAMED:
            self.assertIn(name, files)
        for rel in files:
            data = raw(rel)
            with self.subTest(rel):
                self.assertTrue(all(b < 128 for b in data), rel + ' has a non-ASCII byte')
                self.assertEqual(data.count(b'\n'), data.count(b'\r\n'), rel + ' has a bare LF')
                self.assertEqual(data.count(b'\r'), data.count(b'\r\n'), rel + ' has a bare CR')
                self.assertTrue(data.endswith(b'\r\n'), rel + ' does not end with CRLF')


class LauncherTests(unittest.TestCase):
    def test_agent_seats_is_set_empty_and_nowhere_else(self):
        sets = [(n, line) for n, line in code(LAUNCHER) if re.match(r'set\s+"?AGENT_SEATS=', line, re.I)]
        self.assertEqual([line for _, line in sets], ['set "AGENT_SEATS="'])

    def test_local_b_gets_no_key_and_no_window_but_stays_enrolled(self):    # seat-truth, D9
        import importlib.util
        seats = settings(LAUNCHER)['SEATS'].split()
        self.assertNotIn('local-b', seats)
        self.assertIn('local-a', seats)
        self.assertEqual([t for _, t, _ in crcrew_starts(LAUNCHER) if t == 'local-b'], [])
        self.assertFalse([line for _, line in code(LAUNCHER) if 'CRCREW local-b' in line])
        self.assertTrue((TREE / 'crew' / 'local-b.json').is_file())
        shipped = json.loads((TREE / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8'))
        self.assertIn('local-b', [a['id'] for a in shipped['actors']])
        # The launcher's enrollment check accepts a SEATS list that leaves an enrolled seat out.
        spec = importlib.util.spec_from_file_location('enroll_check', str(TREE / 'crew' / 'enroll_check.py'))
        enroll_check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(enroll_check)
        self.assertEqual(enroll_check.decide(None, shipped, seats)[0], 3)
        self.assertEqual(enroll_check.decide(shipped, shipped, seats)[0], 0)

    def test_every_long_running_start_runs_under_keepalive_with_one_separator(self):
        from common_room import keepalive
        expected = {'room': 'common_room.activation', 'relay': 'common_room.relay',
                    'local-a': 'common_room.ollama_worker',
                    'agent-%%S': 'common_room.agent_worker', 'notify': 'common_room.notify',
                    'duet': 'common_room.duet_relay'}
        seen = {}
        for rel in (LAUNCHER, RELAY):
            for n, title, tokens in crcrew_starts(rel):
                with self.subTest(file=rel, line=n):
                    self.assertEqual(tokens[:5], ['-X', 'utf8', '-B', '-m', 'common_room.keepalive'])
                    self.assertEqual(tokens.count('--'), 1, 'exactly one -- separator')
                    own, child = keepalive._split_argv(tokens[5:])
                    self.assertEqual(own[0], '--name')
                    self.assertEqual(len(own), 2, 'keepalive takes only --name here: %s' % own)
                    self.assertEqual(keepalive.build_parser().parse_args(own).name, title.replace(' ', '-'))
                    self.assertEqual(child[:4], ['-X', 'utf8', '-B', '-m'])
                    module = child[4]
                    self.assertTrue((TREE / (module.replace('.', '/') + '.py')).is_file(), module)
                    self.assertEqual(module, expected[own[1]])
                    # Every flag given to the child is one its own module declares.
                    source = (TREE / (module.replace('.', '/') + '.py')).read_text(encoding='utf-8')
                    for flag in [t for t in child[5:] if t.startswith('--')]:
                        self.assertIn("'%s'" % flag, source, '%s does not declare %s' % (module, flag))
                    seen.setdefault(rel, []).append(own[1])
        self.assertEqual(seen[LAUNCHER], ['room', 'relay', 'local-a', 'notify', 'duet', 'agent-%%S'])   # local-b out (D9)
        self.assertEqual(seen[RELAY], ['relay'])
        # No launched module runs outside keepalive (the one bare room line is an echo for Shane).
        for rel in (LAUNCHER, RELAY):
            for n, line in code(rel):
                if re.search(r'-m common_room\.(activation|relay|ollama_worker|agent_worker|notify|duet_relay)\b', line):
                    self.assertTrue(line.startswith('echo ') or 'common_room.keepalive' in line,
                                    '%s:%d runs a crew program outside keepalive' % (rel, n))

    def test_room_line_has_owner_vault_and_plans_root(self):
        values = settings(LAUNCHER)
        self.assertIn('--keeper-store wincred', values['ROOMARGS'])
        # The guide's orchestra folder is where PLANS_ROOT points from the tree (%~dp0 = the tree
        # folder with its trailing backslash).
        self.assertEqual(ntpath.normpath(values['PLANS_ROOT'].replace('%~dp0', TREE_ABS + '\\')),
                         'D:\\Work Area\\Artifacts\\orchestra')
        self.assertIn('D:\\Work Area\\Artifacts\\orchestra\\', (TREE / GUIDE).read_text(encoding='utf-8'))
        room = [(n, title, tokens) for n, title, tokens in crcrew_starts(LAUNCHER) if title == 'room']
        self.assertEqual(len(room), 1)
        n, _, tokens = room[0]
        line = dict(code(LAUNCHER))[n]
        self.assertIn('%ROOMARGS% --plans-root "%PLANS_ROOT%"', line)
        expanded = TOKEN.findall(expand(' '.join(tokens), values))
        child = expanded[expanded.index('--') + 1:]
        self.assertEqual(child[child.index('--keeper-store') + 1], 'wincred')
        self.assertEqual(child[child.index('--plans-root') + 1], '"%s"' % values['PLANS_ROOT'])
        self.assertIn('--headless', child)
        self.assertIn('--renew', child)
        self.assertEqual(tokens[-2:], ['%ENROLL%', '%*'], 'the explicit arguments pass through last')

    def test_every_pause_and_choice_waits_only_when_attended(self):
        pauses = [(n, line) for n, line in code(LAUNCHER) if re.search(r'\bpause\b', line, re.I)]
        self.assertGreaterEqual(len(pauses), 5)
        for n, line in pauses:
            self.assertEqual(line, 'if not defined CR_UNATTENDED pause', 'line %d' % n)
        choices = [n for n, line in code(LAUNCHER) if re.match(r'choice\b', line, re.I)]
        block = self.block(':oldroom')
        self.assertEqual(len(choices), 1)
        self.assertIn(choices[0], [n for n, _ in block])
        unattended_exit = next(n for n, line in block if line == 'exit /b 0')
        self.assertLess(unattended_exit, choices[0])
        self.assertFalse(any(re.search(r'\bset\s+/p\b', line, re.I) for _, line in code(LAUNCHER)))

    def block(self, label):
        rows = code(LAUNCHER)
        start = next(i for i, (_, line) in enumerate(rows) if line == label)
        end = next((i for i in range(start + 1, len(rows)) if rows[i][1].startswith(':')), len(rows))
        return rows[start + 1:end]

    def test_oldroom_exits_0_unattended_and_closes_crew_windows_before_the_old_pid(self):
        block = self.block(':oldroom')
        text = [line for _, line in block]
        self.assertEqual(text[0], 'if defined CR_UNATTENDED (')
        close = text.index(')')
        self.assertEqual(text[close - 1], 'exit /b 0')
        self.assertFalse(any('taskkill' in line for line in text[:close]))
        kill_windows = next(i for i, line in enumerate(text) if line.startswith('taskkill') and 'CRCREW *' in line)
        kill_pid = next(i for i, line in enumerate(text) if line.startswith('taskkill') and '%OLDPID%' in line)
        self.assertLess(close, kill_windows)
        self.assertLess(kill_windows, kill_pid)
        for index in (kill_windows, kill_pid):
            self.assertIn('/F', text[index].split())
            self.assertIn('/T', text[index].split())
        self.assertIn('/FI "WINDOWTITLE eq CRCREW *"', text[kill_windows])
        choice = next(i for i, line in enumerate(text) if line.startswith('choice'))
        self.assertLess(choice, kill_windows)
        self.assertEqual(text[choice + 1], 'if errorlevel 2 exit /b 1')
        self.assertEqual(text[-1], 'goto startroom')

    def test_no_schtasks_or_startup_folder_write_outside_the_autostart_scripts(self):
        startup = re.compile(r'programs\\startup|shell:startup|start menu|schtasks', re.I)
        for rel in all_cmd_files():
            if rel in AUTOSTART_WRITERS:
                continue
            for n, line in enumerate(lines(rel), 1):
                self.assertIsNone(startup.search(line), '%s:%d names schtasks or the Startup folder' % (rel, n))
        for rel in sorted(AUTOSTART_WRITERS):
            for n, line in code(rel):
                self.assertNotRegex(line, re.compile(r'schtasks', re.I), '%s:%d calls schtasks' % (rel, n))
        # crew/autostart.cmd itself only sets CR_UNATTENDED and starts the launcher.
        auto = [line for _, line in code('crew/autostart.cmd')]
        self.assertIn('set "CR_UNATTENDED=1"', auto)
        self.assertIn('start "Common Room autostart" /min cmd /c ""%~dp0..\\Start Common Room Crew.cmd""', auto)


class OtherScriptTests(unittest.TestCase):
    def test_stop_script_force_kills_the_crcrew_window_trees(self):
        kills = [line for _, line in code(STOP) if line.lower().startswith('taskkill')]
        self.assertEqual(len(kills), 1)
        words = TOKEN.findall(kills[0])
        self.assertIn('/F', words)
        self.assertIn('/T', words)
        self.assertEqual(words[words.index('/FI') + 1], '"WINDOWTITLE eq CRCREW *"')

    def test_relay_script_runs_the_relay_under_keepalive_for_the_launcher_seats(self):
        starts = crcrew_starts(RELAY)
        self.assertEqual([title for _, title, _ in starts], ['relay'])
        tokens = starts[0][2]
        self.assertIn('common_room.keepalive', tokens)
        child = tokens[tokens.index('--') + 1:]
        self.assertEqual(child[4:], ['common_room.relay', '--actors', '%RELAY_SEATS%'])
        self.assertEqual(settings(RELAY)['RELAY_SEATS'], settings(LAUNCHER)['RELAY_SEATS'])

    def wrapper(self, rel, run_line):
        rows = [line for _, line in code(rel)]
        find = next(i for i, line in enumerate(rows) if line.startswith('call "%~dp0find-python.cmd"'))
        run = rows.index(run_line)
        self.assertLess(find, run)
        self.assertIn('cd /d "%~dp0.."', rows[:find])
        self.assertEqual(rows[run + 1], 'set "RC=%errorlevel%"')
        self.assertIn('exit /b %RC%', rows)
        self.assertTrue((TREE / 'crew' / 'find-python.cmd').is_file())

    def test_agent_once_runs_one_runner_cycle_for_the_named_seat(self):
        self.wrapper(AGENT_ONCE, '"%PY%" -X utf8 -B -m common_room.agent_worker --config "crew\\agent-%~1.json" --once')
        self.assertTrue((TREE / 'common_room' / 'agent_worker.py').is_file())
        self.assertIn('if not exist "crew\\agent-%~1.json" goto noconfig', [line for _, line in code(AGENT_ONCE)])

    def test_windows_tests_runs_the_whole_suite_into_one_file(self):
        self.wrapper(WINDOWS_TESTS, '"%PY%" -X utf8 -B -m unittest discover -s tests -t . -p "%PAT%" > "%OUT%" 2>&1')
        self.assertIn('if "%PAT%"=="" set "PAT=test*.py"', [line for _, line in code(WINDOWS_TESTS)])   # default: every test file


class AutostartScriptTests(unittest.TestCase):
    """fix-board (VERIFY7A L4/I5, VERIFY7B INFO): the two scripts that touch the Startup folder."""
    PROMPT = 'set /p "CONFIRM=Type yes to continue, anything else to cancel: "'
    CHECK = 'if /i not "%CONFIRM%"=="yes" goto cancelled'

    def rows(self, rel):
        return [line for _, line in code(rel)]

    def block(self, rows, label):
        start = rows.index(label)
        end = next((i for i in range(start + 1, len(rows)) if rows[i].startswith(':')), len(rows))
        return rows[start + 1:end]

    def check_confirm(self, rel, action):
        """CONFIRM is cleared right before `set /p` (an inherited CONFIRM=yes must not count), the
        typed answer is checked right after it, and `action` (the only change the script makes)
        comes after that check and nowhere else."""
        rows = self.rows(rel)
        prompts = [i for i, line in enumerate(rows) if re.match(r'set\s+/p\b', line, re.I)]
        self.assertEqual(len(prompts), 1, rel)
        ask = prompts[0]
        self.assertEqual(rows[ask], self.PROMPT)
        self.assertEqual(rows[ask - 1], 'set "CONFIRM="', rel + ': CONFIRM must be cleared before set /p')
        self.assertEqual(rows[ask + 1], self.CHECK, rel + ': the typed yes is checked right after the prompt')
        acts = [i for i, line in enumerate(rows) if action(line)]
        self.assertEqual(len(acts), 1, rel)
        self.assertGreater(acts[0], ask + 1)
        self.assertFalse(any(line.startswith('goto') or line.startswith(':') for line in rows[ask + 2:acts[0]]),
                         rel + ': nothing between the check and the change may jump past the check')
        self.assertEqual(self.block(rows, ':cancelled')[-1], 'goto finish')
        self.assertFalse(any(action(line) for line in self.block(rows, ':cancelled')))

    def check_pause_at_the_end(self, rel):
        """Every path ends at one `pause` then one exit, so a double-clicked window stays open."""
        rows = self.rows(rel)
        self.assertEqual(rows[-3:], [':finish', 'pause', 'exit /b %RC%'], rel)
        exits = [line for line in rows if re.match(r'exit\b', line, re.I)]
        self.assertEqual(exits, ['exit /b %RC%'], rel + ': no early exit may skip the pause')
        self.assertEqual(rows.count('pause'), 1)
        self.assertEqual(rows[rows.index('setlocal') + 1:].count('set "RC=1"'), 1)
        for n, line in code(rel):
            self.assertFalse(line.endswith('(') or line == ')', '%s:%d: no ( ) blocks (a ")" in a path breaks them)' % (rel, n))

    def test_install_clears_confirm_checks_the_typed_yes_and_pauses(self):
        rel = 'crew/autostart-install.cmd'
        self.check_confirm(rel, lambda line: line.startswith('(echo start "" /min cmd /c') and line.endswith('>"%TARGET%"'))
        self.check_pause_at_the_end(rel)
        rows = self.rows(rel)
        self.assertLess(rows.index('if exist "%TARGET%" goto already'), rows.index(self.PROMPT))

    def test_remove_clears_confirm_checks_the_typed_yes_and_pauses(self):
        rel = 'crew/autostart-remove.cmd'
        self.check_confirm(rel, lambda line: line.lower().startswith('del '))
        self.check_pause_at_the_end(rel)
        rows = self.rows(rel)
        self.assertLess(rows.index('if not exist "%TARGET%" goto nothing'), rows.index(self.PROMPT))

    def check_success_exit(self, rel, action, check, failures, also_ok=()):
        """fix2-board (VERIFY8A D07): after the change and its check, the success path sets RC=0 on the
        line right before its `goto finish`, so `exit /b %RC%` reports 0; no failure path sets it."""
        rows = self.rows(rel)
        act = next(i for i, line in enumerate(rows) if action(line))
        self.assertEqual(rows[act + 1], check, rel + ': the change is checked on the next line')
        done = rows.index('goto finish', act)
        self.assertEqual(rows[done - 1], 'set "RC=0"', rel + ': the success path must report 0')
        self.assertFalse(any(line.startswith('goto') or line.startswith(':') for line in rows[act + 2:done]), rel)
        for label in failures:
            self.assertNotIn('set "RC=0"', self.block(rows, label), rel + ' ' + label)
        self.assertEqual(rows.count('set "RC=0"'), 1 + len(also_ok), rel)
        for label in also_ok:
            self.assertEqual(self.block(rows, label)[-2:], ['set "RC=0"', 'goto finish'], rel + ' ' + label)

    def test_install_reports_success_with_exit_code_0(self):
        self.check_success_exit('crew/autostart-install.cmd',
                                lambda line: line.startswith('(echo start "" /min cmd /c') and line.endswith('>"%TARGET%"'),
                                'if not exist "%TARGET%" goto notwritten', (':already', ':cancelled', ':notwritten'))

    def test_remove_reports_success_with_exit_code_0(self):
        self.check_success_exit('crew/autostart-remove.cmd', lambda line: line.lower().startswith('del '),
                                'if exist "%TARGET%" goto notremoved', (':cancelled', ':notremoved'), also_ok=(':nothing',))


class ScheduleAndGuideTests(unittest.TestCase):
    def test_every_schedule_spec_is_not_created(self):
        specs = sorted((TREE / 'crew' / 'schedules').glob('*.json'))
        self.assertGreaterEqual(len(specs), 3)
        for spec in specs:
            with self.subTest(spec.name):
                self.assertEqual(json.loads(spec.read_text(encoding='utf-8'))['status'], 'not created')

    # Named in the guide but deliberately NOT in the tree, each with its reason.
    OUTSIDE = {
        'keeper-credential.json': 'a runtime file of the room (a stub once the vault is on)',
        'Common Room Crew.cmd': 'the one file autostart-install.cmd writes into the Startup folder',
        'CommonRoom/keeper/<16 hex>': 'a Windows Credential Manager target, not a file',
        'crew --credential ...keeper-credential.json': 'a command line, not a path',
        'TASK.md': 'written in a headless run folder', 'RESULT.md': 'written in a headless run folder',
        'QUESTION.md': 'written in a headless run folder', 'BLOCKED.md': 'written in a headless run folder',
        'QUESTIONS.md': 'written in the plan folder by an orchestrator run',
        'tick.py failed: ...': 'the reply a scheduled run gives when tick.py itself fails, not a path',
        'Artifacts/...': 'a citation form cite_check recognises, not a path',
        'Lab Notes/...': 'a citation form cite_check recognises, not a path',
        'D:\\Work Area\\...': 'a citation form cite_check recognises, not a path',
        'relay\\<seat>\\requests\\': 'every seat\'s relay request folder under the room runtime',
        '*-credential.json': 'the room key files, named as a pattern',
        'archive\\': "every seat's relay archive folder under the room runtime",
        'relay.py:9999': 'an example of a bare citation, not a path',
        'Artifacts/': 'a citation root cite_check recognises, not a path',
        'common_room/store.py:12': 'an example of a tree-relative citation, not a path',
    }
    # Named in the guide, expected in the tree, not written yet at this commit.
    PENDING = {'REPORT_AUTONOMY_2026-09-24.md': "the build report, written at the autonomy build's close-out"}

    def guide_paths(self):
        """(tree-relative candidates, every file-like backtick span) read from the guide."""
        text = (TREE / GUIDE).read_text(encoding='utf-8')
        spans = re.findall(r'`([^`\n]+)`', text)
        found = {}
        for tail in re.findall(re.escape(TREE_ABS) + r'((?:\\[^\\\s"`]+)+)', text):
            found[tail.lstrip('\\').replace('\\', '/')] = TREE_ABS + tail
        for hit in re.findall(r'(?<![\w.\\/-])((?:crew|common_room|tests|scripts|web)[\\/][\w.*<>/\\-]*)', text):
            found[hit.replace('\\', '/')] = hit
        for module in re.findall(r'\bcommon_room\.([a-z_]+)\b', text):
            found['common_room/%s.py' % module] = 'common_room.' + module
        for doc in re.findall(r'\b([A-Z][A-Z0-9_]+_\d{4}-\d{2}-\d{2}\.md)\b', text):
            found[doc] = doc
        for cmd in re.findall(r'((?:Start|Stop|Open)(?: [A-Z][a-z]+)+\.cmd)', text):
            found[cmd] = cmd
        for span in spans:
            if re.fullmatch(r'[\w.-]+\.json', span) and span not in self.OUTSIDE:
                matches = sorted((TREE / 'crew').rglob(span))
                found[matches[0].relative_to(TREE).as_posix() if matches else 'crew/**/' + span] = span
        filelike = [s for s in spans if re.search(r'\.(md|py|json|cmd)\b', s) or '/' in s or '\\' in s]
        return found, filelike

    def exists(self, rel):
        if any(c in rel for c in '*<'):
            pattern = re.sub(r'<[^>]+>', '*', rel)
            return any(fnmatch.fnmatch(p.relative_to(TREE).as_posix(), pattern) for p in TREE.rglob('*'))
        return (TREE / rel).exists()

    def test_every_tree_path_the_guide_names_exists(self):
        found, _ = self.guide_paths()
        for must in ('crew/tick.py', 'crew/cite_check.py', 'common_room/agent_worker.py', 'common_room/keepalive.py',
                     'crew/agent-once.cmd', 'crew/autostart-install.cmd', 'crew/autostart-remove.cmd',
                     'crew/schedules/orchestrator-claude-room.json', LAUNCHER, STOP,
                     'BUILD_PLAN_COMMON_ROOM_AUTONOMY_2026-09-24.md'):
            self.assertIn(must, found, 'the reader missed ' + must)       # the extraction is not vacuous
        missing = sorted(rel for rel in found if rel not in self.OUTSIDE and rel not in self.PENDING
                         and not self.exists(rel))
        self.assertEqual(missing, [])

    def test_every_file_like_name_in_the_guide_is_classified(self):
        found, filelike = self.guide_paths()
        raw_forms = list(found.values()) + list(found)
        for span in filelike:
            outside = (span in self.OUTSIDE or span.startswith('.runtime\\') or
                       (span.startswith('D:\\Work Area') and not span.startswith(TREE_ABS + '\\')))
            covered = any(form in span for form in raw_forms)
            self.assertTrue(outside or covered, 'the guide names %r and this test does not check it' % span)

    def test_pending_close_out_report(self):
        for rel, why in self.PENDING.items():
            if not (TREE / rel).exists():
                self.skipTest('%s is named in %s but not in the tree yet (%s)' % (rel, GUIDE, why))


# -- the transliteration --------------------------------------------------------------------------
# Exact text of every launcher line the model mirrors (1-based line numbers). If the launcher is
# edited, these fail until the model below is read against the new file again.
ANCHORS = {
    12: 'call "%~dp0crew\\find-python.cmd" || goto python_missing',
    13: 'goto python_ok',
    14: ':python_missing',
    15: 'if not defined CR_UNATTENDED pause',
    16: 'exit /b 1',
    17: ':python_ok',
    26: 'set "AGENT_SEATS="',
    32: '"%PY%" -X utf8 -B crew\\preflight.py >nul 2>nul',
    33: 'set "PF=%errorlevel%"',
    34: 'if "%PF%"=="3" goto oldroom',
    35: 'if "%PF%"=="4" goto stranger',
    36: 'goto startroom',
    37: ':oldroom',
    38: 'if defined CR_UNATTENDED (',
    40: '  exit /b 0',
    41: ')',
    45: 'choice /M "Stop that old room (and its crew windows) and start the crew"',
    46: 'if errorlevel 2 exit /b 1',
    48: 'taskkill /F /FI "WINDOWTITLE eq CRCREW *" /T >nul 2>nul',
    49: 'taskkill /F /T /PID %OLDPID% >nul 2>nul',
    51: 'goto startroom',
    52: ':stranger',
    54: 'if not defined CR_UNATTENDED pause',
    55: 'exit /b 1',
    56: ':startroom',
    # seat-dot (2026-10-05): the findstr gate became crew\enroll_check.py, read again from here to the end.
    57: 'set "ENROLL="',
    59: 'if /i "%~1"=="--enrollment" goto enrolled',
    64: '"%PY%" -X utf8 -B crew\\enroll_check.py %SEATS%',
    65: 'set "EC=%errorlevel%"',
    66: 'if "%EC%"=="0" goto enrolled',
    67: 'if "%EC%"=="3" goto addenroll',
    68: 'if "%EC%"=="4" goto oldscope',
    70: 'if not defined CR_UNATTENDED pause',
    71: 'exit /b 1',
    72: ':oldscope',
    75: 'if not defined CR_UNATTENDED pause',
    76: 'exit /b 1',
    77: ':addenroll',
    79: 'copy /y "crew\\enrollment-crew-2026-09-24.json" ".runtime\\handoffs\\enrollment-crew-2026-09-24.json" >nul '
        '|| goto copyfailed',
    80: 'set "ENROLL=--enrollment .runtime\\handoffs\\enrollment-crew-2026-09-24.json"',
    81: 'goto enrolled',
    82: ':copyfailed',
    84: 'if not defined CR_UNATTENDED pause',
    85: 'exit /b 1',
    86: ':enrolled',
    88: 'start "CRCREW room" /min "%PY%" -X utf8 -B -m common_room.keepalive --name room -- -X utf8 -B -m '
        'common_room.activation %ROOMARGS% --plans-root "%PLANS_ROOT%" %ENROLL% %*',
    90: 'set /a TRIES=0',
    91: ':waitroom',
    93: 'set /a TRIES+=1',
    94: '"%PY%" -X utf8 -B crew\\preflight.py >nul 2>nul',
    95: 'if errorlevel 4 goto notyet',
    96: 'if errorlevel 3 goto roomup',
    97: ':notyet',
    98: 'if %TRIES% LSS 15 goto waitroom',
    99: ':roomdown',
    104: 'if not defined CR_UNATTENDED pause',
    105: 'exit /b 1',
    106: ':roomup',
    107: 'start "CRCREW relay" /min "%PY%" -X utf8 -B -m common_room.keepalive --name relay -- -X utf8 -B -m '
         'common_room.relay --actors %RELAY_SEATS%',
    108: 'start "CRCREW local-a" /min "%PY%" -X utf8 -B -m common_room.keepalive --name local-a -- -X utf8 -B -m '
         'common_room.ollama_worker --config crew\\local-a.json --loop',
    # seat-truth (D9): local-b gets no window; the line that started it is now a remark.
    109: 'rem local-b is out of the run (D9): its enrollment row and crew\\local-b.json stay, it gets no window.',
    # notify (2026-10-05): the phone-push window, only when Shane's crew\notify.json exists.
    111: 'if exist "crew\\notify.json" start "CRCREW notify" /min "%PY%" -X utf8 -B -m common_room.keepalive --name '
         'notify -- -X utf8 -B -m common_room.notify --loop',
    # duet (2026-10-30): the Drive relay window, only when crew\\duet.json exists. Every line below moved down by two.
    112: 'rem Claude Duet over Google Drive (seat duet): the duet relay, only while crew\\duet.json exists '
         '(DUET_SEAT_2026-10-30.md).',
    113: 'if exist "crew\\duet.json" start "CRCREW duet" /min "%PY%" -X utf8 -B -m common_room.keepalive --name duet '
         '-- -X utf8 -B -m common_room.duet_relay --config crew\\duet.json',
    114: 'if defined AGENT_SEATS for %%S in (%AGENT_SEATS%) do start "CRCREW agent %%S" /min "%PY%" -X utf8 -B -m '
         'common_room.keepalive --name agent-%%S -- -X utf8 -B -m common_room.agent_worker --config '
         'crew\\agent-%%S.json --loop',
    122: 'if not defined CR_UNATTENDED pause',
    123: 'exit /b 0',
}
CREW_ENROLL = '--enrollment .runtime\\handoffs\\enrollment-crew-2026-09-24.json'
MAX_TRIES = 15


class Scenario:
    def __init__(self, python_ok=True, unattended=False, preflight=0, choose_yes=False, args=(),
                 enrollment_json=None, check=None, copy_ok=True, polls=(3,), agent_seats='', notify_json=False, duet_json=False):
        self.python_ok, self.unattended, self.preflight, self.choose_yes = python_ok, unattended, preflight, choose_yes
        self.args, self.enrollment_json, self.check, self.copy_ok = list(args), enrollment_json, check, copy_ok
        self.polls, self.agent_seats, self.notify_json = list(polls), agent_seats, notify_json
        self.duet_json = duet_json

    def check_code(self):
        """crew\\enroll_check.py's exit code (seat-dot, 2026-10-05). The real decision is enroll_check.decide,
        tested on real records in tests/test_seat_dot.py; this model needs only its code, so the fixture records
        stand in for the cases they name: no file or neither name -> 3 add, claude-code-b present -> 0 up to
        date, the 09-23 orchestra scope -> 4. `check` overrides it (2: the room would refuse)."""
        if self.check is not None:
            return self.check
        if self.enrollment_json is not None and '"claude-code-b"' in self.enrollment_json:
            return 0
        if self.enrollment_json is not None and '"orchestra"' in self.enrollment_json:
            return 4
        return 3


class Outcome:
    def __init__(self):
        self.labels, self.started, self.killed, self.room_tail = [], [], [], None
        self.pauses, self.prompted, self.copied, self.exit, self.enroll = 0, False, False, None, None

    def pause(self, scenario):
        if not scenario.unattended:            # every pause is `if not defined CR_UNATTENDED pause`
            self.pauses += 1


def launcher(s):
    """The launcher's control flow, as read. `exit` is the `exit /b` code of the path taken; every
    path ends in an explicit `exit /b` (the success path at L123 since duet, L121 since notify, L106 before seat-dot; fix-board 2026-09-24: before
    ea9d206 it ran off its last line and exited with whatever errorlevel `pause` left)."""
    o = Outcome()
    if not s.python_ok:                          # L12: call find-python.cmd || goto python_missing
        o.labels.append(':python_missing')       # L14
        o.pause(s)                               # L15
        o.exit = 1                               # L16
        return o
    o.labels.append(':python_ok')                # L13 goto python_ok / L17
    if s.preflight == 3:                         # L32-L34: preflight exit 3 -> :oldroom
        o.labels.append(':oldroom')              # L37
        if s.unattended:                         # L38-L41: unattended -> exit /b 0, nothing touched
            o.exit = 0
            return o
        o.prompted = True                        # L45: choice (Y = errorlevel 1, N = errorlevel 2)
        if not s.choose_yes:                     # L46: if errorlevel 2 exit /b 1
            o.exit = 1
            return o
        o.killed += ['windows CRCREW *', 'old room PID']    # L48 then L49
        # L51: goto startroom
    elif s.preflight == 4:                       # L35: exit 4 -> :stranger
        o.labels.append(':stranger')             # L52
        o.pause(s)                               # L54
        o.exit = 1                               # L55
        return o
    # L36: any other preflight code (0 nothing answers, 2 descriptors unreadable) -> :startroom
    o.labels.append(':startroom')                # L56
    o.enroll = ''                                # L57
    explicit = bool(s.args) and s.args[0].strip('"').lower() == '--enrollment'   # L59: if /i "%~1"==...
    if not explicit:
        code = s.check_code()                    # L64-L65: crew\enroll_check.py %SEATS%, EC=%errorlevel%
        if code == 3:                            # L67: if "%EC%"=="3" goto addenroll
            o.labels.append(':addenroll')        # L77
            if not s.copy_ok:                    # L79: copy /y ... || goto copyfailed
                o.labels.append(':copyfailed')   # L82
                o.pause(s)                       # L84
                o.exit = 1                       # L85
                return o
            o.copied = True
            o.enroll = CREW_ENROLL               # L80, then L81 goto enrolled
        elif code == 4:                          # L68: if "%EC%"=="4" goto oldscope
            o.labels.append(':oldscope')         # L72
            o.pause(s)                           # L75
            o.exit = 1                           # L76
            return o
        elif code != 0:                          # L66 takes 0 to :enrolled; any other code falls through L68
            o.pause(s)                           # L70
            o.exit = 1                           # L71
            return o
    o.labels.append(':enrolled')                 # L86
    o.started.append('CRCREW room')              # L88
    o.room_tail = ([o.enroll] if o.enroll else []) + list(s.args)       # L88: ... %ENROLL% %*
    tries = 0                                    # L90
    polls = iter(s.polls)
    while True:
        o.labels.append(':waitroom')             # L91
        tries += 1                               # L93
        code = next(polls, 0)                    # L94: preflight again (0 once the list runs out)
        if code >= 4:                            # L95: if errorlevel 4 goto notyet (errorlevel N means >= N)
            pass
        elif code >= 3:                          # L96: if errorlevel 3 goto roomup
            break
        o.labels.append(':notyet')               # L97 (also reached by falling through L96 on 0-2)
        if tries < MAX_TRIES:                    # L98
            continue
        o.labels.append(':roomdown')             # L99
        o.pause(s)                               # L104
        o.exit = 1                               # L105
        return o
    o.labels.append(':roomup')                   # L106
    o.started += ['CRCREW relay', 'CRCREW local-a']  # L107-L108 (L109 is a remark since seat-truth, D9)
    if s.notify_json:                            # L111: only when crew\notify.json exists
        o.started.append('CRCREW notify')
    if s.duet_json:                              # L113: only when crew\\duet.json exists (duet, 2026-10-30)
        o.started.append('CRCREW duet')
    for seat in s.agent_seats.split():           # L114: only when AGENT_SEATS is defined (non-empty)
        o.started.append('CRCREW agent ' + seat)
    o.pause(s)                                   # L122 (L120 before duet)
    o.exit = 0                                   # L123: exit /b 0 (L121 before duet)
    return o


ENROLL_B = '{"actors": [{"id": "claude-code-b"}], "authorities": [{"id": "orchestra"}]}'
ENROLL_OLD_ORCHESTRA = '{"actors": [{"id": "claude-room"}], "authorities": [{"id": "orchestra"}]}'
ENROLL_PLAIN = '{"actors": [{"id": "local-a"}], "authorities": [{"id": "local-crew"}]}'
CREW_WINDOWS = ['CRCREW room', 'CRCREW relay', 'CRCREW local-a']


class LauncherTransliterationTests(unittest.TestCase):
    """A check on reading, not execution: see the module docstring."""

    def test_anchor_lines_still_say_what_the_model_read(self):
        text = lines(LAUNCHER)
        for number, expected in sorted(ANCHORS.items()):
            self.assertEqual(text[number - 1], expected, 'launcher line %d changed; read the model again' % number)
        labels = [line for line in text if line.startswith(':')]
        self.assertEqual(labels, [':python_missing', ':python_ok', ':oldroom', ':stranger', ':startroom', ':oldscope',
                                  ':addenroll', ':copyfailed', ':enrolled', ':waitroom', ':notyet', ':roomdown', ':roomup'])
        self.assertEqual(len(text), max(ANCHORS), 'nothing follows the final exit /b 0')

    def run_model(self, **kw):
        kw.setdefault('agent_seats', settings(LAUNCHER)['AGENT_SEATS'])     # the file's own value (empty)
        return launcher(Scenario(**kw))

    def test_first_run_on_a_fresh_folder_adds_the_crew_enrollment_and_starts_three_windows(self):
        o = self.run_model(enrollment_json=None, polls=(0, 0, 3))
        self.assertEqual(o.labels, [':python_ok', ':startroom', ':addenroll', ':enrolled', ':waitroom', ':notyet',
                                    ':waitroom', ':notyet', ':waitroom', ':roomup'])
        self.assertEqual(o.started, CREW_WINDOWS)
        self.assertEqual((o.enroll, o.room_tail, o.copied, o.killed), (CREW_ENROLL, [CREW_ENROLL], True, []))
        self.assertEqual((o.pauses, o.exit), (1, 0))

    def test_enrollment_with_claude_code_b_starts_without_enroll(self):
        o = self.run_model(enrollment_json=ENROLL_B)
        self.assertEqual(o.labels, [':python_ok', ':startroom', ':enrolled', ':waitroom', ':roomup'])
        self.assertEqual((o.enroll, o.room_tail, o.copied, o.started), ('', [], False, CREW_WINDOWS))

    def test_older_orchestra_scope_without_the_new_seats_stops_before_starting_anything(self):
        for unattended in (False, True):
            o = self.run_model(enrollment_json=ENROLL_OLD_ORCHESTRA, unattended=unattended)
            self.assertEqual(o.labels, [':python_ok', ':startroom', ':oldscope'])
            self.assertEqual((o.started, o.exit, o.pauses), ([], 1, 0 if unattended else 1))

    def test_a_refused_enrollment_check_stops_before_starting_anything(self):     # seat-dot: enroll_check exit 2
        for unattended in (False, True):
            o = self.run_model(check=2, unattended=unattended)
            self.assertEqual(o.labels, [':python_ok', ':startroom'])
            self.assertEqual((o.started, o.exit, o.pauses, o.copied), ([], 1, 0 if unattended else 1, False))

    def test_a_failed_copy_stops_before_starting_anything(self):                 # seat-dot: copy /y ... || goto copyfailed
        for unattended in (False, True):
            o = self.run_model(check=3, copy_ok=False, unattended=unattended)
            self.assertEqual(o.labels, [':python_ok', ':startroom', ':addenroll', ':copyfailed'])
            self.assertEqual((o.started, o.exit, o.pauses, o.enroll), ([], 1, 0 if unattended else 1, ''))

    def test_enrollment_with_neither_name_adds_the_crew_enrollment(self):
        o = self.run_model(enrollment_json=ENROLL_PLAIN)
        self.assertIn(':addenroll', o.labels)
        self.assertEqual(o.enroll, CREW_ENROLL)

    def test_explicit_enrollment_argument_is_used_as_is_whatever_enrollment_json_says(self):
        args = ['--enrollment', '.runtime\\handoffs\\fixed.json']
        for content in (None, ENROLL_B, ENROLL_OLD_ORCHESTRA, ENROLL_PLAIN):
            for spelling in ('--enrollment', '--ENROLLMENT', '"--enrollment"'):
                o = self.run_model(enrollment_json=content, args=[spelling] + args[1:])
                self.assertEqual(o.labels, [':python_ok', ':startroom', ':enrolled', ':waitroom', ':roomup'])
                self.assertEqual((o.enroll, o.room_tail), ('', [spelling] + args[1:]))

    def test_old_room_unattended_exits_0_and_touches_nothing(self):
        o = self.run_model(preflight=3, unattended=True)
        self.assertEqual(o.labels, [':python_ok', ':oldroom'])
        self.assertEqual((o.exit, o.started, o.killed, o.prompted, o.pauses), (0, [], [], False, 0))

    def test_old_room_attended_asks_and_no_changes_nothing(self):
        o = self.run_model(preflight=3, choose_yes=False)
        self.assertEqual((o.exit, o.started, o.killed, o.prompted), (1, [], [], True))

    def test_old_room_attended_yes_closes_crew_windows_before_the_pid_then_starts(self):
        o = self.run_model(preflight=3, choose_yes=True, enrollment_json=ENROLL_B)
        self.assertEqual(o.killed, ['windows CRCREW *', 'old room PID'])
        self.assertEqual(o.labels, [':python_ok', ':oldroom', ':startroom', ':enrolled', ':waitroom', ':roomup'])
        self.assertEqual(o.started, CREW_WINDOWS)

    def test_stranger_on_the_port_stops(self):
        for unattended in (False, True):
            o = self.run_model(preflight=4, unattended=unattended)
            self.assertEqual(o.labels, [':python_ok', ':stranger'])
            self.assertEqual((o.exit, o.started, o.killed, o.pauses), (1, [], [], 0 if unattended else 1))

    def test_unreadable_descriptors_take_the_start_path(self):
        o = self.run_model(preflight=2, enrollment_json=ENROLL_B)
        self.assertEqual(o.labels[:3], [':python_ok', ':startroom', ':enrolled'])

    def test_missing_python_stops_first(self):
        for unattended in (False, True):
            o = self.run_model(python_ok=False, unattended=unattended, preflight=3)
            self.assertEqual((o.labels, o.exit, o.pauses), ([':python_missing'], 1, 0 if unattended else 1))

    def test_a_room_that_never_answers_gives_up_after_15_tries_with_only_the_room_window(self):
        for polls in ((), (4,) * 20, (0, 4, 2, 1) * 5):
            o = self.run_model(enrollment_json=ENROLL_B, polls=polls)
            self.assertEqual(o.labels.count(':waitroom'), MAX_TRIES)
            self.assertEqual(o.labels[-1], ':roomdown')
            self.assertEqual((o.started, o.exit), (['CRCREW room'], 1))

    def test_the_notify_window_only_when_notify_json_exists(self):                # notify, 2026-10-05
        self.assertEqual(self.run_model(enrollment_json=ENROLL_B).started, CREW_WINDOWS)
        o = self.run_model(enrollment_json=ENROLL_B, notify_json=True)
        self.assertEqual(o.started, CREW_WINDOWS + ['CRCREW notify'])
        o = self.run_model(enrollment_json=ENROLL_B, notify_json=True, polls=())   # the room never answers
        self.assertEqual(o.started, ['CRCREW room'])

    def test_the_duet_window_only_when_duet_json_exists(self):                    # duet, 2026-10-30
        self.assertEqual(self.run_model(enrollment_json=ENROLL_B).started, CREW_WINDOWS)
        o = self.run_model(enrollment_json=ENROLL_B, duet_json=True)
        self.assertEqual(o.started, CREW_WINDOWS + ['CRCREW duet'])
        o = self.run_model(enrollment_json=ENROLL_B, notify_json=True, duet_json=True)
        self.assertEqual(o.started, CREW_WINDOWS + ['CRCREW notify', 'CRCREW duet'])
        o = self.run_model(enrollment_json=ENROLL_B, duet_json=True, polls=())     # the room never answers
        self.assertEqual(o.started, ['CRCREW room'])

    def test_agent_windows_only_when_agent_seats_names_them(self):
        self.assertEqual(settings(LAUNCHER)['AGENT_SEATS'], '')
        o = self.run_model(enrollment_json=ENROLL_B)
        self.assertFalse([t for t in o.started if t.startswith('CRCREW agent')])
        o = self.run_model(enrollment_json=ENROLL_B, agent_seats='claude-code claude-code-b')
        self.assertEqual(o.started[len(CREW_WINDOWS):], ['CRCREW agent claude-code', 'CRCREW agent claude-code-b'])

    def test_across_every_input_unattended_never_waits_and_only_an_answered_old_room_is_killed(self):
        grid = itertools.product((True, False), (True, False), (0, 2, 3, 4), (True, False), ((), ('--enrollment', 'x')),
                                 (None, ENROLL_B, ENROLL_OLD_ORCHESTRA, ENROLL_PLAIN), ((3,), (), (0, 0, 4, 3)))
        count = 0
        for python_ok, unattended, preflight, yes, args, content, polls in grid:
            o = self.run_model(python_ok=python_ok, unattended=unattended, preflight=preflight, choose_yes=yes,
                               args=args, enrollment_json=content, polls=polls)
            count += 1
            if unattended:
                self.assertEqual((o.pauses, o.prompted, o.killed), (0, False, []))
            if o.killed:
                self.assertEqual((preflight, unattended, yes), (3, False, True))
            if ':roomup' not in o.labels:
                self.assertIn(o.started, ([], ['CRCREW room']))
            self.assertIn(o.exit, (0, 1))
            # Exit 0 exactly when the crew started, or when an unattended run found the room already up.
            self.assertEqual(o.exit == 0, ':roomup' in o.labels or (python_ok and unattended and preflight == 3))
        self.assertEqual(count, 2 * 2 * 4 * 2 * 2 * 4 * 3)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
