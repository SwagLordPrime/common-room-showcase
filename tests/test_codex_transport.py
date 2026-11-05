"""Parser and fixed invocation tests never contact a model."""
from unittest import TestCase,mock
from pathlib import Path
import hashlib,json,sys
from common_room.codex_transport import CodexTransport
from common_room.protocol import RoomError
from tests.helpers import case_dir

class CodexTransportTests(TestCase):
    def setUp(self):
        self.root=case_dir('codex-transport');self.home=self.root/'cli-home';self.home.mkdir()
        patch=mock.patch('common_room.codex_transport.PINNED_CLI_SHA256',hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest());patch.start();self.addCleanup(patch.stop)
    def transport(self):return CodexTransport(Path(sys.executable),self.home)
    def output(self,extra=None):
        events=[{'type':'thread.started','thread_id':'fixture-run'}, {'type':'turn.started'}]
        if extra:events.append(extra)
        events.extend([{'type':'item.completed','item':{'type':'agent_message','text':'{"words":3,"characters":17}'}},{'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}])
        return {'returncode':0,'stdout':('\n'.join(json.dumps(e) for e in events)+'\n').encode(),'stderr':b''}
    def invoke(self,output):
        transport=self.transport()
        with mock.patch('common_room.codex_transport.bounded_process',return_value=output) as process:
            result=transport.invoke('worker','One quiet orchard',None,self.root/('task-'+__import__('uuid').uuid4().hex),'invocation-fixture')
            return result,process.call_args
    def test_fixed_no_tool_policy_and_stdin_data(self):
        result,args=self.invoke(self.output())
        self.assertEqual(result,{'output':{'words':3,'characters':17},'provider_run_id':'fixture-run'})
        argv=args.args[0];self.assertNotIn('--sandbox',argv);self.assertIn('--ignore-user-config',argv)
        self.assertIn('default_permissions="common_room_task"',argv);self.assertIn('tools.experimental_request_user_input.enabled=false',argv)
        self.assertNotIn('One quiet orchard',argv);self.assertIn(b'One quiet orchard',args.args[1]);self.assertEqual(args.args[3],30)
        catalog=json.loads((Path(argv[argv.index('-C')+1])/'models.json').read_text());self.assertIsNone(catalog['models'][0]['apply_patch_tool_type']);self.assertEqual(catalog['models'][0]['shell_type'],'disabled')
    def test_any_tool_event_is_refused(self):
        for item_type in ('command_execution','file_change','mcp_tool_call','web_search','tool_call','error'):
            with self.subTest(item_type=item_type),self.assertRaises(RoomError):self.invoke(self.output({'type':'item.completed','item':{'type':item_type}}))
    def test_invalid_exit_multiple_messages_and_missing_completion_refused(self):
        for output in ({'returncode':1,'stdout':b'','stderr':b'private path must never enter receipt'}, {'returncode':0,'stdout':b'{"type":"thread.started","thread_id":"x"}\n','stderr':b''},self.output({'type':'item.completed','item':{'type':'agent_message','text':'{}'}})):
            with self.subTest(output=output),self.assertRaises(RoomError):self.invoke(output)
    def test_role_and_output_path_are_not_request_controlled(self):
        with self.assertRaises(RoomError):self.transport().invoke('shell','x',None,self.root/'task','id')
        task=self.root/'occupied';task.mkdir();(task/'models.json').write_text('preserve')
        with self.assertRaises((RoomError,FileExistsError)):self.transport().invoke('worker','x',None,task,'id')
        self.assertEqual((task/'models.json').read_text(),'preserve')

    def test_nonempty_home_instructions_fail_before_process(self):
        (self.home/'AGENTS.md').write_text('Unrelated instructions must not be sent')
        with mock.patch('common_room.codex_transport.bounded_process') as process,self.assertRaises(RoomError):
            self.transport().invoke('worker','x',None,self.root/'task','id')
        self.assertEqual(process.call_count,0)
    def test_stderr_tool_denial_cannot_be_hidden_by_final_success(self):
        output=self.output();output['stderr']=b'ERROR unsupported call apply_patch'
        with self.assertRaises(RoomError):self.invoke(output)

    def test_changed_client_is_refused_before_launch(self):
        with mock.patch('common_room.codex_transport.PINNED_CLI_SHA256','0'*64),mock.patch('common_room.codex_transport.bounded_process') as process,self.assertRaises(RoomError):
            self.transport().invoke('worker','x',None,self.root/'task','id')
        self.assertEqual(process.call_count,0)
