"""Forward local MCP to the SSH-hosted connector; upload explicit local references."""
import json
import os
import subprocess
import sys
import threading
import pathlib
import base64
import uuid
import tempfile
from server import dispatch, reference_data_url, request_paths, saved_images, output_root, client_root, validate_png, atomic_json

def prepare(message):
    if message.get('method') == 'tools/call':
        params = message.get('params') or {}
        if params.get('name') in ('generate_image', 'edit_reference_image'):
            args = params.get('arguments') or {}
            if args.get('reference_ticket') is not None:
                ticket = args['reference_ticket']
                if isinstance(ticket, str) and ticket.count(':') == 1:
                    sid, rid = ticket.split(':')
                    try:
                        folder, rid = request_paths({'session_id':sid,'request_id':rid})
                    except ValueError:
                        folder = None
                    if folder is not None and (folder / (rid + '.png')).is_file():
                        if args.get('reference_image') is not None:
                            raise ValueError('Choose one reference.')
                        args['reference_image'] = reference_data_url(str(folder / (rid + '.png')))
                        del args['reference_ticket']
            elif args.get('reference_image') is not None:
                reference = args['reference_image']
                if isinstance(reference, str) and not reference.startswith('data:'):
                    path = pathlib.Path(reference).expanduser().resolve()
                    if any(root in path.parents for root in (output_root()/'clients', output_root()/'sessions')):
                        raise ValueError('Use the exact reference_ticket for a saved generated image in this chat.')
                args['reference_image'] = reference_data_url(reference)
    return message

def local_result(message):
    params = message.get('params') or {}
    if message.get('method') != 'tools/call':
        return None
    args = params.get('arguments') or {}
    if params.get('name') == 'fetch_image':
        ticket = args.get('ticket')
        if set(args) != {'ticket'} or not isinstance(ticket, str) or ticket.count(':') != 1:
            return None
        sid, rid = ticket.split(':')
        keys = {'session_id':sid,'request_id':rid}
    elif params.get('name') == 'recover_saved_image':
        aliases = {'sessionId':'session_id','requestId':'request_id','Session id':'session_id','Request id':'request_id'}
        keys = {aliases.get(k,k):v for k,v in args.items()}
        if set(keys) != {'session_id','request_id'} or len(keys) != len(args):
            return None
    else:
        return None
    try:
        folder, rid = request_paths(keys)
    except ValueError:
        return None
    if ((folder/(rid+'.json')).is_file() and (folder/(rid+'.png')).is_file()) or (folder/(rid+'.state.json')).is_file():
        return saved_images(keys)
    return None


def cache_result(line):
    message = json.loads(line)
    result = message.get('result') or {}
    content = result.get('content', []) if isinstance(result, dict) else []
    images = [block for block in content if block.get('type') == 'image']
    texts = [block for block in content if block.get('type') == 'text']
    if not images or not texts:
        return line
    info = json.loads(texts[0]['text'])
    sid, rid = info['session_id'], info['request_id']
    if any(not isinstance(value, str) or str(uuid.UUID(value)) != value for value in (sid,rid)):
        raise ValueError('Remote image identifiers are invalid.')
    folder = client_root() / 'sessions' / sid
    if folder.resolve() != folder:
        raise ValueError('Invalid local image cache path.')
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = folder / (rid + '.png')
    meta = folder / (rid + '.json')
    if any(p.is_symlink() for p in (path, meta, folder/'.session')):
        raise ValueError('Invalid local cache file.')
    data = base64.b64decode(images[0]['data'], validate=True)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Remote image is too large to cache.')
    validate_png(data)
    if path.exists() and path.read_bytes() != data:
        raise ValueError('Local image ticket collision.')
    if not path.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
                temporary = pathlib.Path(output.name)
                output.write(data)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError('Local image ticket collision.')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    (folder/'.session').write_text(sid)
    info['remote_path'] = info['path']
    info['path'] = str(path)
    atomic_json(meta, info)
    texts[0]['text'] = json.dumps(info)
    return json.dumps(message) + '\n'


