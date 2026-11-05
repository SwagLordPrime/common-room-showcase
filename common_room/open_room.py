"""Open or resume the one explicitly saved room; never probe with a credential."""
import argparse,http.client,json,re,webbrowser
from pathlib import Path
from .activation import ROOT,safe_path,read_json,validate_enrollment,start_room,run_console

REQUIRED=['room.sqlite','room-mode.json','enrollment.json','participant-selection.json','activation-state.json','connection.json']
def descriptor(root):
    d=read_json(root,'connection.json',None)
    if type(d) is not dict or type(d.get('origin')) is not str or not re.fullmatch(r'http://127[.]0[.]0[.]1:[0-9]{1,5}',d['origin']) or not re.fullmatch('[0-9a-f]{32}',str(d.get('instance_id'))):raise ValueError('Invalid saved room descriptor.')
    if not 1<=int(d['origin'].rsplit(':',1)[1])<=65535:raise ValueError('Invalid saved port.')
    return d

def probe(d):
    conn=http.client.HTTPConnection('127.0.0.1',int(d['origin'].rsplit(':',1)[1]),timeout=3)
    try:
        conn.request('GET','/api/instance',headers={'Host':d['origin'][7:]})
        response=conn.getresponse();raw=response.read(2049)
        if response.status!=200 or len(raw)>2048:raise ValueError('The saved port is not the expected room. Nothing was opened or changed.')
        found=json.loads(raw)
        expected={'protocol':'common-room-handoffs-v1','instance_id':d['instance_id']}
        if 'build' in d:expected['build']=d['build']
        if found!=expected:raise ValueError('The running room does not match its saved instance. Nothing was changed.')
        return True
    except ConnectionRefusedError:return False
    except (OSError,http.client.HTTPException,json.JSONDecodeError) as exc:raise ValueError('Cannot verify the saved room. No alternate room was started.') from exc
    finally:conn.close()

def open_saved(root=None,launch_browser=True):
    root=Path(root or ROOT/'.runtime'/'handoffs').absolute()
    if not any(root.resolve().is_relative_to((ROOT/n).resolve()) and root.resolve()!=(ROOT/n).resolve() for n in ['.runtime','.checks']):raise ValueError('Use the configured package runtime.')
    safe_path(ROOT,root)
    for name in REQUIRED:
        p=safe_path(root,root/name)
        if not p.is_file():raise ValueError('Saved room setup is incomplete: '+name+'. Use explicit setup; no new room was created.')
    enrollment=validate_enrollment(read_json(root,'enrollment.json',None))
    selection=read_json(root,'participant-selection.json',None)
    if type(selection) is not dict or set(selection)!={'version','participants'} or type(selection['version']) is not int or selection['version']!=1 or type(selection['participants']) is not list:raise ValueError('Invalid saved participant selection.')
    ids={a['id'] for a in enrollment['actors']}
    if any(type(x) is not str or x not in ids for x in selection['participants']) or len(set(selection['participants']))!=len(selection['participants']):raise ValueError('Saved selection must contain distinct enrolled participant identities.')
    d=descriptor(root);room=None
    settings=read_json(root,'activation-state.json',None)
    if type(settings) is not dict or settings.get('port')!=int(d['origin'].rsplit(':',1)[1]):raise ValueError('Saved port and descriptor differ.')
    if not probe(d):
        try:room=start_room(root,selected=selection['participants'])
        except OSError:
            # A simultaneous explicit opener may have won the room lock. Recheck once.
            d=descriptor(root)
            if not probe(d):raise ValueError('Room could not be started; no alternate port or store was used.')
        if room:d=room.descriptor
    if launch_browser:webbrowser.open(d['origin'])
    return room

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path);p.add_argument('--no-browser',action='store_true');p.add_argument('--headless',action='store_true');a=p.parse_args()
    try:
        room=open_saved(a.root,not a.no_browser)
        if room:run_console(room,headless=a.headless)
        else:print('Opened the existing Common Room. The existing room remains in charge.')
    except (ValueError,OSError) as exc:print('Common Room: '+str(exc));raise SystemExit(1)
if __name__=='__main__':main()
