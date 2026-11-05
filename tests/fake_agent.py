"""Fictional stand-in for a headless coding agent (Claude Code / Codex), used only by
tests/test_agent_worker.py through the runner's own command line:

    [sys.executable, <this file>, "{task_file}", "{run_dir}"]

It reads TASK.md like a real agent would and picks its behaviour from the first line of the
form "AGENT: <WORD> [argument]" (only a task body carries such a line). Before acting it records
what it was given, so the tests can check it: its argv, the NAMES of its environment variables
(never their values), its working folder, its pid and whether stdin is the null device.
It never contacts the Common Room and never opens the network.
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

LINE = re.compile(r'AGENT: ([A-Z_]+)(?: (.+))?')
MIDDLE = ("import subprocess, sys, time\n"
          "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
          "print(p.pid, flush=True)\n"
          "time.sleep(120)\n")
CLAUDE_JSON = {'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.0421,
               'num_turns': 3, 'result': 'Fixture agent finished.'}


def write(path, text):
    Path(path).write_text(text, encoding='utf-8')


def stdin_is_null():
    if os.name == 'nt':
        # Windows has no st_rdev and gives NUL no inode: NUL is a character device that is not a
        # console, while an inherited console is one and a pipe or file is not a character device.
        import ctypes, msvcrt
        try:
            handle = msvcrt.get_osfhandle(0)
        except OSError:
            return None
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetFileType.argtypes = [ctypes.c_void_p]
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        mode = ctypes.c_ulong()
        console = bool(kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
        return kernel32.GetFileType(ctypes.c_void_p(handle)) == 2 and not console      # FILE_TYPE_CHAR
    try:
        null, given = os.stat(os.devnull), os.fstat(0)
    except OSError:
        return None
    return (given.st_dev, given.st_ino, given.st_rdev) == (null.st_dev, null.st_ino, null.st_rdev)


def result(run_dir, task_id, extra=''):
    write(run_dir / 'RESULT.md', 'Fake result for %s. MEASURED: the fixture ran.%s\n' % (task_id, extra))
    print(json.dumps(CLAUDE_JSON), flush=True)


def question(run_dir):
    write(run_dir / 'QUESTION.md', 'Which folder should the summary go in?\n')


def main():
    task_file, run_dir = Path(sys.argv[1]), Path(sys.argv[2])
    write(run_dir / 'agent-argv.json', json.dumps(sys.argv))
    write(run_dir / 'agent-env-names.json', json.dumps(sorted(os.environ)))     # names only, never values
    write(run_dir / 'agent-cwd.txt', os.getcwd())
    write(run_dir / 'agent-pid.txt', str(os.getpid()))
    write(run_dir / 'agent-stdin.json', json.dumps({'devnull': stdin_is_null()}))
    text = task_file.read_text(encoding='utf-8')
    task_id = re.search(r'^- Task: (\S+)', text, re.M)[1]
    word, argument = 'NOTHING', None
    for line in text.splitlines():
        match = LINE.fullmatch(line.strip())
        if match:
            word, argument = match[1], match[2]
            break
    print('fixture agent: ' + word, file=sys.stderr, flush=True)
    if word == 'RESULT':
        result(run_dir, task_id)
    elif word == 'BIGRESULT':
        write(run_dir / 'RESULT.md', 'BEGIN ' + 'x' * int(argument) + ' TAIL\n')
    elif word == 'QUESTION':
        question(run_dir)
    elif word == 'BLOCKED':
        write(run_dir / 'BLOCKED.md', 'The named input does not exist.\n')
    elif word == 'ASK_THEN_RESULT':
        if '## Came back from a decision' in text:
            result(run_dir, task_id, ' It used the answer.')
        else:
            question(run_dir)
    elif word == 'SLEEP':
        time.sleep(float(argument))
        write(run_dir / 'agent-done.txt', 'slept')
        result(run_dir, task_id)
    elif word == 'SPAWN_CHILD_AND_SLEEP':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        write(run_dir / 'pids.json', json.dumps({'agent': os.getpid(), 'child': child.pid}))
        time.sleep(120)
        result(run_dir, task_id)
    elif word == 'CRASH':
        sys.exit(3)
    elif word == 'TWO':
        result(run_dir, task_id)
        question(run_dir)
    elif word == 'EMPTY_RESULT':
        write(run_dir / 'RESULT.md', ' \n')
    elif word == 'LINK_RESULT':
        os.symlink(argument, run_dir / 'RESULT.md')
    elif word == 'LINK_QUESTION':
        os.symlink(argument, run_dir / 'QUESTION.md')
    elif word == 'HARDLINK_RESULT':
        os.link(argument, run_dir / 'RESULT.md')
    elif word == 'COPY_RESULT':
        shutil.copyfile(argument, run_dir / 'RESULT.md')
    elif word == 'COPY_QUESTION':
        shutil.copyfile(argument, run_dir / 'QUESTION.md')
    elif word == 'RELAY':
        # VERIFY8B N1: ways around a relay watch that compares request names only (argument: <case> <room folder>).
        case, room = argument.split(' ', 1)
        relay = Path(room) / 'relay'
        reviewer = relay / 'check-reviewer'
        forged = json.dumps({'request_id': 'forged-' + case.lower(), 'kind': 'command', 'action': 'note',
                             'payload': {'authority_id': 'crew-one', 'body': 'forged'}})
        if case == 'OVERWRITE':                       # in place, over a request name that was there before the run
            with open(reviewer / 'requests' / 'tick-old.json', 'w', encoding='utf-8') as handle:
                handle.write(forged)
        elif case == 'MANYDIRS':                      # 1000 folders that sort before every seat, then a new request
            for index in range(1000):
                (relay / ('%04d' % index)).mkdir()
            write(reviewer / 'requests' / 'forged-d.json', forged)
        elif case == 'ARCHIVE':                       # written, then moved to archive/ as if answered and tidied
            write(reviewer / 'requests' / 'forged-e.json', forged)
            os.replace(reviewer / 'requests' / 'forged-e.json', reviewer / 'archive' / 'forged-e.1.json')
            # A request that was there before the run, moved unchanged: a chat tidying its own folder.
            os.replace(reviewer / 'requests' / 'tick-old.json', reviewer / 'archive' / 'tick-old.1.json')
        elif case == 'SEATS':                         # more seat folders than the runner lists
            for index in range(65):
                (relay / ('zz-seat-%02d' % index) / 'requests').mkdir(parents=True)
        result(run_dir, task_id)
    elif word in ('WRITE_REQUESTS', 'WRITE_REQUESTS_ASK'):
        # Acts as other seats through the file relay (argument: the room folder), the H1 attack.
        relay = Path(argument) / 'relay'
        for seat, name in [('check-reviewer', 'rv-self.json'), ('agent-b', 'b-1.json'), ('agent-b', 'b-2.json'),
                           ('agent-b', 'b-3.json.tmp'), ('new-seat', 'n-1.json'), ('Odd Name', 'o-1.json'),
                           ('linked', 'l-1.json'), ('agent-c', 'c-1.json')]:
            folder = relay / seat / 'requests'
            folder.mkdir(parents=True, exist_ok=True)
            write(folder / ('%s-%s' % (task_id, name)), json.dumps({'request_id': 'fixture', 'kind': 'state'}))
        if word == 'WRITE_REQUESTS':
            result(run_dir, task_id)
        else:
            question(run_dir)
    elif word == 'HANG_AT_FIRST_FENCE':
        if re.search(r'^- Fence: 1$', text, re.M):
            time.sleep(300)
        result(run_dir, task_id)
    elif word == 'GROUP_CHILD_THEN_EXIT':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        write(run_dir / 'child.json', json.dumps({'child': child.pid}))
        result(run_dir, task_id)
    elif word == 'SETSID_CHILD_THEN_EXIT':
        # A child that leaves the agent's session, and a same-group middle process whose own child does.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
        middle = subprocess.Popen([sys.executable, '-c', MIDDLE], stdout=subprocess.PIPE)
        grandchild = int(middle.stdout.readline())
        write(run_dir / 'child.json', json.dumps({'child': child.pid, 'middle': middle.pid, 'grandchild': grandchild}))
        time.sleep(float(argument))
        result(run_dir, task_id)
    elif word == 'SWAP_RUN_DIR':
        # Replaces its own run folder with a link to another folder holding a RESULT.md.
        write(Path(argument) / 'RESULT.md', 'Result from another folder. MEASURED: nothing.\n')
        os.rename(run_dir, str(run_dir) + '.moved')
        os.symlink(argument, run_dir, target_is_directory=True)
    # NOTHING (or no AGENT: line): exit 0 without writing any outcome file.


if __name__ == '__main__':
    main()
