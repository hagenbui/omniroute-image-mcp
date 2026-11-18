import importlib.util
import pathlib
import sys
import tempfile
import unittest
import os,json,uuid,base64,struct,zlib,io
from unittest.mock import patch, Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
spec=importlib.util.spec_from_file_location('bridge',pathlib.Path(__file__).with_name('bridge.py'))
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
class Test(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'OMNIROUTE_IMAGE_OUTPUT_DIR':self.temp.name,'OMNIROUTE_IMAGE_CLIENT':'claude-desktop'})
        env.start();self.addCleanup(env.stop)
    def test_generated_paths_blocked_but_exact_legacy_ticket_recovers(self):
        sid,rid=str(uuid.uuid4()),str(uuid.uuid4())
        folder=pathlib.Path(self.temp.name)/'clients/claude-desktop/sessions'/sid
        folder.mkdir(parents=True)
        (folder/'.session').write_text(sid)
        image=folder/(rid+'.png');image.write_bytes(b'\x89PNG\r\n\x1a\nreference')
        (folder/(rid+'.json')).write_text(json.dumps({'session_id':sid,'request_id':rid}))
        with self.assertRaises(ValueError):
            bridge.prepare({'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'reference_image':str(image)}}})
        message={'id':1,'method':'tools/call','params':{'name':'fetch_image','arguments':{'ticket':sid+':'+rid}}}
        self.assertEqual(bridge.local_result(message)['content'][1]['type'],'image')
        edit=bridge.prepare({'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'reference_ticket':sid+':'+rid}}})
        self.assertTrue(edit['params']['arguments']['reference_image'].startswith('data:image/png'))
        with patch.dict(os.environ,{'OMNIROUTE_IMAGE_CLIENT':'opencode'}):
            self.assertIsNone(bridge.local_result(message))

    def test_remote_result_is_cached_with_local_path_and_original_ticket(self):
        def chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\xff'))+chunk(b'IEND',b'')
        sid,rid=str(uuid.uuid4()),str(uuid.uuid4())
        line=json.dumps({'id':1,'result':{'content':[{'type':'text','text':json.dumps({'session_id':sid,'request_id':rid,'path':'/home/user/remote.png','ticket':sid+':'+rid})},{'type':'image','mimeType':'image/png','data':base64.b64encode(png).decode()}]}})
        with patch.object(bridge.os,'link',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                bridge.cache_result(line)
        self.assertFalse(list(pathlib.Path(self.temp.name).rglob('*.png')))
        cached=json.loads(bridge.cache_result(line))['result']
        info=json.loads(cached['content'][0]['text'])
        self.assertEqual(pathlib.Path(info['path']).read_bytes(),png)
        self.assertEqual(info['remote_path'],'/home/user/remote.png')
        recovery=bridge.local_result({'method':'tools/call','params':{'name':'recover_saved_image','arguments':{'sessionId':sid,'requestId':rid}}})
        self.assertEqual(base64.b64decode(recovery['content'][1]['data']),png)

    def test_connection_failure_does_not_replay_and_next_call_can_succeed(self):
        message={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'create_image_session','arguments':{}}}
        send=Mock()
        with patch.object(bridge,'remote_call',side_effect=[RuntimeError('SSH disconnected'),{'jsonrpc':'2.0','id':8,'result':{'content':[]}}]) as remote:
            bridge.handle(message,send)
            self.assertEqual(remote.call_count,1)
            self.assertTrue(send.call_args[0][0]['result']['isError'])
            bridge.handle(dict(message,id=8),send)
            self.assertEqual(remote.call_count,2)
            self.assertEqual(send.call_args[0][0]['id'],8)
            self.assertFalse(send.call_args[0][0]['result'].get('isError',False))

    def test_discovery_and_ping_do_not_open_idle_ssh_connection(self):
        with patch.object(bridge,'remote_call') as remote:
            send=Mock()
            for i,method in enumerate(('initialize','tools/list','ping')):
                bridge.handle({'jsonrpc':'2.0','id':i,'method':method,'params':{}},send)
                self.assertIn('result',send.call_args[0][0])
            self.assertFalse(remote.called)

    def test_lost_generation_connection_keeps_exact_ticket_without_resubmission(self):
        sid,rid=str(uuid.uuid4()),str(uuid.uuid4())
        sent=[];responses=[]
        class Input:
            def write(self,line):
                request=json.loads(line);sent.append(request)
                if request['params']['name']=='create_image_session':
                    responses.append(json.dumps({'id':request['id'],'result':{'content':[{'type':'text','text':json.dumps({'session_id':sid,'request_id':rid})}]}})+'\n')
            def flush(self):pass
            def close(self):pass
        class Output:
            def __iter__(self):return self
            def __next__(self):
                if not responses:raise StopIteration
                return responses.pop(0)
            def close(self):pass
        process=Mock(stdin=Input(),stdout=Output())
        with patch.object(bridge.subprocess,'Popen',return_value=process) as spawn:
            with self.assertRaisesRegex(RuntimeError,sid+':'+rid):
                bridge.remote_call({'id':9,'method':'tools/call','params':{'name':'generate_image','arguments':{'prompt':'test'}}},Mock())
            self.assertEqual(spawn.call_count,1)
        self.assertEqual([r['params']['name'] for r in sent],['create_image_session','generate_image'])
        self.assertEqual(sent[-1]['params']['arguments']['request_id'],rid)

    def test_existing_session_gets_known_request_id_before_submission(self):
        sid=str(uuid.uuid4())
        process=Mock(stdin=Mock())
        # A concrete empty response stream models SSH loss immediately after write.
        process.stdout=io.StringIO('')
        with patch.object(bridge.subprocess,'Popen',return_value=process):
            with self.assertRaises(RuntimeError) as exc:
                bridge.remote_call({'id':8,'method':'tools/call','params':{'name':'generate_image','arguments':{'session_id':sid,'prompt':'test'}}},Mock())
        sent=json.loads(process.stdin.write.call_args[0][0])
        rid=sent['params']['arguments']['request_id']
        self.assertEqual(str(uuid.UUID(rid)),rid)
        self.assertIn(sid+':'+rid,str(exc.exception))

    def test_each_ssh_call_has_a_bounded_deadline_and_cancels_it(self):
        process=Mock(stdin=Mock(),stdout=io.StringIO(''))
        timer=Mock()
        with patch.object(bridge.subprocess,'Popen',return_value=process), patch.object(bridge.threading,'Timer',return_value=timer) as factory:
            with self.assertRaises(RuntimeError):
                bridge.remote_call({'id':1,'method':'tools/call','params':{'name':'fetch_image','arguments':{'ticket':'exact:ticket'}}},Mock())
        self.assertEqual(factory.call_count,1)
        self.assertLess(factory.call_args[0][0],60)
        self.assertTrue(timer.start.called)
        self.assertTrue(timer.cancel.called)
        factory.call_args[0][1]()
        self.assertTrue(process.kill.called)

    def test_explicit_file_is_uploaded_and_ticket_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'reference.png';p.write_bytes(b'\x89PNG\r\n\x1a\nreference')
            message={'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'reference_image':str(p),'prompt':'edit'}}}
            result=bridge.prepare(message)
            self.assertTrue(result['params']['arguments']['reference_image'].startswith('data:image/png;base64,'))
            ticket={'method':'tools/call','params':{'name':'fetch_image','arguments':{'ticket':'exact:ticket'}}}
            self.assertEqual(bridge.prepare(ticket),ticket)
    def test_unreadable_reference_does_not_reach_remote(self):
        with self.assertRaises(ValueError):
            bridge.prepare({'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'reference_image':'/no-file.png'}}})
if __name__=='__main__':unittest.main()
