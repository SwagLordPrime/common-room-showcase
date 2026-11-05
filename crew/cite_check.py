"""Mechanical citation check for a worker's report (crew/cite_check.py, piece C / improvement #6
of BUILD_PLAN_COMMON_ROOM_AUTONOMY_2026-09-24.md).

A reviewer (a Claude seat) must check every cited path:line in a worker's report against the
file before accepting it -- local models invent plausible citations (CREW_SEAT_GUIDE_2026-09-23.md
Sec3). This tool does the mechanical part: it finds every citation, resolves it under the
estate root the same careful way common_room/ollama_worker.py resolves an INPUT line (no `..`
escape, no symlink or junction anywhere on the path, the same deny list refused unread), checks
the file exists and the line range is really in it, and -- when a quoted string sits next to the
citation on the same report line -- checks that quote is really in those lines. It never judges
substance; that stays with the reviewer.

Usage:
  python3 cite_check.py --root "<estate root>" <report-file | ->  [--json]
  python3 cite_check.py --root "<estate root>" --relay-answer -

--root is the estate root (D:\\Work Area on Windows, "$HOME/mnt/Work Area" in the Cowork VM). It
must already exist and contain the Artifacts/ and/or Lab Notes/ folders citations are checked
under.

<report-file> is the saved report text to check, or "-" to read it from stdin. With
--relay-answer, the same argument instead names a file holding (or "-" reads from stdin) the
JSON that crew/relay_ask.py prints for a "view_text" request -- see common_room/handoff_api.py
for that shape ({"ok": ..., "result": {"path", "sha256", "text"}, ...}). The report text is
taken from result.text; a non-ok answer, or one with no text, is refused with a clear message
and exit 2.

Recognised citations: `Artifacts/...` or `Lab Notes/...` (with forward or back slashes, and
optionally inside backticks), or the same thing written as an absolute `D:\\Work Area\\...` or
`D:/Work Area/...` path (mapped onto --root); optionally followed by `:N`, `:N-M`, `:LN-LM`,
`#LN` or `#LN-LM`. A bare path with no line locator is a file-only citation. Paths may contain
spaces and most other characters ollama_worker's own INPUT-line resolver accepts (including `&`,
`'` and non-ASCII letters) -- only the separators, `:`, `#` and quoting characters are reserved.
The `Artifacts`, `Lab Notes` and `Work Area` names are recognised in any case (`artifacts/...`,
`LAB NOTES/...`, `d:\\work area\\...`); a citation is recognised only once it reaches one of
ollama_worker's TEXT_EXTENSIONS, exactly as a local worker is only ever allowed to cite those
file types.

For each citation: resolve it under --root (refusing `..`; refusing a path component that ends
in a dot or space, the same way Windows silently trims one; refusing a hard-linked file; and
refusing -- without opening -- any symlink or junction anywhere on the path, and any path
matching the worker's deny rules); check the file exists; check the line range fits inside it;
and, when the same report line holds a quoted string of 12 or more characters (in backticks or
double quotes) near the citation, check it appears, whitespace-normalised and case-sensitive,
within the cited lines +/- 2. Also counts evidence labels (MEASURED, VERIFIED, CLAIMED,
INFERENCE, MISSING, UNVERIFIABLE) anywhere in the report and flags it when there are none.

This tool is deliberately strict about what it *cannot* check: any other path-like token with a
line reference (`something/with/slashes.ext:N`, `#LN`, a tree-relative path such as
`common_room/store.py:12`, a bare file name with no path separator at all such as `relay.py:9999`
or `store.py#L12` -- when it ends in one of TEXT_EXTENSIONS -- or a recognised-root path with an
extension outside TEXT_EXTENSIONS) is reported as `NOT CHECKED (unrecognised location)` rather
than silently ignored, and that result counts as a problem (exit 1) so a reviewer looks at it by
hand. A bare name is only ever flagged when it ends in a recognised text extension immediately
before the `:`/`#` locator, so an ordinary time (`12:30`), a URL's `host:port`, or a version
string (neither has a recognised extension right there) is not mistaken for a citation.

Output: a Markdown table of citation / report line / result, plus totals and the evidence-label
count (default), or with --json one JSON object carrying the same information.

Exit codes: 0 every checked citation is OK and the report uses at least one evidence label;
1 any citation problem (including an unrecognised-location citation), or no evidence labels
found; 2 usage or input error (bad --root, unreadable report, a --relay-answer input that is not
a usable view_text answer).

Caps: at most 200 citations are checked (further ones are counted but not opened); a cited
file is read only up to 1 MB (a citation into a larger file is reported as OUT OF RANGE, since
its line range cannot be confirmed within the cap).

This script is stdlib-only, Python 3.8 grammar, and runs under python3.10 against a
Windows-mounted folder as well as on Windows itself. It never imports the common_room package
(which needs Python 3.12) and it never writes, moves or deletes anything -- it only reads.
"""
import argparse, fnmatch, json, os, re, stat, sys
from pathlib import Path

