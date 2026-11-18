"""Real SSH MCP round trip with a synthetic provider. No OpenAI call or user image."""
import json, os, pathlib, shlex, subprocess, sys, tempfile, time
from unittest.mock import patch
HERE=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import bridge
PROVIDER = """
import http.server, threading, pathlib, json, os, base64, zlib, struct
root=pathlib.Path(__file__).parent
def chunk(kind,data):
 return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
png=b'\\x89PNG\\r\\n\\x1a\\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',768,640,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(b'\\0'+os.urandom(768*4) for _ in range(640))))+chunk(b'IEND',b'')
class Handler(http.server.BaseHTTPRequestHandler):
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']))
  threading.Event().wait(3)
  body=json.dumps({'data':[{'b64_json':base64.b64encode(png).decode()}]}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  threading.Timer(2,server.shutdown).start()
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
(root/'port').write_text(str(server.server_port))
timeout=threading.Timer(120,server.shutdown);timeout.daemon=True;timeout.start()
server.serve_forever();server.server_close()
"""
setup = """
import pathlib,json,sys,tempfile,subprocess,time,os,shlex
os.umask(0o077)
data=json.load(sys.stdin);root=pathlib.Path(tempfile.mkdtemp(prefix='image-mcp-e2e-'))
for name,content in data.items():(root/name).write_text(content)
subprocess.Popen(['python3',str(root/'provider.py')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
for i in range(100):
 if (root/'port').exists():break
 time.sleep(.1)
port=int((root/'port').read_text())
env={'OMNIROUTE_IMAGE_API_KEY':'fake-diagnostic-only','OMNIROUTE_IMAGE_BASE_URL':'http://127.0.0.1:'+str(port)+'/v1','OMNIROUTE_IMAGE_MODEL':'codex/test','OMNIROUTE_IMAGE_RESPONSE_WAIT_SECONDS':'0','OMNIROUTE_IMAGE_OUTPUT_DIR':str(root/'images'),'OMNIROUTE_IMAGE_TIMEOUT_SECONDS':'20'}
(root/'image.env').write_text(''.join('export '+k+'='+shlex.quote(v)+'\\n' for k,v in env.items()))
print(root)
"""
payload={'server.py':(HERE/'server.py').read_text(),'provider.py':PROVIDER}
remote_root=subprocess.check_output(['ssh','-T','-o','BatchMode=yes','worker-host','python3 -c '+shlex.quote(setup)],input=json.dumps(payload),text=True).strip()
assert remote_root.startswith('/tmp/image-mcp-e2e-')
original_popen=subprocess.Popen
def routed(args,**kwargs):
 args=list(args)
 args[-1]=args[-1].replace('$HOME/services/image-mcp',remote_root)
 return original_popen(args,**kwargs)
try:
 with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'OMNIROUTE_IMAGE_OUTPUT_DIR':tmp,'OMNIROUTE_IMAGE_CLIENT':'claude-desktop'}),patch.object(bridge.subprocess,'Popen',side_effect=routed):
  def call(i,name,args):
   responses=[]
   bridge.handle({'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':name,'arguments':args}},responses.append)
   result=next(r['result'] for r in responses if r.get('id')==i)
   assert not result.get('isError'), result
   return result
  started=time.monotonic()
  result=call(1,'generate_image',{'prompt':'synthetic transport test','quality':'high'})
  first=json.loads(result['content'][0]['text'])
  assert first['status'] in ('queued','running'),first
  elapsed=time.monotonic()-started
  assert elapsed<15,elapsed
  print('Real SSH submission returns pending ticket:',round(elapsed,2),'seconds',flush=True)
  ticket=first['ticket']
  for i in range(2,8):
   time.sleep(2)
   result=call(i,'fetch_image',{'ticket':ticket})
   if any(c['type']=='image' for c in result['content']):
    info=json.loads(result['content'][0]['text'])
    data=pathlib.Path(info['path']).read_bytes()
    assert len(data)>1_000_000
    assert info['ticket']==ticket
    print('Real SSH worker -> fetch -> inline PNG -> Mac cache:',len(data),'bytes; total',round(time.monotonic()-started,2),'seconds',flush=True)
    break
  else:raise AssertionError('Synthetic image never returned')
finally:
 cleanup="from pathlib import Path;import shutil;p=Path("+repr(remote_root)+");assert p.name.startswith('image-mcp-e2e-');shutil.rmtree(p)"
 subprocess.run(['ssh','-T','-o','BatchMode=yes','worker-host','python3 -c '+shlex.quote(cleanup)],check=True)
