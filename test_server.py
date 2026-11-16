import base64
import importlib.util
import io
import http.client
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('server', HERE / 'server.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
PNG = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0\xff')) + chunk(b'IEND', b'')
PAYLOAD = {'data': [{'b64_json': base64.b64encode(PNG).decode()}], 'usage': {'total_tokens': 42}}

class Response(io.BytesIO):
    headers = {'x-request-id': 'provider-test-id'}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'OMNIROUTE_IMAGE_OUTPUT_DIR': self.tmp.name, 'OMNIROUTE_IMAGE_CLIENT': 'claude-desktop', 'OMNIROUTE_IMAGE_API_KEY': 'fake-test-key', 'OMNIROUTE_IMAGE_BASE_URL': 'http://127.0.0.1:1/v1', 'OMNIROUTE_IMAGE_MODEL': 'codex/test-image', 'OMNIROUTE_IMAGE_TIMEOUT_SECONDS': '5'})
        self.env.start()
        self.addCleanup(self.env.stop)
        sid = json.loads(s.create_session({})['content'][0]['text'])['session_id']
        self.args = dict(session_id=sid, request_id=str(uuid.uuid4()), prompt='test')
        self.folder, self.rid = s.request_paths(self.args)
        self.job = self.folder / (self.rid + '.job.json')

    def run_worker(self, response=None, error=None):
        s.atomic_json(self.job, self.args)
        s.write_state(self.folder / (self.rid + '.state.json'), {'status': 'queued', 'submitted': False})
        with patch.object(s.urllib.request, 'urlopen', side_effect=error, return_value=Response(json.dumps(PAYLOAD if response is None else response).encode())):
            s.worker(self.job)
        return json.loads((self.folder / (self.rid + '.state.json')).read_text())

    def test_new_image_needs_only_prompt_and_allocates_private_ids(self):
        params={'name':'generate_image','arguments':{'prompt':'style sheet'}}
        allocated=[]
        def capture(args,lock,guard=None):
            folder,rid=s.request_paths(args)
            allocated.append((folder,rid))
            return {'content':[]}
        with patch.object(s,'ROOT',Path(self.tmp.name)), patch.object(s,'run_detached',side_effect=capture):
            for _ in range(2):
                r=s.dispatch({'method':'tools/call','params':params})
                self.assertFalse(r.get('isError'),r)
        self.assertEqual(len(allocated),2)
        self.assertNotEqual(allocated[0][0],allocated[1][0])
        self.assertNotEqual(allocated[0][1],allocated[1][1])
        schema=next(t['inputSchema'] for t in s.dispatch({'method':'tools/list'})['tools'] if t['name']=='generate_image')
        self.assertEqual(schema['required'],['prompt'])

    def test_bad_explicit_ids_never_fall_back_to_another_session(self):
        with patch.object(s,'ROOT',Path(self.tmp.name)), patch.object(s.subprocess,'Popen',side_effect=AssertionError('must not submit')):
            for args in ({'session_id':'bad'}, {'request_id':self.rid}, {'session_id':None}):
                r=s.dispatch({'method':'tools/call','params':{'name':'generate_image','arguments':dict(args,prompt='test')}})
                self.assertTrue(r.get('isError'),r)

    def test_ticket_recovers_exact_saved_png_and_aliases_do_not_cross_clients(self):
        self.run_worker()
        ticket=self.args['session_id']+':'+self.rid
        def call(name,args):
            return s.dispatch({'method':'tools/call','params':{'name':name,'arguments':args}})
        for result in [call('fetch_image',{'ticket':ticket}),call('recover_saved_image',{'sessionId':self.args['session_id'],'requestId':self.rid})]:
            self.assertFalse(result.get('isError'),result)
            self.assertEqual(base64.b64decode(result['content'][1]['data']),PNG)
        with patch.dict(os.environ,{'OMNIROUTE_IMAGE_CLIENT':'opencode'}):
            self.assertTrue(call('fetch_image',{'ticket':ticket})['isError'])
        self.assertTrue(call('fetch_image',{'ticket':'../'+self.rid})['isError'])

    def test_desktop_generation_schema_does_not_invite_partial_ids(self):
        tool=next(t for t in s.dispatch({'method':'tools/list'})['tools'] if t['name']=='generate_image')
        self.assertFalse(set(tool['inputSchema']['properties']) & {'session_id','request_id','reference_image'})

    def test_edit_allocates_ids_and_preserves_explicit_reference(self):
        ref=Path(self.tmp.name)/'reference.png'
        ref.write_bytes(PNG)
        with patch.object(s,'ROOT',Path(self.tmp.name)), patch.object(s,'run_detached') as run:
            run.return_value={'content':[]}
            result=s.dispatch({'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'prompt':'edit','reference_image':str(ref)}}})
            self.assertFalse(result.get('isError'),result)
            args=run.call_args[0][0]
            s.request_paths(args)
            self.assertEqual(args['reference_image'],str(ref))

    def test_edit_ticket_selects_exact_image_and_rejects_other_client(self):
        self.run_worker()
        ticket=json.loads((self.folder/(self.rid+'.json')).read_text())['ticket']
        def call():
            return s.dispatch({'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'prompt':'edit','reference_ticket':ticket}}})
        with patch.object(s,'ROOT',Path(self.tmp.name)), patch.object(s,'run_detached') as run:
            run.return_value={'content':[]}
            self.assertFalse(call().get('isError'))
            args=run.call_args[0][0]
            self.assertEqual(Path(args['reference_image']),self.folder/(self.rid+'.png'))
            with patch.dict(os.environ,{'OMNIROUTE_IMAGE_CLIENT':'opencode'}):
                self.assertTrue(call()['isError'])

    def test_partial_edit_ids_do_not_submit(self):
        with patch.object(s,'ROOT',Path(self.tmp.name)), patch.object(s,'run_detached') as run:
            result=s.dispatch({'method':'tools/call','params':{'name':'edit_reference_image','arguments':{'prompt':'edit','reference_image':'/tmp/example.png','request_id':self.rid}}})
            self.assertTrue(result.get('isError'))
            self.assertFalse(run.called)

    def test_fifo_queue_uses_two_slots_and_third_waits(self):
        import fcntl
        root=Path(self.tmp.name)
        (root/'queue').mkdir()
        markers=[]; holders=[]; slots=[]
        with patch.object(s,'ROOT',root):
            try:
                for i in range(3):
                    active=(root/('active'+str(i))).open('a')
                    fcntl.flock(active,fcntl.LOCK_EX);holders.append(active)
                    marker=root/'queue'/('%02d.json'%i)
                    s.atomic_json(marker,{'active':str(root/('active'+str(i)))})
                    markers.append(marker)
                # Later arrivals cannot pass the oldest waiting entry.
                completed=threading.Event()
                waiter=threading.Thread(target=lambda:(slots.append(s.queue_slot(markers[2])),completed.set()),daemon=True)
                waiter.start()
                first=s.queue_slot(markers[0]);second=s.queue_slot(markers[1])
                self.assertFalse(completed.wait(.3))
                first.close()
                self.assertTrue(completed.wait(2))
                second.close()
                waiter.join(2)
                self.assertFalse(list((root/'queue').glob('*.json')))
            finally:
                for f in slots+holders:f.close()

    def test_enqueue_cleans_orphans_and_preserves_reference_snapshot(self):
        root=Path(self.tmp.name)
        queue=root/'queue';queue.mkdir()
        for i in range(32):
            s.atomic_json(queue/('%02d.json'%i),{'active':str(root/('missing'+str(i)))})
        reference=root/'reference.png';reference.write_bytes(PNG)
        args=dict(self.args,reference_image=str(reference))
        def spawn(command,**kwargs):
            # File changing after enqueue must not alter the snapshotted reference.
            reference.write_bytes(b'changed')
            s.worker(Path(command[3]),Path(command[4]))
            result=type('Process',(),{'wait':lambda self,timeout=None:0})()
            return result
        with patch.object(s,'ROOT',root), patch.object(s.subprocess,'Popen',side_effect=spawn), patch.object(s.urllib.request,'urlopen',return_value=Response(json.dumps(PAYLOAD).encode())), (root/'guard').open('a') as guard:
            result=s.run_detached(args,None,guard)
        info=json.loads(result['content'][0]['text'])
        self.assertEqual(info['reference_path'],str(reference))
        self.assertEqual(info['reference_sha256'],s.hashlib.sha256(PNG).hexdigest())
        self.assertFalse(list(queue.glob('*.json')))

    def test_invalid_reference_never_creates_queued_state(self):
        with patch.object(s,'ROOT',Path(self.tmp.name)), patch.object(s.subprocess,'Popen') as spawn:
            with self.assertRaises(ValueError):
                s.run_detached(dict(self.args,reference_image='/no-such-file.png'),None)
            self.assertFalse(spawn.called)
            self.assertFalse((self.folder/(self.rid+'.state.json')).exists())

    def test_success_metadata_and_exact_recovery(self):
        self.assertEqual(self.run_worker()['status'], 'completed')
        result = s.saved_images({k:self.args[k] for k in ('session_id','request_id')})
        info = json.loads(result['content'][0]['text'])
        self.assertEqual(info['usage'], {'total_tokens':42})
        self.assertEqual(info['provider_request_id'], 'provider-test-id')
        self.assertEqual(info['quality'], 'medium')
        self.assertEqual(base64.b64decode(result['content'][1]['data']), PNG)
        self.assertFalse(self.job.exists())

    def test_timeout_is_unknown_and_recoverable_as_status(self):
        state = self.run_worker(error=TimeoutError())
        self.assertEqual(state['status'], 'outcome_unknown')
        self.assertTrue(state['submitted'])
        result = s.saved_images({k:self.args[k] for k in ('session_id','request_id')})
        self.assertEqual(json.loads(result['content'][0]['text'])['status'], 'outcome_unknown')

    def test_rejection_is_failed_not_running(self):
        error = urllib.error.HTTPError('http://test', 400, 'bad', {}, io.BytesIO(b'{"error":{"message":"bad request"}}'))
        self.assertEqual(self.run_worker(error=error)['status'], 'failed')

    def test_invalid_image_not_saved(self):
        state = self.run_worker(response={'data':[{'b64_json':base64.b64encode(b'not image').decode()}]})
        self.assertEqual(state['status'], 'failed')
        self.assertFalse((self.folder / (self.rid + '.png')).exists())

    def test_truncated_and_corrupt_png_rejected(self):
        for data in (PNG[:-5], PNG[:-1]+b'x', PNG+b'extra'):
            with self.assertRaises(ValueError): s.validate_png(data)

    def test_client_and_reference_isolation(self):
        with patch.dict(os.environ, {'OMNIROUTE_IMAGE_CLIENT':'opencode'}):
            with self.assertRaises(ValueError): s.request_paths(self.args)
        other = json.loads(s.create_session({})['content'][0]['text'])['session_id']
        reference = self.folder / 'original.png'
        reference.write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, 'another session'):
            s.generate(dict(self.args, session_id=other, reference_image=str(reference)))

    def test_unregistered_session_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown session'):
            s.request_paths(dict(self.args, session_id=str(uuid.uuid4())))

    def test_stopped_worker_reports_unknown(self):
        s.write_state(self.folder / (self.rid + '.state.json'), {'status':'running','submitted':True})
        result=s.request_status(self.folder,self.rid)
        self.assertEqual(json.loads(result['content'][0]['text'])['status'],'outcome_unknown')

    def test_changed_duplicate_inputs_rejected(self):
        s.write_state(self.folder / (self.rid + '.state.json'), {'status':'failed','fingerprint':s.fingerprint(self.args)})
        with self.assertRaisesRegex(ValueError, 'different inputs'):
            s.run_detached(dict(self.args,prompt='changed'),None)

    def test_same_session_reference_provenance(self):
        ref = self.folder / 'reference.png'
        ref.write_bytes(PNG)
        self.args['reference_image'] = str(ref)
        self.assertEqual(self.run_worker()['status'], 'completed')
        info = json.loads((self.folder / (self.rid + '.json')).read_text())
        self.assertEqual(info['reference_path'], str(ref))
        self.assertEqual(info['reference_sha256'], s.hashlib.sha256(PNG).hexdigest())

    def test_oversized_response_rejected_before_decoding(self):
        class Oversized:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'x' * limit
        s.atomic_json(self.job, self.args)
        with patch.object(s.urllib.request, 'urlopen', return_value=Oversized()):
            s.worker(self.job)
        state = json.loads((self.folder / (self.rid + '.state.json')).read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertIn('48 MiB', state['error'])

    def test_duplicate_completed_request_returns_without_network(self):
        self.run_worker()
        s.write_state(self.folder / (self.rid + '.state.json'), {'fingerprint': s.fingerprint(self.args)})
        with patch.object(s.subprocess, 'Popen', side_effect=AssertionError('must not generate twice')):
            result = s.run_detached(self.args, None)
        self.assertEqual(result['content'][1]['type'], 'image')

    def test_png_header_and_scanline_validation(self):
        for depth, pixels in ((9, b'\0\xff\0\0\xff'), (8, b'\0'), (8, b'\x05\xff\0\0\xff')):
            image = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB',1,1,depth,6,0,0,0)) + chunk(b'IDAT',zlib.compress(pixels)) + chunk(b'IEND',b'')
            with self.subTest(depth=depth,pixels=pixels):
                with self.assertRaises(ValueError): s.validate_png(image)

    def test_png_chunk_order_validation(self):
        header=chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))
        pixels=chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\xff'))
        indexed=chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,3,0,0,0))
        for chunks in (header+header+pixels, header+pixels+chunk(b'tEXt',b'a')+pixels, indexed+chunk(b'IDAT',zlib.compress(b'\0\0'))):
            with self.subTest(chunks=chunks):
                with self.assertRaises(ValueError):
                    s.validate_png(b'\x89PNG\r\n\x1a\n'+chunks+chunk(b'IEND',b''))

    def test_incomplete_response_is_unknown(self):
        class Interrupted(Response):
            def read(self, limit): raise http.client.IncompleteRead(b'partial', 100)
        s.atomic_json(self.job,self.args)
        with patch.object(s.urllib.request,'urlopen',return_value=Interrupted()):
            s.worker(self.job)
        state=json.loads((self.folder/(self.rid+'.state.json')).read_text())
        self.assertEqual(state['status'],'outcome_unknown')

    def test_usage_preserved_when_image_is_invalid(self):
        state=self.run_worker(response={'data':[{'b64_json':base64.b64encode(b'bad').decode()}],'usage':{'total_tokens':42}})
        self.assertEqual(state.get('usage'),{'total_tokens':42})
        self.assertEqual(state.get('provider_request_id'),'provider-test-id')

    def test_interrupted_error_body_keeps_http_outcome(self):
        class Interrupted(io.BytesIO):
            def read(self, limit): raise http.client.IncompleteRead(b'partial', 100)
        error=urllib.error.HTTPError('http://test',503,'unavailable',{'x-request-id':'failure-id'},Interrupted())
        state=self.run_worker(error=error)
        self.assertEqual(state['status'],'outcome_unknown')
        self.assertEqual(state.get('provider_request_id'),'failure-id')
        self.assertIn('503',state['error'])

    def test_claude_returns_pending_before_client_timeout(self):
        import subprocess
        for client in ('claude-desktop','claude-code'):
            with self.subTest(client=client), patch.dict(os.environ, {'OMNIROUTE_IMAGE_CLIENT':client}):
                sid=json.loads(s.create_session({})['content'][0]['text'])['session_id']
                args=dict(self.args,session_id=sid)
                with patch.object(s.subprocess,'Popen') as spawn, (Path(self.tmp.name)/'lock').open('a') as lock:
                    def wait(timeout=None):
                        if timeout is None and threading.current_thread() is not threading.main_thread(): return 0
                        self.assertIsNotNone(timeout, 'generation would wait past the client timeout')
                        self.assertLess(timeout,60)
                        raise subprocess.TimeoutExpired('worker',timeout)
                    spawn.return_value.wait.side_effect=wait
                    result=s.run_detached(args,lock)
                state=json.loads(result['content'][0]['text'])
                self.assertFalse(result.get('isError'))
                self.assertEqual(state['session_id'],sid)
                self.assertEqual(state['request_id'],args['request_id'])
                self.assertIn('fetch_image',state['next_step'])
                self.assertEqual(state['ticket'],sid+':'+args['request_id'])

    def test_recovery_waits_for_original_image(self):
        with (self.folder/(self.rid+'.active')).open('a') as active:
            s.fcntl.flock(active,s.fcntl.LOCK_EX)
            s.write_state(self.folder/(self.rid+'.state.json'),{'status':'running'})
            keys={key:self.args[key] for key in ('session_id','request_id')}
            with patch.object(s.time,'sleep',side_effect=lambda _: self.run_worker()):
                result=s.recover(keys)
        self.assertEqual(base64.b64decode(result['content'][1]['data']),PNG)

    def test_detached_worker_survives_parent_and_keeps_global_lock(self):
        received, release = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                received.set()
                release.wait(10)
                body=json.dumps(PAYLOAD).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*args): pass
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.addCleanup(release.set)
        os.environ['OMNIROUTE_IMAGE_BASE_URL']='http://127.0.0.1:%d/v1'%http.server_port
        proc=subprocess.Popen([sys.executable,str(HERE/'server.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'generate_image','arguments':self.args}})+'\n')
        proc.stdin.flush()
        self.assertTrue(received.wait(5))
        proc.stdin.close()
        proc.wait(timeout=5)
        with (s.ROOT/'.generation.lock').open('a') as maintenance:
            with self.assertRaises(BlockingIOError):
                s.fcntl.flock(maintenance,s.fcntl.LOCK_EX|s.fcntl.LOCK_NB)
        release.set()
        deadline=time.monotonic()+5
        state_path=self.folder/(self.rid+'.state.json')
        while time.monotonic()<deadline:
            state=json.loads(state_path.read_text())
            if state['status']=='completed': break
            time.sleep(.05)
        self.assertEqual(state['status'],'completed')
        self.assertTrue((self.folder/(self.rid+'.png')).exists())
        proc.stdout.close(); proc.stderr.close()

if __name__ == '__main__': unittest.main(verbosity=2)