MAX_CITATIONS = 200
MAX_FILE_BYTES = 1024 * 1024  # 1 MB; a citation into a larger file cannot be range-checked

# Copied literally from common_room/ollama_worker.py (tests/test_cite_check.py asserts equality
# against the live module under python3.12; this module must not import common_room itself,
# since that package needs 3.12 and this script must run under python3.10).
TEXT_EXTENSIONS = frozenset({'.md', '.txt', '.json', '.csv', '.py', '.ps1', '.cmd', '.yaml', '.yml',
                             '.toml', '.html', '.js', '.css', '.log'})
DENIED_COMPONENTS = frozenset({'_private', 'secrets', '.runtime', '.git', '.ssh', '.obsidian', '.config', '.docker',
                               '.aws', '.azure', '.gnupg'})
DENIED_NAMES = ('*.env', '.env*', '*credential*', '*secret*', '*token*', '*password*', '*passwd*', '*key*', '*auth*',
                '*cookie*', '*session*', '*wallet*', 'id_*', 'service-account*', '*.pem', '*.kdbx', '*.pfx', '*.p12',
                '*.sqlite*', '*.db')

RESULT_OK = 'OK'
RESULT_MISSING = 'MISSING FILE'
RESULT_RANGE = 'OUT OF RANGE'
RESULT_QUOTE = 'QUOTE NOT FOUND'
RESULT_SECRET = 'REFUSED (secret-looking path)'
RESULT_LINKED = 'REFUSED (outside root or linked)'
RESULT_NOT_CHECKED = 'NOT CHECKED (unrecognised location)'
RESULT_ORDER = (RESULT_OK, RESULT_MISSING, RESULT_RANGE, RESULT_QUOTE, RESULT_SECRET, RESULT_LINKED,
                RESULT_NOT_CHECKED)

LABELS = ('MEASURED', 'VERIFIED', 'CLAIMED', 'INFERENCE', 'MISSING', 'UNVERIFIABLE')
LABEL_RE = re.compile(r'\b(' + '|'.join(LABELS) + r')\b')

