import importlib.util
import pathlib
import sys
import tempfile
import unittest
import os,json,uuid,base64,struct,zlib,io
from unittest.mock import patch
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
