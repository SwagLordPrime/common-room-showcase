"""Is an older Common Room still running on this room folder? Read-only; stops nothing.

Reads the non-secret connection.json and background-process.json, then asks the advertised
origin for its credential-free /api/instance. Exit codes:
  0  nothing answers: safe to start
  3  the room recorded in background-process.json answers; its PID is printed with --pid
  4  something answers on the port but it is not the recorded room: do not stop anything
  2  descriptor files unreadable
Never reads a credential file.
"""
import argparse, http.client, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def live_instance(origin, timeout=2.0):
    if not isinstance(origin, str) or not origin.startswith('http://127.0.0.1:'):
        return None
    port = int(origin.rsplit(':', 1)[1])
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request('GET', '/api/instance', headers={'Host': '127.0.0.1:%d' % port})
        response = conn.getresponse()
        if response.status != 200:
            return {'instance_id': None, 'status': response.status}
        return json.loads(response.read(65536))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def check(room_root, clock=time.time):
    try:
        connection = read_json(Path(room_root) / 'connection.json')
        process = read_json(Path(room_root) / 'background-process.json')
    except (OSError, ValueError) as exc:
        return 2, {'problem': 'descriptor unreadable: %s' % type(exc).__name__}
    if not connection:
        return 0, {'running': False, 'reason': 'no connection.json yet'}
    live = live_instance(connection.get('origin'))
    if live is None:
        return 0, {'running': False, 'origin': connection.get('origin')}
    info = {'running': True, 'origin': connection.get('origin'), 'live_instance': live.get('instance_id'),
            'keys_expired': bool(connection.get('expires_at')) and connection['expires_at'] <= clock()}
    if process and live.get('instance_id') and live['instance_id'] == process.get('instance_id') == connection.get('instance_id'):
        info['pid'] = process.get('pid')
        return 3, info
    return 4, info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=ROOT / '.runtime' / 'handoffs')
    parser.add_argument('--pid', action='store_true', help='print only the recorded PID when code is 3')
    args = parser.parse_args(argv)
    code, info = check(args.root)
    if args.pid:
        if code == 3:
            print(info['pid'])
    else:
        print(json.dumps(info))
    return code


if __name__ == '__main__':
    sys.exit(main())