# Reserved characters a path segment may never contain: the two separators, the locator
# prefixes (':' and '#'), and the characters that delimit a quoted string or a backtick-fenced
# citation. Everything else -- spaces, '&', "'", punctuation, non-ASCII letters -- is accepted,
# the same permissive stance common_room/ollama_worker.py's resolve_input takes.
_PATH_SEGMENT = r'[^\\/:#`"\r\n\x00-\x1f]+'
# A tree-relative or otherwise unrooted path segment: like _PATH_SEGMENT but also excludes
# whitespace, since nothing anchors where such a path starts or ends in surrounding prose.
_GENERIC_SEGMENT = r'[^\s\\/:#`"\x00-\x1f]+'
_EXT_PATTERN = '(?:' + '|'.join(re.escape(e) for e in sorted(TEXT_EXTENSIONS, key=len, reverse=True)) + ')'
CITATION_RE = re.compile(
    r'(?<![A-Za-z0-9])'
    r'(?:[A-Za-z]:[\\/](?i:Work Area)[\\/])?'
    r'(?P<root>(?i:Artifacts|Lab Notes))'
    r'(?P<rest>(?:[\\/]' + _PATH_SEGMENT + r')+?)'
    r'(?P<ext>' + _EXT_PATTERN + r')'
    r'(?![A-Za-z0-9_])'
    r'(?:'
    r':(?P<cnum1>L?\d+)(?:-(?P<cnum2>L?\d+))?'
    r'|#(?P<hnum1>L\d+)(?:-(?P<hnum2>L\d+))?'
    r')?'
)
# Anything else path-like with a line reference: a tree-relative slash path
# ('common_room/store.py:12'), or a file named directly under D:\Work Area\ with no
# Artifacts/Lab Notes segment ('D:\Work Area\CREW_BRIEF.md:3'). Deliberately broad -- this
# tool cannot resolve or check either form, so it flags them for a human instead of staying
# silent about them. A locator is required: a bare unrecognised path with no line reference is
# not, by itself, something this tool claims to check.
GENERIC_CITATION_RE = re.compile(
    r'(?<![A-Za-z0-9_])'
    r'(?P<gpath>'
    r'(?:[A-Za-z]:[\\/](?i:Work Area)[\\/](?:' + _PATH_SEGMENT + r'[\\/])*' + _PATH_SEGMENT + r'\.\w{1,8}'
    r'|' + _GENERIC_SEGMENT + r'(?:[\\/]' + _GENERIC_SEGMENT + r')+\.\w{1,8}'
    r')'
    r')'
    r'(?![A-Za-z0-9_])'
    r'(?:'
    r':(?P<cnum1>L?\d+)(?:-(?P<cnum2>L?\d+))?'
    r'|#(?P<hnum1>L\d+)(?:-(?P<hnum2>L\d+))?'
    r')'
)
# N3: a bare file name with NO path separator at all (no slash, no backslash) -- 'relay.py:9999',
# 'agent_worker.py#L873'. GENERIC_CITATION_RE requires at least one separator, so these were
# previously invisible (found 0, exit 0, "Overall: OK") even though they are exactly the shape a
# report cites a source file by. Deliberately narrower than GENERIC_CITATION_RE's own extension
# rule (any \w{1,8}): with no separator to anchor it, an unrestricted extension would also catch a
# time ('12:30' -- excluded anyway, it has no dot), a URL's host:port ('example.com:8080' -- '.com'
# is not a recognised extension) and a version string ('v2.3.4:5' -- '.4' is not one either). So
# the extension right before the locator must be one of TEXT_EXTENSIONS, exactly as a citation
# under a recognised root requires. Same leading segment and lookbehind as GENERIC_CITATION_RE
# (only word characters excluded before the start): a bare match that turns out to be the tail of
# a longer recognised or tree-relative citation (e.g. 'relay.py:615' inside
# 'common_room/relay.py:615') is never a second, separate finding -- find_citations()'s own
# overlap dedup always keeps the longer, earlier-starting match and drops the contained one.
BARE_CITATION_RE = re.compile(
    r'(?<![A-Za-z0-9_])'
    r'(?P<bname>' + _GENERIC_SEGMENT + _EXT_PATTERN + r')'
    r'(?![A-Za-z0-9_])'
    r'(?:'
    r':(?P<cnum1>L?\d+)(?:-(?P<cnum2>L?\d+))?'
    r'|#(?P<hnum1>L\d+)(?:-(?P<hnum2>L\d+))?'
    r')'
)
QUOTE_RE = re.compile(r'`([^`\n]{12,})`|"([^"\n]{12,})"')


class UsageError(Exception):
    """A problem with the command line or the input, not with a citation. Exit code 2."""


def _denied(parts):
    for part in parts:
        name = part.casefold()
        if name in DENIED_COMPONENTS or any(fnmatch.fnmatchcase(name, pattern) for pattern in DENIED_NAMES):
            return part
    return None