def remote_call(message, send):
    client = os.environ.get('OMNIROUTE_IMAGE_CLIENT')
    if client not in ('claude-desktop', 'claude-code', 'opencode'):
        raise ValueError('An explicit client name is required.')
    command = 'set -eu; . "$HOME/services/image-mcp/image.env"; export OMNIROUTE_IMAGE_CLIENT=' + client + '; exec python3 "$HOME/services/image-mcp/server.py"'
    process = subprocess.Popen(
        ['/usr/bin/ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',
         '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
         os.environ.get('OMNIROUTE_IMAGE_SSH_HOST','worker-host'), command],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    ticket = None
    expired = threading.Event()
    def expire():
        expired.set()
        try:
            process.kill()
        except ProcessLookupError:
            pass
    deadline = threading.Timer(50, expire)
    deadline.daemon = True
    deadline.start()

    def exchange(request):
        process.stdin.write(json.dumps(request)+'\n')
        process.stdin.flush()
        for line in process.stdout:
            response = json.loads(line)
            if response.get('id') == request['id']:
                return response
            if 'id' not in response:
                send(response)
        raise RuntimeError('SSH connection ended before the result arrived.')

    try:
        message = json.loads(json.dumps(message))
        params = message.get('params') or {}
        if params.get('name') in ('generate_image', 'edit_reference_image'):
            args = params.get('arguments') or {}
            if 'session_id' not in args and 'request_id' not in args:
                if not args.get('reference_ticket'):
                    setup = exchange({'jsonrpc':'2.0','id':'setup-'+str(uuid.uuid4()),
                        'method':'tools/call','params':{'name':'create_image_session','arguments':{}}})
                    result = setup.get('result') or {}
                    if 'error' in setup or result.get('isError'):
                        raise RuntimeError('Could not prepare a remote image session. No image was submitted.')
                    identifiers = json.loads(result['content'][0]['text'])
                    args['session_id'] = identifiers['session_id']
                    args['request_id'] = identifiers['request_id']
                elif isinstance(args['reference_ticket'], str) and args['reference_ticket'].count(':') == 1:
                    args['session_id'] = args['reference_ticket'].split(':')[0]
                    args['request_id'] = str(uuid.uuid4())
                params['arguments'] = args
            if args.get('session_id') and 'request_id' not in args:
                args['request_id'] = str(uuid.uuid4())
                params['arguments'] = args
            if args.get('session_id') and args.get('request_id'):
                ticket = args['session_id'] + ':' + args['request_id']
        return exchange(message)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        detail = ('Hosted image connection failed. No automatic retry was made. '
                  'If an image was submitted it may still be running or billed. ')
        if expired.is_set():
            detail = 'Connection reached its 50-second limit. ' + detail
        if ticket:
            detail += 'Recover only this exact ticket with fetch_image: ' + ticket
        else:
            detail += 'Check the server connection before your next request.'
        raise RuntimeError(detail) from exc
    finally:
        deadline.cancel()
        try:
            process.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        process.stdout.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def handle(message, send):
    try:
        if message.get('method') != 'tools/call':
            result = dispatch(message)
            if message.get('method') == 'initialize':
                result['serverInfo']['version'] += '-ssh-on-demand'
            send({'jsonrpc':'2.0','id':message['id'],'result':result})
            return
        local = local_result(message)
        if local is not None:
            send({'jsonrpc':'2.0','id':message['id'],'result':local})
            return
        response = remote_call(prepare(message), send)
        try:
            response = json.loads(cache_result(json.dumps(response)))
        except (ValueError, KeyError, OSError, TypeError):
            print('Image returned; local cache unavailable. Use its ticket to fetch again.', file=sys.stderr)
        send(response)
    except Exception as exc:
        send({'jsonrpc':'2.0','id':message.get('id'),'result':{'isError':True,
            'content':[{'type':'text','text':str(exc)}]}})


def main():
    lock = threading.Lock()
    def send(message):
        with lock:
            sys.stdout.write(json.dumps(message)+'\n')
            sys.stdout.flush()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError('Expected a JSON-RPC object.')
        except (ValueError, TypeError):
            send({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON-RPC message'}})
            continue
        if 'id' not in message:
            continue
        if message.get('method') == 'tools/call':
            threading.Thread(target=handle, args=(message,send), daemon=True).start()
        else:
            handle(message,send)


if __name__ == '__main__':
    main()
