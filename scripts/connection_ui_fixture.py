"""Disposable fictional connection for actual browser checks; never a provider."""
from pathlib import Path
import sys,json,threading,secrets
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tests.test_store import StoreFixture
from tests.test_connection import FakeTransport
from common_room.connection import Dispatcher,RunController
from common_room.board import make_server
fixture=StoreFixture();fixture.setUp();transport=FakeTransport()
controller=RunController(Dispatcher(fixture.store,fixture.builder,fixture.reviewer,transport,fixture.home/'transport'))
key=secrets.token_urlsafe(32);server=make_server(fixture.store,{key:fixture.owner},controller=controller)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
output=Path(sys.argv[1]).resolve()
if not output.is_relative_to((ROOT/'.checks').resolve()) or output.exists():raise ValueError('Use a new .checks connection file')
output.write_text(json.dumps({'origin':server.origin,'key':key}),encoding='utf-8')
try:input()
finally:
    controller.close();server.shutdown();server.server_close();thread.join(3)
    output.with_suffix('.result.json').write_text(json.dumps({'provider_calls':0,'fake_transport_calls':len(transport.calls),'tasks':fixture.store.list_tasks(fixture.owner)}),encoding='utf-8')