def _is_link_or_junction(path):
    """True for a symlink (any platform) or a Windows junction/reparse point. Never raises: a
    missing or inaccessible path is reported as "not a link" here, so a missing intermediate
    directory falls through to the ordinary MISSING FILE check instead of a false refusal."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, 'st_file_attributes', None)
    reparse_bit = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return bool(attrs is not None and attrs & reparse_bit)


def _normalize_ws(text):
    return re.sub(r'\s+', ' ', text).strip()


def _parse_locator(match):
    if match.group('cnum1'):
        first = int(match.group('cnum1').lstrip('L'))
        second = match.group('cnum2')
        last = int(second.lstrip('L')) if second else first
        return first, last
    if match.group('hnum1'):
        first = int(match.group('hnum1')[1:])
        second = match.group('hnum2')
        last = int(second[1:]) if second else first
        return first, last
    return None


def _nearby_quote(line, start, end):
    """The quoted string (backtick- or double-quote-delimited, 12+ chars) on this report line
    closest to the citation's own span, skipping any quote that overlaps the citation itself
    (a citation written inside backticks would otherwise look like its own 12+ char quote)."""
    best_text, best_distance = None, None
    for match in QUOTE_RE.finditer(line):
        if match.start() < end and match.end() > start:
            continue
        distance = min(abs(match.start() - end), abs(start - match.end()))
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_text = match.group(1) if match.group(1) is not None else match.group(2)
    return best_text


def _canonical_root(raw_root):
    """The real, correctly-cased estate folder name for a root matched case-insensitively."""
    return 'Artifacts' if raw_root.casefold() == 'artifacts' else 'Lab Notes'


def _prefixed_root(line, root_start, found):
    """True when an overlapping unrecognised match starts before this root match and has a path
    separator before it, and the text right before the root is not the estate's own folder name."""
    if line[:root_start].casefold().endswith(('work area\\', 'work area/')):
        return False
    for start, end, kind, _ in found:
        if kind != 'root' and start < root_start < end and re.search(r'[\\/]', line[start:root_start]):
            return True
    return False


def find_citations(report_text):
    """Every recognised citation in the report, in the order it appears, one dict each:
    citation (display text), line (1-indexed report line), kind ('root' if it resolves under an
    estate root, or 'unrecognised' for a path-like citation this tool cannot resolve), parts
    (path components including the root name, or None for 'unrecognised'), locator (first, last)
    or None, quote (nearby quoted text) or None.

    CITATION_RE, GENERIC_CITATION_RE and BARE_CITATION_RE are all tried on every line. Overlaps
    are resolved by preferring a 'root' match over an 'unrecognised' one -- even when the
    unrecognised match starts earlier -- and otherwise by whichever starts first; the result is
    then put back in the order matches appear on the line. Root priority matters because a
    citation written wrapped (in parentheses, a Markdown link, bold, quotes, ...) makes the
    generic/bare pattern's own leading segment swallow the wrapping character, so its match starts
    one character before the recognised citation's own match and would otherwise win the overlap
    purely by starting earlier (N2 regression: `(Artifacts/x.md:3)` was reported NOT CHECKED
    instead of checked)."""
    citations = []
    for line_no, line in enumerate(report_text.splitlines(), start=1):
        found = []
        for match in CITATION_RE.finditer(line):
            found.append((match.start(), match.end(), 'root', match))
        for match in GENERIC_CITATION_RE.finditer(line):
            found.append((match.start(), match.end(), 'unrecognised', match))
        for match in BARE_CITATION_RE.finditer(line):
            found.append((match.start(), match.end(), 'unrecognised', match))
        # VERIFY9 N2: a root match preceded, inside an overlapping path-like match, by another folder
        # (backup/Artifacts/..., C:\Users\me\Artifacts\..., a web URL) is not the estate's file:
        # drop it so the longer match reports it NOT CHECKED. The estate's own absolute forms end in
        # "Work Area\" or "Work Area/" right before the root and stay checked.
        found = [item for item in found if item[2] != 'root' or not _prefixed_root(line, item[0], found)]
        found.sort(key=lambda item: (item[2] != 'root', item[0]))
        kept = []
        for start, end, kind, match in found:
            if any(start < e2 and end > s2 for s2, e2, _, _ in kept):
                continue
            kept.append((start, end, kind, match))
        kept.sort(key=lambda item: item[0])
        for start, end, kind, match in kept:
            if kind == 'root':
                canonical_root = _canonical_root(match.group('root'))
                raw_path = canonical_root + match.group('rest') + match.group('ext')
                parts = [part for part in re.split(r'[\\/]+', raw_path) if part not in ('', '.')]
                locator = _parse_locator(match)
                quote = _nearby_quote(line, start, end) if locator else None
                citations.append({'citation': match.group(0), 'line': line_no, 'kind': 'root',
                                   'parts': parts, 'locator': locator, 'quote': quote})
            else:
                citations.append({'citation': match.group(0), 'line': line_no, 'kind': 'unrecognised',
                                   'parts': None, 'locator': _parse_locator(match), 'quote': None})
    return citations


