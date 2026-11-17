"""Forward local MCP to the SSH-hosted connector; upload explicit local references."""
import json
import os
import subprocess
import sys
import threading
import pathlib
import select
import base64
import uuid
import tempfile
from server import reference_data_url, request_paths, saved_images, output_root, client_root, validate_png, atomic_json

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


def main():
    client = os.environ.get('OMNIROUTE_IMAGE_CLIENT')
    if client not in ('claude-desktop', 'claude-code', 'opencode'):
        raise SystemExit('An explicit client name is required.')
    command = 'set -eu; . "$HOME/services/image-mcp/image.env"; export OMNIROUTE_IMAGE_CLIENT=' + client + '; exec python3 "$HOME/services/image-mcp/server.py"'
    process = subprocess.Popen(
        ['/usr/bin/ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',
         '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
         os.environ.get('OMNIROUTE_IMAGE_SSH_HOST','worker-host'), command],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    output_lock = threading.Lock()
    def send_line(line):
        with output_lock:
            sys.stdout.write(line)
            sys.stdout.flush()
    def output():
        for line in process.stdout:
            try:
                line = cache_result(line)
            except (ValueError, KeyError, OSError, TypeError):
                # Preserve the returned image even if the optional Mac cache fails.
                print('Image returned; local cache unavailable. Use its ticket to fetch again.', file=sys.stderr)
            send_line(line)
    reader = threading.Thread(target=output, daemon=True)
    reader.start()
    def incoming():
        pending = bytearray()
        while process.poll() is None:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not ready:
                continue
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                if pending:
                    yield pending.decode()
                break
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                pending = bytearray(rest)
                yield line.decode()
    try:
        for line in incoming():
            message = {}
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError('Expected a JSON-RPC object.')
                local = local_result(message)
                if local is not None:
                    send_line(json.dumps({'jsonrpc':'2.0','id':message.get('id'),'result':local})+'\n')
                    continue
                prepared = prepare(message)
            except (ValueError, OSError) as exc:
                message = message if isinstance(message, dict) else {}
                send_line(json.dumps({'jsonrpc':'2.0','id':message.get('id'),
                  'result':{'isError':True,'content':[{'type':'text','text':str(exc)}]}})+'\n')
                continue
            process.stdin.write(json.dumps(prepared)+'\n')
            process.stdin.flush()
    finally:
        process.stdin.close()
        reader.join()
        process.wait()
    raise SystemExit(process.returncode)

if __name__ == '__main__':
    main()
