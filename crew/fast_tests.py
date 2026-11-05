"""Optional per-file unittest runner. Default Windows runner remains serial.

Each subprocess gets a private system temporary directory. Existing fixture
helpers keep their original package-root layout and unique case directories. Failed parallel files
are rerun alone; a passing rerun becomes sticky serial for later invocations.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

FIELDS = ('tests', 'failures', 'errors', 'skipped', 'expected failures', 'unexpected successes')
FILE_NAME = re.compile(r'test[^/\\]*\.py\Z')


def child(root, name, folder, result_file, jobs):
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ['COMMON_ROOM_TEST_JOBS'] = str(jobs)
    for key in ('TMP', 'TEMP', 'TMPDIR'):
        os.environ[key] = str(folder)
    tempfile.tempdir = str(folder)
    if (root / 'tests' / 'helpers.py').is_file():
        from tests import helpers
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=name, top_level_dir=str(root))
        result = unittest.TextTestRunner(stream=capture).run(suite)
    data = dict(zip(FIELDS, (result.testsRun, len(result.failures), len(result.errors),
                            len(result.skipped), len(result.expectedFailures), len(result.unexpectedSuccesses))))
    data['ok'] = result.wasSuccessful()
    data['log'] = capture.getvalue()
    result_file.write_text(json.dumps(data), encoding='utf-8')
    return 0 if data['ok'] else 1


CLEANUP_TRIES = 3          # removing a file's temp folder: the first try right after its child exits, then 2 retries
CLEANUP_RETRY_PAUSE = 1.0  # seconds between tries, so the bound is about 2 s of waiting in all
CLEANUP_RETRY_LINE = 'TEMP CLEANUP RETRY '
CLEANUP_ERROR_LINE = 'TEMP CLEANUP ERROR: '


def _writable_then_retry(function, path, exc):
    """rmtree's onexc: what tempfile.TemporaryDirectory's own cleanup did here at 356bbac for a read-only entry (make it
    writable, remove it once more). Any other error, such as a file another process still holds open, goes on to
    remove_folder."""
    if isinstance(exc, PermissionError) and not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        function(path)
        return
    raise exc


def remove_folder(folder):
    """Remove one file's temp folder. Returns the lines to add to that file's section and whether removing failed:
    one TEMP CLEANUP RETRY line per failed try that is retried, and a final TEMP CLEANUP ERROR line naming the leftover
    path if every try failed. A folder that cannot be removed usually means a process the test started still holds a
    file in it."""
    lines = []
    for attempt in range(1, CLEANUP_TRIES + 1):
        try:
            shutil.rmtree(folder, onexc=_writable_then_retry)
            return lines, False
        except OSError as exc:
            if attempt < CLEANUP_TRIES:
                lines.append(CLEANUP_RETRY_LINE + str(attempt) + ' of ' + str(CLEANUP_TRIES - 1) + ': ' + str(folder) +
                             ': ' + type(exc).__name__ + ': ' + str(exc))
                time.sleep(CLEANUP_RETRY_PAUSE)
            else:
                lines.append(CLEANUP_ERROR_LINE + 'could not remove ' + str(folder) + ' after ' + str(CLEANUP_TRIES) +
                             ' tries; it is left behind: ' + type(exc).__name__ + ': ' + str(exc))
    return lines, True


def run_file(root, name, jobs):
    folder = Path(tempfile.mkdtemp(prefix='f-'))
    result_file = folder / 'result.json'
    command = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
               '--child', str(root), name, str(folder), str(result_file), str(jobs)]
    env = dict(os.environ, TMP=str(folder), TEMP=str(folder), TMPDIR=str(folder))
    started = time.perf_counter()
    process = subprocess.run(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    seconds = time.perf_counter() - started
    try:
        data = json.loads(result_file.read_text(encoding='utf-8'))
        if any(type(data.get(key)) is not int or data[key] < 0 for key in FIELDS):
            raise ValueError('invalid child counts')
        if type(data.get('ok')) is not bool or type(data.get('log')) is not str:
            raise ValueError('invalid child result')
        if (process.returncode == 0) != data['ok']:
            raise ValueError('child exit/result mismatch')
    except (OSError, ValueError, TypeError) as exc:
        data = dict.fromkeys(FIELDS, 0)
        data.update(errors=1, ok=False, log='ERROR: child process ' + name + '\n' + str(exc) + '\n' + process.stdout.decode('utf-8', 'replace'))
    # The folder is removed right after the child exits. A folder that cannot be removed fails this attempt, visibly,
    # and the run goes on: it never aborts the whole run, and such an attempt never counts as passed.
    lines, failed = remove_folder(folder)
    if lines:
        log = data['log']
        data['log'] = log + ('\n' if log and not log.endswith('\n') else '') + '\n'.join(lines) + '\n'
    data['cleanup_failed'] = failed
    if failed:
        data['errors'] += 1
        data['ok'] = False
    data.update(file=name, seconds=seconds)
    return data


def load_serial(path):
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        if not FILE_NAME.fullmatch(name) or name in ('.', '..'):
            raise ValueError('invalid serial list entry: ' + name)
        names.add(name)
    return names


def save_serial(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    # One coordinator writes after all parallel children have finished.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
        out.write(''.join(name + '\n' for name in sorted(names)))
        temporary = Path(out.name)
    os.replace(temporary, path)


def run(root, output, jobs, serial_file, pattern, baseline_file=None):
    names = sorted(p.name for p in (root / 'tests').glob(pattern) if p.is_file() and FILE_NAME.fullmatch(p.name))
    local_serial = load_serial(serial_file)
    original_local = set(local_serial)
    baseline = load_serial(baseline_file) if baseline_file is not None else set()
    serial = baseline | local_serial
    if not names:
        raise ValueError('no matching test files')
    parallel_names = [name for name in names if name not in serial]
    serial_names = [name for name in names if name in serial]
    started = time.perf_counter()
    records = []
    notes = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        batch = list(pool.map(lambda name: run_file(root, name, jobs), parallel_names))
    for first in batch:
        final = first
        mode = 'parallel' if jobs > 1 else 'serial'
        elapsed = first['seconds']
        if jobs > 1 and not first['ok']:
            final = run_file(root, first['file'], 1)
            elapsed += final['seconds']
            notes.append('=== ' + first['file'] + ': initial parallel failure; rerun alone ===\n' + first['log'])
            mode = 'rerun alone'
            if first['cleanup_failed']:
                # a temp folder that could not be removed is not a parallel-only failure: it is never recorded as serial
                mode += '; temp cleanup failed in parallel'
            elif final['ok']:
                serial.add(first['file'])
                if first['file'] not in baseline:
                    local_serial.add(first['file'])
                mode += '; serial-only detected'
        records.append((final, mode, elapsed))
    for name in serial_names:
        data = run_file(root, name, 1)
        records.append((data, 'serial', data['seconds']))
    if local_serial != original_local:
        save_serial(serial_file, local_serial)
    elapsed = time.perf_counter() - started
    counts = {key: sum(data[key] for data, _, _ in records) for key in FIELDS}
    failed = any(not data['ok'] for data, _, _ in records)
    sections = ['Fast unittest: ' + str(jobs) + ' worker(s); isolated per-file temp folders\n'] + notes
    for data, mode, seconds in records:
        sections.append('=== ' + data['file'] + ': ' + mode + ', ' + format(seconds, '.3f') + 's ===\n' + data['log'])
    sections.append('\n=== COMBINED FINAL RESULT (reruns replace initial attempts) ===\n')
    sections.append('Ran ' + str(counts['tests']) + ' tests in ' + format(elapsed, '.3f') + 's\n\n')
    details = ', '.join(key + '=' + str(counts[key]) for key in FIELDS[1:] if counts[key])
    sections.append(('FAILED' if failed else 'OK') + (' (' + details + ')' if details else '') + '\n')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('\n'.join(sections), encoding='utf-8')
    print('Fast tests finished: ' + str(counts['tests']) + ' tests, ' + format(elapsed, '.3f') + 's; output: ' + str(output))
    return 1 if failed else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == '--child':
        if len(argv) != 6:
            raise ValueError('invalid child arguments')
        return child(Path(argv[1]), argv[2], Path(argv[3]), Path(argv[4]), int(argv[5]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=0, help='0 = CPU count minus two, capped at eight')
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--serial-list', type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.jobs <= 8:
        parser.error('jobs must be between 1 and 8, or 0 for the default')
    jobs = args.jobs or max(1, min(8, (os.cpu_count() or 1) - 2))
    root = args.root.resolve()
    if not (root / 'tests').is_dir() or not (root / 'tests' / '__init__.py').is_file():
        parser.error('root must contain an importable tests package')
    if '/' in args.pattern or '\\' in args.pattern:
        parser.error('pattern must be a filename pattern')
    serial_file = args.serial_list or root / '.checks' / 'serial_tests.local.txt'
    baseline_file = None if args.serial_list else root / 'crew' / 'SERIAL_TESTS.txt'
    try:
        return run(root, args.output.resolve(), jobs, serial_file, args.pattern, baseline_file)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

if __name__ == '__main__':
    raise SystemExit(main())