def check_citation(root, cite):
    """(result, detail) for one citation dict from find_citations(), resolved under root
    (a Path). detail is a short human-readable reason, or None for RESULT_OK."""
    parts = cite['parts']
    if '..' in parts:
        return RESULT_LINKED, 'parent traversal (..) refused'
    bad = next((part for part in parts if part != part.rstrip('. ')), None)
    if bad is not None:
        return RESULT_LINKED, 'path component ends in a dot or space, which Windows silently trims (%s)' % bad
    hit = _denied(parts)
    if hit:
        return RESULT_SECRET, 'denied path component or file name (%s)' % hit
    candidate = root
    for part in parts:
        candidate = candidate / part
        if _is_link_or_junction(candidate):
            return RESULT_LINKED, 'symlink or junction in the path refused'
    try:
        real = candidate.resolve(strict=False)
        real_root = root.resolve(strict=False)
        real.relative_to(real_root)
    except (OSError, ValueError):
        return RESULT_LINKED, 'resolves outside the estate root'
    hit = _denied(Path(os.path.relpath(str(real), str(real_root))).parts)
    if hit:
        return RESULT_SECRET, 'denied path component or file name (%s)' % hit
    if not candidate.is_file():
        return RESULT_MISSING, 'not found under the estate root'
    try:
        info = candidate.stat()
    except OSError:
        return RESULT_MISSING, 'not found under the estate root'
    if info.st_nlink != 1:
        return RESULT_LINKED, 'hard-linked file refused (link count %d)' % info.st_nlink
    size = info.st_size
    if size > MAX_FILE_BYTES:
        return RESULT_RANGE, 'file is larger than the 1 MB read cap (%d bytes); its line range could not be checked' % size
    try:
        # A bare, module-level open() call (not Path.read_bytes) so a denied path -- which
        # always returns above, before this line is ever reached -- is verifiably never opened:
        # patching builtins.open (or os.open, which open() calls in turn) proves it in a test.
        with open(str(candidate), 'rb') as handle:
            data = handle.read()
    except OSError:
        return RESULT_MISSING, 'could not be read'
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return RESULT_MISSING, 'not valid UTF-8 text'
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    total = len(lines)
    locator = cite['locator']
    if locator is None:
        return RESULT_OK, None
    first, last = locator
    if first < 1 or last < first or first > total or last > total:
        return RESULT_RANGE, 'lines %d-%d requested; file has %d line(s)' % (first, last, total)
    quote = cite['quote']
    if quote:
        low = max(1, first - 2)
        high = min(total, last + 2)
        window = '\n'.join(lines[low - 1:high])
        if _normalize_ws(quote) not in _normalize_ws(window):
            return RESULT_QUOTE, 'quoted text not found within lines %d-%d' % (low, high)
    return RESULT_OK, None


def count_labels(report_text):
    counts = dict((label, 0) for label in LABELS)
    for match in LABEL_RE.finditer(report_text):
        counts[match.group(1)] += 1
    return counts


def _read_source(path):
    if path == '-':
        return sys.stdin.read()
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read()
    except OSError as exc:
        raise UsageError('cannot read %s: %s' % (path, exc))
    except UnicodeDecodeError as exc:
        raise UsageError('%s is not UTF-8 text: %s' % (path, exc))


