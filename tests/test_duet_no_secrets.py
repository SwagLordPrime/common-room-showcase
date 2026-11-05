"""The duet seat's filter proof: the fictional room credential, keeper key, tokens, runtime contents and deny-listed
names planted below are not written to DuetRelay (plan duet-seat-2026-10-30, "What leaves the PC"). It proves those
planted shapes only. The relay removes known secret shapes, but long-path tails, 64-hex strings and lowercase slug
passphrases can pass, and any task title, recent Lounge note or question to or from duet can reach the Drive mirror,
so no seat puts a secret in one.

Every write path is driven with secrets planted in the room's state, in command results and in error messages: the
README at start, the periodic mirror, a state request, a command whose result echoes secrets, a malformed file
after it settles, a CrewError carrying the origin and the token, and the blocked-secret fallback. Then every file
under the DuetRelay folder is walked and no planted string may occur in any byte. Fictional values only.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_duet_no_secrets.py` run from a tree root

import json, unittest
from unittest import mock

from common_room import duet_relay
from tests.test_duet_relay import DuetTestCase, FakeClient, OWN_TOKEN, TASK_MINE, TASK_OTHER
from common_room.crew_client import CrewError
from common_room.duet_mirror import BlockedSecret, WITHHELD, guard, scrub

KEEPER_KEY = 'KpR7vQ2xZ9mN4bL8cT1wY6hJ3sD5fG0aE-uI_oXyWqV'          # 43 characters, token_urlsafe(32) shape
OTHER_TOKEN_A = 'OtherSeatAlphaTokenAAAAAAAAAAAAAAAAAAAAAAAAAa1'       # 44
OTHER_TOKEN_B = 'vesper_Token-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBb2'       # 41
SESSION_A = 'sess-claude-room-chat-4471-orchestrator'
SESSION_B = 'sess-vesper-window-9902-x'
INSTANCE = '3f9a1c7e5b2d4a608c1e7f93b5d2a4c6'
ORIGIN = 'http://127.0.0.1:50263'
VAULT_IP = '100.64.0.20'
BANK_PATH = r'C:\Users\you\Documents\bank.csv'
KEEPER_PATH = r'D:\Work Area\Artifacts\common-room\tree\.runtime\handoffs\keeper-credential.json'

# Nothing under DuetRelay may contain any of these, in any byte.
PLANTS = [OWN_TOKEN, KEEPER_KEY, OTHER_TOKEN_A, OTHER_TOKEN_B, SESSION_A, SESSION_B, INSTANCE, ORIGIN,
          '127.0.0.1:50263', VAULT_IP, BANK_PATH, 'bank.csv', '2,345.67', '.runtime', '-credential.json',
          'keeper-credential', 'vesper-credential', '_Private', 'secrets/api.env', 'api.env', r'Users\shane']

LOUNGE = [
    (1, 'SHANE UPDATE: answering #12: yes, go', 'same'),
    (2, 'claude-code-b: suite green on 5d5ee83, 1609 OK', 'same'),
    (3, 'QUESTION FOR SHANE: approve the keeper key rotation?', 'same'),
    (4, 'astra-audit accepted ' + TASK_MINE + ' (note 4533)', 'same'),
    (5, 'reviewing Artifacts/orchestra/x/REPORT.md', 'same'),
    (6, 'The Vault at ' + VAULT_IP + ' answered the health check', 'The Vault at [ip] answered the health check'),
    (7, r'handoffs now live under .runtime\handoffs on the PC', WITHHELD),
    (8, 'the new room key is ' + KEEPER_KEY + ' please rotate', 'the new room key is [key] please rotate'),
    (9, 'copy vesper-credential.json to the other seat', WITHHELD),
]


def planted_state(client):
    state = client.state_body
    state.update(instance_id=INSTANCE, origin=ORIGIN, connection={'mode': 'codex', 'token': OTHER_TOKEN_A})
    state['tasks'] = [
        {'id': TASK_MINE, 'title': 'rotate ' + KEEPER_KEY, 'status': 'delivered', 'sender': 'claude-room',
         'recipient': 'duet', 'builder': None, 'parent_id': None, 'authority_id': 'orchestra', 'failed_attempts': 0,
         'lease': None, 'body': 'tokens ' + OTHER_TOKEN_B},
        {'id': TASK_OTHER, 'title': 'read ' + KEEPER_PATH, 'status': 'waiting_decision', 'sender': 'keeper',
         'recipient': 'vesper', 'builder': None, 'parent_id': None, 'authority_id': 'orchestra', 'failed_attempts': 1,
         'lease': {'holder': 'vesper', 'fence': 2, 'until': 2000.0}}]
    state['notes'] = [{'seq': seq, 'sender': 'claude-room', 'authority_id': 'orchestra', 'body': body,
                       'timestamp': 900.0 + seq, 'sha256': 'f' * 64} for seq, body, _ in LOUNGE]
    state['notes'].append({'seq': 10, 'sender': 'vesper', 'authority_id': 'orchestra', 'timestamp': 910.0,
                           'body': 'open ' + BANK_PATH + ' and _Private\\x.md and secrets/api.env, paid $2,345.67'})
    state['participants'] = [
        {'id': 'duet', 'role': 'worker', 'contact_age_s': 3, 'session': SESSION_A, 'label': 'Duet ' + SESSION_B,
         'contact': 'Credential contact active', 'key_active': True},
        {'id': 'vesper', 'role': 'reviewer', 'contact_age_s': 40, 'session': SESSION_B, 'instance_id': INSTANCE}]
    state['needs'] = [{'id': 'question:3', 'kind': 'question', 'since': 903.0,
                       'summary': 'QUESTION from claude-room (#3): approve the keeper key rotation? ' + VAULT_IP,
                       'action': 'Answer in the claude-room chat'}]
    state['questions'] = [{'seq': 3, 'to': 'duet', 'from': 'claude-room', 'authority_id': 'orchestra',
                           'marks': {'read': 1.0, 'accepted': None, 'checked': None}, 'origin': ORIGIN}]
    return state


class NoSecretsTests(DuetTestCase):
    def walk(self):
        """Every file the relay wrote under DuetRelay (requests/ is Duet's own input, not the relay's output)."""
        found = {p.relative_to(self.drive).as_posix(): p.read_bytes() for p in self.drive.rglob('*') if p.is_file()}
        return {n: d for n, d in found.items() if not n.startswith('requests/')}

    def assert_clean(self, files=None):
        files = self.walk() if files is None else files
        self.assertTrue(files)
        for name, data in files.items():
            lowered = data.lower()
            for plant in PLANTS + [KEEPER_PATH, 'handoffs\\keeper']:
                self.assertNotIn(plant.encode('utf-8'), data, name + ' holds ' + plant)
                self.assertNotIn(plant.lower().encode('utf-8'), lowered, name + ' holds (any case) ' + plant)
                self.assertNotIn(json.dumps(plant)[1:-1].encode('utf-8'), data, name + ' holds (escaped) ' + plant)
            guard(data, [OWN_TOKEN, KEEPER_KEY, OTHER_TOKEN_A, OTHER_TOKEN_B])
        return files

    def test_every_write_path_leaves_nothing_private_under_duetrelay(self):
        self.make(mirror_every=300)
        client = self.client
        # 1. the README, written at start
        self.assert_clean()
        # 2. a periodic cycle with no request at all, with secrets planted in the room's state
        planted_state(client)
        self.cycle()
        self.assertTrue((self.drive / 'mirror' / 'board.json').exists())
        files = self.assert_clean()
        mirror = json.loads(files['mirror/board.json'])
        titles = {t['id']: t['title'] for t in mirror['tasks']}
        self.assertEqual(titles[TASK_MINE], 'rotate [key]')
        self.assertEqual(titles[TASK_OTHER], WITHHELD)
        self.assertEqual(mirror['participants'][0], {'id': 'duet', 'role': 'worker', 'contact_age_s': 3})
        self.assertIn('[ip]', mirror['needs'][0]['summary'])
        last = [n for n in mirror['notes'] if n['seq'] == 10][0]['body']
        self.assertIn('[withheld', last, 'the _Private/secrets note is withheld whole')
        # 3. a state request
        self.request('state.json', {'request_id': 'state-1', 'kind': 'state'})
        self.cycle()
        self.assertTrue(self.response('state-1')['ok'])
        self.assert_clean()
        # 4a. a command whose result echoes other secrets (not duet's own): written, redacted
        client.command_result = {'task': {'id': TASK_MINE, 'status': 'acknowledged', 'title': 'x ' + KEEPER_KEY,
                                          'note': 'see ' + KEEPER_PATH + ' at ' + VAULT_IP + ' paid $2,345.67',
                                          'token': OTHER_TOKEN_A, 'session': SESSION_A, 'instance_id': INSTANCE,
                                          'origin': ORIGIN, 'where': BANK_PATH + ' and vesper-credential.json'}}
        self.request('ack.json', {'request_id': 'ack-1', 'kind': 'command', 'action': 'ack',
                                  'payload': {'task_id': TASK_MINE}})
        self.cycle()
        echoed = self.response('ack-1')
        self.assertTrue(echoed['ok'])
        self.assertEqual(echoed['result']['task']['title'], 'x [key]')
        self.assertNotIn('token', echoed['result']['task'])
        self.assert_clean()
        # 4b. a command whose result echoes duet's OWN token: nothing of it is written; the short answer is
        client.command_result = {'task': {'id': TASK_MINE, 'status': 'x', 'note': 'your key is ' + OWN_TOKEN}}
        self.request('ack2.json', {'request_id': 'ack-2', 'kind': 'command', 'action': 'ack',
                                   'payload': {'task_id': TASK_MINE}})
        lines = self.cycle()
        withheld = self.response('ack-2')
        self.assertEqual(withheld, {
            'ok': True, 'result': 'withheld', 'performed': True, 'kind': 'command', 'request_id': 'ack-2',
            'message': 'The room did this, but its answer held a private value, so the answer was not written. Ask on the PC.',
            'request_file': 'ack2.json', 'request_sha256': withheld['request_sha256']})
        self.assertEqual(client.actions()[-1], 'ack', 'the room did receive and perform it')
        self.assertIn('blocked_secret', [line['error'] for line in lines])
        self.assert_clean()
        # 5. a malformed file, answered only after it settles
        client.command_result = None
        self.request('torn.json', b'{"request_id": "torn-1", "kind": "comm' + OTHER_TOKEN_A.encode())
        self.cycle()
        self.clock.t += 200
        self.cycle()
        torn = json.loads((self.drive / 'responses' / 'resp-invalid-torn.json').read_text(encoding='utf-8'))
        self.assertEqual(torn['request_file'], 'torn.json')
        self.assert_clean()
        # 6a. a CrewError carrying the origin and duet's own token: blocked
        client.command_error = CrewError('error', 'POST ' + ORIGIN + '/api/command failed for token ' + OWN_TOKEN)
        self.request('err1.json', self.note_request('err-1', 'hi'))
        lines = self.cycle()
        refused = self.response('err-1')
        self.assertEqual((refused['ok'], refused['error'], refused['performed'], refused['kind']),
                         (False, 'blocked', False, 'command'))
        self.assertIn('blocked_secret', [line['error'] for line in lines])
        self.assert_clean()
        # 6b. a CrewError carrying only the origin and a path: written, redacted
        client.command_error = CrewError('error', 'POST ' + ORIGIN + '/api/command failed near ' + BANK_PATH)
        self.request('err2.json', self.note_request('err-2', 'hi'))
        self.cycle()
        redacted = self.response('err-2')
        self.assertEqual(redacted['error'], 'error')
        self.assertIn('[ip]', redacted['message'])
        self.assertIn('[path]', redacted['message'])
        self.assert_clean()
        # 7. duet's own token inside one note of the room's state: that note alone is left out of the mirror
        client.state_body['notes'].append({'seq': 11, 'sender': 'claude-room', 'authority_id': 'orchestra',
                                           'timestamp': 911.0, 'body': 'oops ' + OWN_TOKEN})
        client.state_body['server_time'] = 5000.0
        self.clock.t += 400
        lines = self.cycle()
        mirror = json.loads((self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8'))
        self.assertEqual(mirror['generated_at'], 5000.0, 'the mirror was rewritten')
        self.assertNotIn(11, [n['seq'] for n in mirror['notes']])
        self.assertEqual([n['seq'] for n in mirror['notes']], list(range(1, 11)))
        self.assertIn('blocked_item', [line['error'] for line in lines])
        self.request('state2.json', {'request_id': 'state-2', 'kind': 'state'})
        self.cycle()
        self.assertTrue(self.response('state-2')['ok'])
        # 8. the final walk, over every file, in every byte
        files = self.assert_clean()
        self.assertEqual(sorted(n.replace('\\', '/') for n in files if not n.startswith('requests')),
                         ['README.md', 'mirror/board.json'] + sorted('responses/' + n for n in self.responses()))
        self.assert_nothing_created_in_drive()

    def test_token_is_reread_each_cycle(self):
        self.make()
        planted_state(self.client)
        self.cycle()
        rotated = 'RotatedTokenAbCdEfGhIjKlMnOpQrStUvWxYz987654'
        self.write_credential(rotated)
        self.client.state_body['notes'].append({'seq': 12, 'sender': 'x', 'authority_id': 'orchestra',
                                                'timestamp': 912.0, 'body': 'leak ' + rotated})
        self.clock.t += 400
        lines = self.cycle()
        mirror = json.loads((self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8'))
        self.assertNotIn(12, [n['seq'] for n in mirror['notes']], 'the note holding the new key is left out')
        self.assertIn(10, [n['seq'] for n in mirror['notes']], 'the other notes are still there')
        self.assertIn('blocked_item', [line['error'] for line in lines])
        self.assertNotIn(rotated.encode(), (self.drive / 'mirror' / 'board.json').read_bytes())

    def test_ordinary_lounge_notes_keep_their_words_and_only_credential_shaped_ones_are_withheld(self):
        self.make()
        planted_state(self.client)
        self.cycle()
        mirror = json.loads((self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8'))
        bodies = {n['seq']: n['body'] for n in mirror['notes']}
        withheld = 0
        for seq, original, expected in LOUNGE:
            got = bodies[seq]
            expected = original if expected == 'same' else expected
            self.assertEqual(got, expected, 'note %d' % seq)
            withheld += got == WITHHELD
        print('\nLounge fixture: %d notes, %d withheld whole, %d kept word for word' % (
            len(LOUNGE), withheld, sum(1 for _, o, e in LOUNGE if e == 'same')))
        self.assertEqual(withheld, 2)
        self.assertEqual(bodies[4], 'astra-audit accepted ' + TASK_MINE + ' (note 4533)', 'task ids survive')

    def test_scrub_and_guard_rules_directly(self):
        self.assertEqual(scrub({'authority_id': 'orchestra', 'author': 'a', 'authorized_by': 'keeper', 'origin_seq': 3,
                                'tokens_used': 5, 'session_count': 1, 'token': 'x', 'a_key': 1, 'key': 2, 'api_key': 4,
                                'Cookie': 3, 'sha256': 'ab' * 32, 'n': 5, 'ok': True, 'none': None, 'instance_id': 'i',
                                'origin': 'o', 'authorization': 'z', 'auth_token': 't', 'x_secret': 1, 'session_id': 's',
                                'credentials': 1, 'my-token': 2, 'C:\\a\\b': 'path as key'}),
                         {'authority_id': 'orchestra', 'author': 'a', 'authorized_by': 'keeper', 'origin_seq': 3,
                          'tokens_used': 5, 'session_count': 1, 'n': 5, 'ok': True, 'none': None})
        self.assertEqual(scrub('authority session keeper keys token talk'), 'authority session keeper keys token talk')
        self.assertEqual(scrub('see foo/keyboard.md'), 'see [private name]')
        self.assertEqual(scrub('mail $12 and $1,234.56 on 1234567 or 123456'), 'mail [amount] and [amount] on [number] or 123456')
        self.assertEqual(scrub('ip 10.0.0.1 and /c/Users/x /home/u/y /mnt/z'), 'ip [ip] and [path] [path] [path]')
        self.assertEqual(scrub(r'\\host\share\f.txt and E:\a\b.md'), '[path] and [path]')
        self.assertEqual(scrub(TASK_MINE), TASK_MINE)
        self.assertEqual(scrub(KEEPER_KEY), '[key]')
        with self.assertRaises(BlockedSecret):
            scrub({'a': ['x', 'has ' + OWN_TOKEN]}, secrets=[OWN_TOKEN])
        with self.assertRaises(BlockedSecret):
            scrub('wrapped ' + OWN_TOKEN[:20] + '\n' + OWN_TOKEN[20:], secrets=[OWN_TOKEN])
        for text in ('x ' + OWN_TOKEN, 'keeper-credential', 'a/.runtime/b', 'C:\\_PRIVATE', 'f-credential.json', KEEPER_KEY):
            with self.assertRaises(BlockedSecret):
                guard(text.encode('utf-8'), [OWN_TOKEN])
        guard(('sha ' + 'ab' * 32 + ' results/' + 'cd' * 24 + '.txt').encode('utf-8'))   # digests are not keys

    def test_redaction_gaps_the_reviewer_measured(self):
        """M1: each of these leaked or half-leaked before (Opus review of c6e1918, MEASURED)."""
        sha64 = 'ab' * 32
        cases = [
            # drive paths with spaces, every separator style
            (r'see D:\Work Area\Artifacts\orchestra\x\y.md now', 'see [path] now'),
            ('see D:/Work Area/Artifacts/orchestra/x/y.md now', 'see [path] now'),
            (r'D:\Work Area\Lab Notes\04_Log\Handoffs.md', '[path]'),
            (r'"D:\Work Area\_Vault\TAXONOMY.md"', '"[path]"'),
            (r'\\?\D:\Work Area\x', '[path]'),
            ('/d/Work Area/Artifacts/x', '[path]'),
            (r'C:\Users\you\My Documents\Finance\Chase Statement 2026-09.pdf is ready', '[path] is ready'),
            ('JSON dir D:\\\\Work Area\\\\x.md ok', 'JSON dir [path] ok'),
            # a folder name too long for the match: the safety net takes the fragment the match left behind
            (r'D:\one two three four five\x.md and more', '[path] and more'),
            ('Vault /opt/actual/data and /srv/x /var/log/y /etc/z /root/w', 'Vault [path] and [path] [path] [path] [path]'),
            # addresses, hosts, ports, mail
            ('ollama 127.0.0.1:11434 and http://100.64.0.20:5050/api', 'ollama [ip] and http://[ip]/api'),
            ('vault.tailnet0.ts.net and homepc.tailnet0.ts.net:5050', '[host] and [host]'),
            ('fd7a:115c:a1e0:ab12:4843:cd96:624e:1306 and [::1]:50263 and fd7a:115c:a1e0::6', '[ip] and [ip] and [ip]'),
            ('mail owner@example.com', 'mail [email]'),
            # money and account fragments
            ('balance 12,345.67 and 1200.50 and USD 1,200 and 950 dollars', 'balance [amount] and [amount] and [amount] and [amount]'),
            ('card ending 4242 acct 1234 routing 021000021', 'card ending [number] acct [number] routing [number]'),
            # secrets by name
            ('Finance budget app password: hunter2 here', 'Finance budget app password: [secret] here'),
            ('pwd: Tr0ub4dor and passwd=abc', 'pwd: [secret] and passwd=[secret]'),
            ('ACTUAL_PASSWORD=hunter2 SERVER_TOKEN=abc MY_KEY=k X_SECRET=s', 'ACTUAL_PASSWORD=[secret] SERVER_TOKEN=[secret] MY_KEY=[secret] X_SECRET=[secret]'),
            # known key prefixes, whatever their shape
            ('xoxb-1234567890-1234567890123-abcdefghijklmnopqrstuvwx', '[key]'),
            ('token ghp_1234567890abcdefghijABCDEFGHIJ123456 end', 'token [key] end'),
            ('sk-ant-api03-abcDEF1234567890abcDEF1234 AKIAIOSFODNN7EXAMPLE', '[key] [key]'),
            ('github_pat_11ABCDEFG0abcdefghij_abcdefghijklmnop', '[key]'),
            # hex: only a sha256 and a result file name are exempt
            ('commit 0123456789abcdef0123456789abcdef01234567', 'commit [key]'),
            ('hash ' + sha64, 'hash ' + sha64),
            ('results/' + 'cd' * 24 + '.txt', 'results/' + 'cd' * 24 + '.txt'),
            ('result ' + 'cd' * 24, 'result [key]'),
            ('ids ' + TASK_MINE + ' and branch up-duet-seat-with-a-long-slug-name-for-the-switch', 'ids ' + TASK_MINE + ' and branch up-duet-seat-with-a-long-slug-name-for-the-switch'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(scrub(text), expected)
        self.assertEqual(scrub({'sha256': sha64, 'request_sha256': sha64, 'other_sha256': 'nothex'}, keep_sha256=True),
                         {'sha256': sha64, 'request_sha256': sha64})
        for text in ('a key sk-ant-api03-abcDEF1234567890abcDEF1234 here', 'x ' + 'cd' * 24, 'ghp_1234567890abcdefghijABCDEFGHIJ123456'):
            with self.assertRaises(BlockedSecret):
                guard(text.encode('utf-8'))
        guard(('results/' + 'cd' * 24 + '.txt ' + 'ab' * 32).encode('utf-8'))

    def test_ordinary_words_survive_and_the_secrets_beside_them_do_not(self):
        """N2: both directions for each rule that used to over-redact (probe4 of the Opus re-check)."""
        cases = [
            # password / pwd need a ':' or '=' after the word
            ('the password rule says use the keeper; reset your pwd later', 'the password rule says use the keeper; reset your pwd later'),
            ('password: hunter2 and pwd=abc and PASSWD : x', 'password: [secret] and pwd=[secret] and PASSWD : [secret]'),
            # NAME=value needs no space before '='
            ('sort key = id and key = order', 'sort key = id and key = order'),
            ('token=abc in the query, api_key=zzz', 'token=[secret] in the query, api_key=[secret]'),
            # an email needs a letter-only top-level domain of 2+ letters
            ('install react@18.3.1 and pkg@1.2.3', 'install react@18.3.1 and pkg@1.2.3'),
            ('mail shane@example.com and a.b@c.co.uk', 'mail [email] and [email]'),
            # comma-grouped numbers are money only with a decimal part, a currency sign or a currency word
            ('1,609 tests green; 312.45s; 1609 OK; Ran 57 tests in 3.814s', '1,609 tests green; 312.45s; 1609 OK; Ran 57 tests in 3.814s'),
            ('owed 12,345.67 or $1,200 or USD 1,200 or 1,200 dollars or 1200.50', 'owed [amount] or [amount] or [amount] or [amount] or [amount]'),
            # IPv6 needs 3 groups, a known prefix (fd7a, fe80) or ::1
            ('call std::vector and dead::beef at 12:30:45', 'call std::vector and dead::beef at 12:30:45'),
            ('fe80::1 and [::1]:50263 and fd7a::6 and a1:b2:c3::d4', '[ip] and [ip] and [ip] and [ip]'),
            # a folder path with no extension takes one following capitalised word; a lowercase word is left alone
            (r'see D:\Work Area\Lab Notes and then stop', 'see [path] and then stop'),
            (r'Saved to C:\t\duet and the suite passed in REPORT.md today', 'Saved to [path] and the suite passed in REPORT.md today'),
            (r'Open D:\x\y.md Then reply', 'Open [path] Then reply'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(scrub(text), expected)

    def test_guard_alone_blocks_a_keeper_key_and_duets_token_when_scrub_does_nothing(self):
        """(12) The filter's second line: with scrub switched off, guard still stops the write. A mutation that also
        turns guard off must fail this test."""
        self.make()
        planted_state(self.client)
        identity = lambda value, secrets=(), keep_sha256=False: value
        no_item_scrub = lambda mirror, secrets=(): (mirror, 0)
        with mock.patch.object(duet_relay, 'scrub', identity), mock.patch.object(duet_relay, 'scrub_mirror', no_item_scrub):
            lines = self.cycle()
            self.assertFalse((self.drive / 'mirror' / 'board.json').exists(), 'the keeper key in a title and a note')
            self.assertIn('blocked_secret', [line['error'] for line in lines])
            for index, plant in enumerate((KEEPER_KEY, OWN_TOKEN)):
                self.client.command_result = {'task': {'id': TASK_MINE, 'status': 'x', 'note': 'value ' + plant}}
                self.request('echo%d.json' % index, {'request_id': 'echo-%d' % index, 'kind': 'command', 'action': 'ack',
                                                     'payload': {'task_id': TASK_MINE}})
                self.cycle()
                body = self.response('echo-%d' % index)
                self.assertEqual((body['ok'], body['result'], body['performed']), (True, 'withheld', True))
            self.request('state.json', {'request_id': 'state-1', 'kind': 'state'})
            self.cycle()
            self.assertEqual(self.response('state-1')['error'], 'blocked')
        self.assert_clean()

    def test_end_to_end_amounts_digits_and_spaced_paths_in_an_ordinary_note(self):
        """A note nothing else withholds: the redactions alone must clean it (they were the only line for these)."""
        self.make()
        body = ('Paid 1,200.50 to the vendor, acct 12345678, see D:\\Work Area\\Artifacts\\x.md for details, mail a@b.com, '
                'host vault.tailnet0.ts.net:5006, ssh ' + VAULT_IP + ', invoice 9876543210')
        self.client.state_body['notes'] = [{'seq': 1, 'sender': 'claude-room', 'authority_id': 'orchestra',
                                            'timestamp': 901.0, 'body': body}]
        self.cycle()
        mirror = json.loads((self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8'))
        self.assertEqual(mirror['notes'][0]['body'], 'Paid [amount] to the vendor, acct [number], see [path] for details, '
                                                     'mail [email], host [host], ssh [ip], invoice [number]')
        self.assert_clean()

    def test_planted_finance_and_network_data_in_handoff_answers_is_cut_and_scrubbed(self):
        self.make()
        planted_state(self.client)
        report = ('Import the Chase export from D:\\Work Area\\Lab Notes\\05_Tools\\finance\\companion\\data\\chase 2026-09.csv; '
                  'balance 12,345.67, card ending 4242; Actual at http://vault.tailnet0.ts.net:5006 (fd7a:115c:a1e0::6) '
                  'mail owner@example.com; password: hunter2; vault ' + VAULT_IP + ' ' + KEEPER_KEY)
        banned = ['chase 2026', 'Lab Notes', '12,345.67', '4242', 'tailnet0', 'fd7a', 'owner-name', 'hunter2',
                  KEEPER_KEY, 'REF-Pi-projects-chat']
        plans = [
            ('task_view', {'task_id': TASK_MINE},
             {'task': {'id': TASK_MINE, 'recipient': 'duet', 'sender': 'keeper', 'title': 'Finance S2', 'body': report,
                       'acceptance': report, 'intake': {'external_reference': 'REF-Pi-projects-chat'}},
              'events': [{'seq': 9, 'actor': 'vesper', 'kind': 'task_finished', 'timestamp': 1.0,
                          'payload': {'body': report}}]}),
            ('view_text', {'task_id': TASK_MINE, 'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32},
             {'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32, 'text': report}),
            ('receipt', {'task_id': TASK_MINE},
             {'task_id': TASK_MINE, 'status': 'done', 'external_reference': 'REF-Pi-projects-chat',
              'artifacts': [{'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32}]}),
        ]
        for operation, payload, result in plans:
            self.client.handoff_result = result
            self.request(operation + '.json', {'request_id': operation + '-1', 'kind': 'handoff', 'operation': operation,
                                               'payload': payload})
            self.cycle()
            answer = self.response(operation + '-1')
            self.assertTrue(answer['ok'], operation)
            text = self.response_path(operation + '-1').read_text(encoding='utf-8')
            for plant in banned:
                self.assertNotIn(plant, text, operation + ' holds ' + plant)
            self.assertIn('Import the', text) if operation != 'receipt' else None
        # the same reads on another seat's task are refused before anything is sent
        before = len(self.client.calls)
        for operation in ('task_view', 'receipt'):
            self.request(operation + '-x.json', {'request_id': operation + '-2', 'kind': 'handoff', 'operation': operation,
                                                 'payload': {'task_id': TASK_OTHER}})
        self.cycle()
        for operation in ('task_view', 'receipt'):
            self.assertEqual(self.response(operation + '-2')['error'], 'not_yours')
        self.assertEqual(len(self.client.calls), before)
        self.assert_clean()


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