def load_report(report_arg, relay_answer):
    raw = _read_source(report_arg)
    if not relay_answer:
        return raw
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise UsageError('relay answer is not valid JSON: %s' % exc)
    if not isinstance(answer, dict):
        raise UsageError('relay answer must be a JSON object')
    if not answer.get('ok'):
        raise UsageError('relay answer is not ok (error=%r): %s' % (answer.get('error'), answer.get('message')))
    result = answer.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('text'), str):
        raise UsageError('relay answer has no report text (expected a view_text answer with a result.text string)')
    return result['text']


def run(root_arg, report_arg, relay_answer):
    root = Path(root_arg)
    if not root.is_dir():
        raise UsageError('--root %s is not a directory' % root_arg)
    report_text = load_report(report_arg, relay_answer)
    citations = find_citations(report_text)
    checked = citations[:MAX_CITATIONS]
    capped = len(citations) > MAX_CITATIONS
    rows = []
    by_result = {}
    for cite in checked:
        if cite.get('kind') == 'unrecognised':
            result, detail = RESULT_NOT_CHECKED, ('a path-like citation with a line reference that is not '
                'one of the recognised Artifacts/, Lab Notes/ or D:\\Work Area\\ forms this tool can '
                'resolve (a tree-relative path, an unlisted extension, or a root-level D:\\Work Area\\ '
                'file); check it by hand')
        else:
            result, detail = check_citation(root, cite)
        by_result[result] = by_result.get(result, 0) + 1
        rows.append({'citation': cite['citation'], 'line': cite['line'], 'result': result, 'detail': detail})
    labels = count_labels(report_text)
    no_labels = sum(labels.values()) == 0
    overall_ok = all(row['result'] == RESULT_OK for row in rows) and not no_labels
    totals = {'found': len(citations), 'checked': len(checked), 'capped': capped, 'by_result': by_result}
    return {'root': str(root), 'citations': rows, 'totals': totals,
            'evidence_labels': labels, 'no_evidence_labels': no_labels, 'ok': overall_ok}


def render_markdown(payload):
    lines = ['| Citation | Report line | Result |', '| --- | --- | --- |']
    if not payload['citations']:
        lines.append('| _(no citations found)_ | | |')
    for row in payload['citations']:
        detail = ' -- ' + row['detail'] if row.get('detail') else ''
        cite_text = row['citation'].replace('|', '\\|')
        lines.append('| `%s` | %d | %s%s |' % (cite_text, row['line'], row['result'], detail))
    totals = payload['totals']
    note = ''
    if totals['capped']:
        note = ' (checked %d of %d found; capped at %d)' % (totals['checked'], totals['found'], MAX_CITATIONS)
    lines.append('')
    lines.append('Totals: %d citation(s) found, %d checked%s.' % (totals['found'], totals['checked'], note))
    for key in RESULT_ORDER:
        count = totals['by_result'].get(key)
        if count:
            lines.append('- %s: %d' % (key, count))
    label_bits = ', '.join('%s=%d' % (label, count) for label, count in payload['evidence_labels'].items() if count)
    lines.append('')
    lines.append('Evidence labels: ' + (label_bits or 'none') + '.')
    if payload['no_evidence_labels']:
        lines.append('**No evidence labels found in this report.**')
    lines.append('')
    lines.append('Overall: %s' % ('OK' if payload['ok'] else 'PROBLEMS FOUND'))
    return '\n'.join(lines)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='cite_check.py',
        description='Mechanically check every path:line citation in a worker report against the estate root.')
    parser.add_argument('report', metavar='report-file',
                         help='the report to check, or - for stdin; with --relay-answer, the '
                              'relay_ask.py JSON answer instead of the raw report text')
    parser.add_argument('--root', required=True, metavar='ESTATE_ROOT',
                         help='the estate root citations are resolved under')
    parser.add_argument('--json', action='store_true', help='print one JSON object instead of a Markdown table')
    parser.add_argument('--relay-answer', action='store_true', dest='relay_answer',
                         help='the report-file argument is a relay_ask.py view_text answer (JSON), not raw report text')
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        payload = run(args.root, args.report, args.relay_answer)
    except UsageError as exc:
        sys.stderr.write(str(exc) + '\n')
        return 2
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(payload))
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
