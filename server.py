#!/usr/bin/env python3
"""Small stdio MCP image-generation server."""
import hashlib
import http.client
import io
import base64
import json
import re
import fcntl
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import subprocess
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent
OUTPUT_LOCK = threading.Lock()
MAX_IMAGE_PIXELS = 16777216


def configuration(selection='gpt'):
    url = os.environ.get('OMNIROUTE_IMAGE_BASE_URL', '').rstrip('/')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Set a valid OmniRoute base_url without credentials or query parameters.')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Use HTTPS for a remote OmniRoute endpoint.')
    if selection == 'gemini':
        return url, 'antigravity/gemini-3.1-flash-image'
    if selection != 'gpt':
        raise ValueError('Choose model gpt or gemini.')
    model = os.environ.get('OMNIROUTE_IMAGE_MODEL')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Set OMNIROUTE_IMAGE_MODEL in image.env.')
    return url, model
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'session_id': {'type': 'string', 'description': 'Omit for new text-to-image. Otherwise use the exact session UUID returned by this connector in THIS conversation. Never copy from another chat.'},
        'request_id': {'type': 'string', 'description': 'Omit for ordinary generate_image calls. Explicit IDs must be canonical UUIDs. Retain for exact recovery after timeout. Never reuse for different inputs.'},
        'reference_image': {'type': 'string', 'description': 'Optional reference image: absolute path on this Mac or a base64 data URL (PNG/JPEG/WebP). In OpenCode, use the readable local-copy path supplied by the attachment handoff for this conversation; do not ask the user to save the attachment or provide its path again. For Cowork VM paths, first copy the file to an accessible Mac path. Never invent a path or base64.'},
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 32000},
        'model': {'type': 'string', 'enum': ['gpt', 'gemini'], 'default': 'gpt',
                  'description': 'Default to GPT. Use Gemini when specifically requested. For an explicit comparison, call this tool once per model with identical prompt, settings and reference. Keep each ticket, fetch both, and label each result. After a confirmed GPT failure, ask before submitting Gemini. Never duplicate a comparison job.'},
        'size': {'type': 'string', 'enum': ['1024x1024', '1536x1024', '1024x1536'], 'default': '1024x1024'},
        'quality': {'type': 'string', 'enum': ['low', 'medium', 'high'], 'default': 'medium'},
        'background': {'type': 'string', 'enum': ['auto', 'opaque', 'transparent'], 'default': 'auto', 'description': 'Codex image routes support only auto; OmniRoute does not forward explicit background values for that provider.'},
    }, 'required': ['prompt', 'session_id', 'request_id'],
}


def reference_data_url(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Supply a reference image path or data URL.')
    limit = 15 * 1024 * 1024
    if value.startswith('data:'):
        if len(value) > limit * 4 // 3 + 100:
            raise ValueError('Reference image exceeds 15 MiB.')
        try:
            header, encoded = value.split(',', 1)
            if header not in ('data:image/png;base64', 'data:image/jpeg;base64', 'data:image/webp;base64'):
                raise ValueError()
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            raise ValueError('Use a valid PNG, JPEG, or WebP base64 data URL.') from None
    else:
        path = pathlib.Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError('Reference path must be absolute on this Mac.')
        try:
            with path.open('rb') as file:
                data = file.read(limit + 1)
        except OSError:
            raise ValueError('Cannot read reference on this Mac. Save or copy it to an accessible Mac folder and provide that absolute path; a Cowork /mnt path is not a Mac path.') from None
    if not data or len(data) > limit:
        raise ValueError('Reference image must be nonempty and no larger than 15 MiB.')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ValueError('Reference file must be PNG, JPEG, or WebP.')
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode()


def output_root():
    return pathlib.Path(os.environ['OMNIROUTE_IMAGE_OUTPUT_DIR']).expanduser().resolve()


def client_root():
    client = os.environ.get('OMNIROUTE_IMAGE_CLIENT', '')
    if client not in ('claude-desktop', 'claude-code', 'opencode'):
        raise ValueError('Launch with an explicit OMNIROUTE_IMAGE_CLIENT: claude-desktop, claude-code, or opencode.')
    root = output_root() / 'clients' / client
    if root.resolve() != root:
        raise ValueError('Invalid client storage path.')
    return root


def new_session_id():
    sid = str(uuid.uuid4())
    folder = client_root() / 'sessions' / sid
    folder.mkdir(parents=True, mode=0o700)
    (folder / '.session').write_text(sid)
    return sid


def create_session(args):
    if args:
        raise ValueError('create_image_session takes no arguments.')
    return {'content': [{'type': 'text', 'text': json.dumps({'session_id': new_session_id(), 'request_id': str(uuid.uuid4())})}]}


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def write_state(path, value):
    previous = json.loads(path.read_text()) if path.exists() else {}
    atomic_json(path, dict(previous, **value))


def fingerprint(args):
    return hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()


class OutcomeUnknown(ValueError):
    pass


def validate_png(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Provider returned non-PNG image bytes.')
    offset, chunks, compressed = 8, [], bytearray()
    while offset + 12 <= len(data):
        length = struct.unpack('>I', data[offset:offset+4])[0]
        kind = data[offset+4:offset+8]
        end = offset + 8 + length
        if end + 4 > len(data) or zlib.crc32(data[offset+4:end]) != struct.unpack('>I', data[end:end+4])[0]:
            raise ValueError('Provider returned a corrupt PNG.')
        body = data[offset+8:end]
        if not chunks:
            if kind != b'IHDR' or length != 13:
                raise ValueError('Provider returned an invalid PNG header.')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
            allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in allowed.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise ValueError('Provider returned unsupported PNG header fields.')
            bits_per_pixel = depth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            if not width or not height or width * height > MAX_IMAGE_PIXELS:
                raise ValueError('Provider image dimensions exceed the supported limit.')
        if chunks and kind == b'IHDR':
            raise ValueError('Provider returned duplicate PNG headers.')
        if kind == b'PLTE':
            if b'PLTE' in chunks or b'IDAT' in chunks or color in (0, 4) or not length or length % 3 or length > 768 or (color == 3 and length // 3 > 2 ** depth):
                raise ValueError('Provider returned an invalid PNG palette.')
        if kind == b'IDAT':
            if (b'IDAT' in chunks and chunks[-1] != b'IDAT') or (color == 3 and b'PLTE' not in chunks):
                raise ValueError('Provider returned invalid PNG chunk ordering.')
            compressed.extend(body)
        elif kind not in (b'IHDR', b'PLTE', b'IEND') and not kind[0] & 32:
            raise ValueError('Provider returned an unknown critical PNG chunk.')
        chunks.append(kind)
        offset = end + 4
        if kind == b'IEND':
            if length or offset != len(data) or not compressed:
                raise ValueError('Provider returned an incomplete PNG.')
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(compressed, 128 * 1024 * 1024 + 1)
            if not decoded or len(decoded) > 128 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
                raise ValueError('Provider returned invalid or oversized PNG pixel data.')
            passes = ((0, 0, 1, 1),) if not interlace else ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
            position = 0
            for x, y, dx, dy in passes:
                columns, rows = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
                if not columns or not rows:
                    continue
                stride = 1 + (columns * bits_per_pixel + 7) // 8
                end = position + rows * stride
                if end > len(decoded) or any(decoded[row] > 4 for row in range(position, end, stride)):
                    raise ValueError('Provider returned invalid PNG scanlines.')
                position = end
            if position != len(decoded):
                raise ValueError('Provider returned invalid PNG pixel length.')
            return
    raise ValueError('Provider returned a truncated PNG.')


def check_webp_canvas(data):
    # Pillow's WebP decoder allocates its canvas inside Image.open().
    kind = data[12:16]
    if kind == b'VP8X' and len(data) >= 30:
        if data[20] & 2:
            raise ValueError('Gemini returned an animated WebP.')
        width = 1 + int.from_bytes(data[24:27], 'little')
        height = 1 + int.from_bytes(data[27:30], 'little')
    elif kind == b'VP8L' and len(data) >= 25 and data[20] == 0x2f:
        dimensions = int.from_bytes(data[21:25], 'little')
        width = 1 + (dimensions & 0x3fff)
        height = 1 + ((dimensions >> 14) & 0x3fff)
    elif kind == b'VP8 ' and len(data) >= 30 and data[23:26] == b'\x9d\x01\x2a':
        width, height = struct.unpack_from('<HH', data, 26)
        width &= 0x3fff
        height &= 0x3fff
    else:
        raise ValueError('Gemini returned an invalid WebP canvas header.')
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        raise ValueError('Gemini returned an invalid or oversized WebP canvas.')


def gemini_png(data):
    from PIL import Image
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        check_webp_canvas(data)
    try:
        with Image.open(io.BytesIO(data), formats=('JPEG', 'WEBP')) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS or getattr(source, 'is_animated', False):
                raise ValueError('Gemini returned an oversized or animated image.')
            output = io.BytesIO()
            source.convert('RGBA' if 'A' in source.getbands() else 'RGB').save(output, format='PNG')
            return output.getvalue()
    except (OSError, Image.DecompressionBombError):
        raise ValueError('Gemini returned invalid JPEG or WebP image bytes.') from None


def request_paths(args):
    ids = []
    for field in ('session_id', 'request_id'):
        value = args.get(field)
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('Invalid ' + field + ': expected an exact connector UUID. For a NEW text-only image omit both IDs. For edit or recovery copy the IDs returned in this conversation. No request submitted by this validation.') from None
        ids.append(value)
    folder = client_root() / 'sessions' / ids[0]
    if not (folder / '.session').is_file():
        raise ValueError('Unknown session for this client. Call create_image_session once in this conversation.')
    if folder.resolve() != folder or any((folder / (ids[1] + suffix)).is_symlink() for suffix in ('.png', '.json', '.state.json', '.job.json', '.active', '.reserved')):
        raise ValueError('Invalid image storage path.')
    return folder, ids[1]


def saved_images(args):
    if set(args) != {'session_id', 'request_id'}:
        raise ValueError('Recovery requires exactly session_id and request_id from the original call. Global recent-image lists and filename-only recovery are disabled.')
    folder, rid = request_paths(args)
    path = folder / (rid + '.png')
    meta = folder / (rid + '.json')
    if not path.is_file() or not meta.is_file():
        return request_status(folder, rid)
    info = json.loads(meta.read_text())
    return {'content': [{'type': 'text', 'text': json.dumps(info)},
                        {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(path.read_bytes()).decode()}]}


def generate(args):
    if not isinstance(args, dict) or set(args) - set(SCHEMA['properties']):
        raise ValueError('Invalid image arguments.')
    prompt = args.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise ValueError('Supply a prompt between 1 and 32000 characters.')
    folder, rid = request_paths(args)
    selection = args.get('model', 'gpt')
    base_url, model = configuration(selection)
    if selection == 'gemini':
        try:
            from PIL import features
        except ImportError:
            raise ValueError('Install Pillow on the image worker before using Gemini.') from None
        if not all(features.check(codec) for codec in ('jpg', 'webp', 'zlib')):
            raise ValueError('Gemini needs Pillow with JPEG and WebP decoding and PNG encoding on the image worker.')
    payload = {'model': model, 'prompt': prompt, 'n': 1, 'output_format': 'png', 'response_format': 'b64_json'}
    for field in ('size', 'quality', 'background'):
        spec = SCHEMA['properties'][field]
        value = args.get(field, spec['default'])
        if value not in spec['enum']:
            raise ValueError('Invalid ' + field)
        payload[field] = value
    background_note = ''
    if model.startswith(('codex/', 'cx/')):
        if payload['background'] != 'auto':
            background_note = (' Requested background=' + payload['background'] +
                ' was not applied: this OmniRoute Codex route uses backend defaults. Do not claim transparency or opacity is guaranteed.')
        del payload['background']
    key = os.environ.get('OMNIROUTE_IMAGE_API_KEY', '').strip()
    if not key:
        raise ValueError('Set OMNIROUTE_IMAGE_API_KEY in image.env, then restart the MCP connection.')
    endpoint = '/images/generations'
    if args.get('reference_image') is not None:
        ref = args['reference_image']
        if isinstance(ref, str) and not ref.startswith('data:'):
            ref_path = pathlib.Path(ref).expanduser().resolve()
            generated_roots = (output_root() / 'sessions', output_root() / 'clients')
            if any(root in ref_path.parents for root in generated_roots) and ref_path.parent != folder:
                raise ValueError('This generated reference belongs to another session. Ask the user to explicitly provide a separate reference file instead of selecting another chat output.')
        payload['images'] = [{'image_url': reference_data_url(args['reference_image'])}]
        endpoint = '/images/edits'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with (folder / (rid + '.reserved')).open('x') as marker:
            marker.write('Request reserved; recover using this session_id and request_id.')
    except FileExistsError:
        raise ValueError('This request ID was already submitted. Recover this exact session/request; do not resubmit it.') from None
    request = urllib.request.Request(
        base_url + endpoint,
        data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': 'Claude-Image-MCP/1.0'},
        method='POST',
    )
    started = time.monotonic()
    write_state(folder / (rid + '.state.json'), {'status': 'running', 'submitted': True, 'selection': selection, 'model': model})
    try:
        with urllib.request.urlopen(request, timeout=float(os.environ.get('OMNIROUTE_IMAGE_TIMEOUT_SECONDS', '600'))) as response:
            provider_request_id = response.headers.get('x-request-id')
            write_state(folder / (rid + '.state.json'), {'provider_request_id': provider_request_id})
            raw = response.read(48 * 1024 * 1024 + 1)
            if len(raw) > 48 * 1024 * 1024:
                raise ValueError('Provider response exceeds 48 MiB.')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError('Provider returned an invalid response object.')
            write_state(folder / (rid + '.state.json'), {'usage': result.get('usage'),
                        'duration_seconds': round(time.monotonic() - started, 3),
                        'model': model, 'size': payload['size'], 'quality': payload['quality']})
    except urllib.error.HTTPError as exc:
        write_state(folder / (rid + '.state.json'), {'provider_request_id': (exc.headers or {}).get('x-request-id')})
        try:
            raw = exc.read(16384).decode(errors='replace')
        except (OSError, http.client.HTTPException):
            raw = ''
        try:
            parsed = json.loads(raw)
            error = parsed.get('error', parsed) if isinstance(parsed, dict) else parsed
            detail = error.get('message', error.get('detail', '')) if isinstance(error, dict) else error
            if not isinstance(detail, str):
                detail = ''
        except ValueError:
            detail = ''
        detail = detail.replace(key, '[redacted]')
        detail = re.sub(r'(?i)bearer\s+\S+|\bsk-[A-Za-z0-9_-]+', '[redacted]', detail)
        detail = ' '.join(detail.split())[:800]
        if not detail:
            detail = ('Gateway/provider failure; the server returned no JSON error details.'
                      if exc.code >= 500 else 'Request rejected by OmniRoute.')
        raise (OutcomeUnknown if exc.code >= 500 or exc.code == 408 else ValueError)('OmniRoute HTTP %s: %s Do not automatically retry or reduce quality; check recovered images before resubmitting.' % (exc.code, detail)) from None
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError):
        raise OutcomeUnknown('OmniRoute connection failed or timed out. Check network access before retrying; a timed-out request may still be billed.') from None
    items = result.get('data')
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ValueError('OmniRoute returned no image data.')
    encoded = items[0].get('b64_json')
    if not encoded:
        data_url = items[0].get('url', '')
        prefixes = ('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,') if selection == 'gemini' else ('data:image/png;base64,',)
        if isinstance(data_url, str) and data_url.startswith(prefixes):
            encoded = data_url.split(',', 1)[1]
    if not isinstance(encoded, str) or not encoded:
        raise ValueError('OmniRoute did not return base64 image bytes despite response_format=b64_json.')
    data = base64.b64decode(encoded, validate=True)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Provider image exceeds 32 MiB.')
    if selection == 'gemini' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
        data = gemini_png(data)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError('Converted Gemini image exceeds 32 MiB.')
        encoded = base64.b64encode(data).decode()
    validate_png(data)
    path = folder / (rid + '.png')
    reference = payload.get('images', [{}])[0].get('image_url')
    info = {'session_id': args['session_id'], 'request_id': rid, 'ticket': args['session_id'] + ':' + rid, 'path': str(path),
            'prompt': prompt, 'model': model, 'client': os.environ['OMNIROUTE_IMAGE_CLIENT'],
            'size': payload['size'], 'quality': payload['quality'],
            'duration_seconds': round(time.monotonic() - started, 3),
            'usage': result.get('usage'), 'provider_request_id': provider_request_id,
            'created_at': time.time(),
            'reference_sha256': hashlib.sha256(base64.b64decode(reference.split(',', 1)[1])).hexdigest() if reference else None,
            'reference_path': args.get('reference_image') if args.get('reference_image') and not args['reference_image'].startswith('data:') else None,
            'note': background_note}
    atomic_json(folder / (rid + '.json'), info)
    temp = folder / (rid + '.png.tmp')
    temp.write_bytes(data)
    temp.replace(path)
    return {'content': [
        {'type': 'text', 'text': json.dumps(info)},
        {'type': 'image', 'mimeType': 'image/png', 'data': encoded},
    ]}


def request_status(folder, rid):
    state_path = folder / (rid + '.state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {'status': 'not_found'}
    if state['status'] in ('queued', 'running'):
        with (folder / (rid + '.active')).open('a') as active:
            try:
                fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = dict(state, status='outcome_unknown', error='Worker stopped before saving a completed image.')
            except BlockingIOError:
                pass
    state.update(session_id=folder.name, request_id=rid, ticket=folder.name + ":" + rid)
    state['next_step'] = ('The worker continues independently. Wait 20–30 seconds before fetch_image with this ticket; repeat only while queued/running. Never regenerate this request.' if state['status'] in ('queued', 'running')
                          else 'Do not automatically regenerate. A submitted request may have been billed; an intentional retry needs a new request_id.')
    if state['status'] == 'failed' and state.get('selection') == 'gpt' and state.get('submitted'):
        state['next_step'] += ' If Gemini was already requested for this comparison, use its existing ticket. Otherwise ask the user "Would you like to try Gemini?" and wait for confirmation.'
    return {'isError': state['status'] not in ('queued', 'running'),
            'content': [{'type': 'text', 'text': json.dumps(state)}]}


CLAUDE_WAIT_SECONDS = max(0, min(38, float(os.environ.get('OMNIROUTE_IMAGE_RESPONSE_WAIT_SECONDS', '38'))))

def recover(args):
    result = saved_images(args)
    if os.environ.get('OMNIROUTE_IMAGE_CLIENT') in ('claude-desktop', 'claude-code'):
        end = time.monotonic() + CLAUDE_WAIT_SECONDS
        while not result.get('isError') and len(result['content']) == 1 and time.monotonic() < end:
            time.sleep(0.5)
            result = saved_images(args)
    return result


def prune_queue():
    # Retire orphan queue entries so a killed waiter cannot block later jobs.
    for entry in sorted((ROOT / 'queue').glob('*.json')):
        record = json.loads(entry.read_text())
        active_path = pathlib.Path(record['active'])
        if not active_path.exists():
            entry.unlink(missing_ok=True)
            continue
        with active_path.open('a') as active:
            try:
                fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                entry.unlink(missing_ok=True)
            except BlockingIOError:
                pass


def queue_slot(marker):
    while True:
        with (ROOT / '.queue.lock').open('a') as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            prune_queue()
            entries = sorted((ROOT / 'queue').glob('*.json'))
            if entries and entries[0] == marker:
                for slot in range(2):
                    candidate = (ROOT / ('.generation.slot-' + str(slot) + '.lock')).open('a')
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        marker.unlink()
                        return candidate
                    except BlockingIOError:
                        candidate.close()
        time.sleep(0.2)


def run_detached(args, global_lock, admission_guard=None):
    folder, rid = request_paths(args)
    state_path = folder / (rid + '.state.json')
    if state_path.exists() or (folder / (rid + '.reserved')).exists():
        prior = json.loads(state_path.read_text()) if state_path.exists() else {}
        if prior.get('fingerprint') != fingerprint(args):
            raise ValueError('Request ID already belongs to different inputs. Use exact recovery; do not silently substitute its image.')
        return saved_images({key: args[key] for key in ('session_id', 'request_id')})
    job = folder / (rid + '.job.json')
    with (folder / (rid + '.active')).open('a') as active:
        fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queued_args = dict(args)
        if args.get('reference_image'):
            ref = args['reference_image']
            if isinstance(ref, str) and not ref.startswith('data:'):
                ref_path = pathlib.Path(ref).expanduser().resolve()
                if any(root in ref_path.parents for root in (output_root() / 'sessions', output_root() / 'clients')) and ref_path.parent != folder:
                    raise ValueError('Selected generated reference belongs to another session; use its exact reference_ticket.')
            queued_args['reference_image'] = reference_data_url(args['reference_image'])
        write_state(state_path, {'status': 'queued', 'submitted': False, 'selection': args.get('model', 'gpt'), 'fingerprint': fingerprint(args)})
        atomic_json(job, {'args':queued_args, 'reference_path':args.get('reference_image') if args.get('reference_image') and not args['reference_image'].startswith('data:') else None})
        queue = ROOT / 'queue'
        queue.mkdir(mode=0o700, exist_ok=True)
        with (ROOT / '.queue.lock').open('a') as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            prune_queue()
            if len(list(queue.glob('*.json'))) >= 32:
                job.unlink(missing_ok=True)
                write_state(state_path, {'status':'failed','submitted':False,'error':'Image queue is full (32 waiting). No provider request sent.'})
                return request_status(folder, rid)
            marker = queue / (str(time.time_ns()).zfill(20) + '-' + str(uuid.uuid4()) + '.json')
            atomic_json(marker, {'active': str(folder / (rid + '.active'))})
        try:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'server.py'), '--worker', str(job), str(marker)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(active.fileno(),) + ((global_lock.fileno(),) if global_lock else ()) + ((admission_guard.fileno(),) if admission_guard else ()))
        except OSError:
            marker.unlink(missing_ok=True)
            job.unlink(missing_ok=True)
            write_state(state_path, {'status': 'failed', 'submitted': False, 'error': 'Could not start image worker.'})
            return request_status(folder, rid)
        try:
            budget = CLAUDE_WAIT_SECONDS
            process.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            # The worker inherited both locks. Closing our descriptors leaves it running.
            threading.Thread(target=process.wait, args=(), daemon=True).start()
            result = saved_images({key: args[key] for key in ('session_id', 'request_id')})
            if not result.get('isError') and len(result['content']) == 1:
                state = json.loads(result['content'][0]['text'])
                state['next_step'] = 'Generation continues. Wait 20–30 seconds before fetch_image with the returned ticket. If still queued/running, wait before fetching the same ticket again. Never regenerate this job.'
                result['content'][0]['text'] = json.dumps(state)
            return result
    return saved_images({key: args[key] for key in ('session_id', 'request_id')})


def worker(job, marker=None):
    state_path = job.with_name(job.name.replace('.job.json', '.state.json'))
    slot = None
    try:
        record = json.loads(job.read_text())
        args = record.get('args', record)
        if marker is not None:
            slot = queue_slot(marker)
        generate(args)
        if record.get('reference_path'):
            meta = job.with_name(job.name.replace('.job.json', '.json'))
            info = json.loads(meta.read_text())
            info['reference_path'] = record['reference_path']
            atomic_json(meta, info)
        write_state(state_path, {'status': 'completed', 'submitted': True})
    except Exception as exc:
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        error = str(exc) if isinstance(exc, ValueError) else 'Unable to process or save the image response.'
        key = os.environ.get('OMNIROUTE_IMAGE_API_KEY')
        if key:
            error = error.replace(key, '[redacted]')
        write_state(state_path, {'status': 'outcome_unknown' if isinstance(exc, OutcomeUnknown) else 'failed',
                                'submitted': previous.get('submitted', False), 'error': error[:1000]})
    finally:
        if slot:
            slot.close()
        if marker:
            marker.unlink(missing_ok=True)
        job.unlink(missing_ok=True)


def send(message):
    with OUTPUT_LOCK:
        sys.stdout.write(json.dumps(message) + '\n')
        sys.stdout.flush()


def handle_request(message):
    response = {'jsonrpc': '2.0', 'id': message['id']}
    params = message.get('params') or {}
    token = (params.get('_meta') or {}).get('progressToken')
    stop = threading.Event()
    started = time.monotonic()

    def progress():
        while not stop.wait(10):
            send({'jsonrpc': '2.0', 'method': 'notifications/progress',
                  'params': {'progressToken': token,
                             'progress': round(time.monotonic() - started, 1),
                             'message': 'Generating image'}})

    ticker = None
    if message.get('method') == 'tools/call' and isinstance(token, (str, int, float)):
        ticker = threading.Thread(target=progress, daemon=True)
        ticker.start()
    try:
        response['result'] = dispatch(message)
    except LookupError:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    except Exception:
        response['error'] = {'code': -32602, 'message': 'Invalid parameters'}
    finally:
        stop.set()
        if ticker:
            ticker.join()
    send(response)


def dispatch(message):
    method = message.get('method')
    params = message.get('params') or {}
    if method == 'initialize':
        requested = params.get('protocolVersion')
        version = requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2025-11-25'
        return {'protocolVersion': version, 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'openai-images', 'version': '1.7.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        schema = dict(SCHEMA, properties=dict(SCHEMA['properties']))
        if os.environ.get('OMNIROUTE_IMAGE_MODEL', '').startswith(('codex/', 'cx/')):
            schema['properties'].pop('background', None)
        text_schema = dict(schema, required=['prompt'])
        desktop = os.environ.get('OMNIROUTE_IMAGE_CLIENT') == 'claude-desktop'
        if desktop:
            text_schema['properties'] = {k: v for k, v in schema['properties'].items() if k not in ('session_id', 'request_id', 'reference_image')}
        edit_schema = dict(schema, properties=dict(schema['properties']), required=['prompt'])
        edit_schema['properties']['reference_ticket'] = {'type':'string','description':'Exact ticket of a generated image explicitly chosen in THIS chat. Use instead of reference_image.'}
        if desktop:
            for field in ('session_id','request_id'):
                edit_schema['properties'].pop(field, None)
        edit_schema['anyOf'] = [{'required':['reference_image']},{'required':['reference_ticket']}]
        return {'tools': [{'name': 'fetch_image', 'description': 'Return the existing image for the ticket from generate_image or edit_reference_image. Copy the ticket exactly. A queued/running status is successful, not a failure. Wait 20–30 seconds before fetching again. Never generate again or look up the newest image.', 'inputSchema': {'type':'object','properties':{'ticket':{'type':'string'}},'required':['ticket'],'additionalProperties':False}}, {'name': 'create_image_session', 'description': 'Optional advanced setup for edits or explicit recovery IDs. Skip this tool for ordinary text-to-image; call generate_image directly. Retain the returned session_id for all images in THIS chat. Never reuse another chat session.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}, {'name': 'generate_image',
            'description': 'Generate a new image directly from a prompt. Do not create a session or supply IDs. For an edit use edit_reference_image. Two jobs run concurrently across all apps. Further requests enter a shared first-in-first-out queue and start automatically. Never resubmit queued jobs. If queued/running, wait 20–30 seconds then call fetch_image with its ticket; if still pending, wait before fetching again. Do not resubmit.' if desktop else 'Generate or edit one image through OmniRoute. For a new text-to-image request, omit session_id and request_id. The server creates an isolated session and returns both IDs. Never invent IDs or automatically repeat a generation, even if the call disconnects before returning IDs. For a follow-up in a known session, supply its exact session_id and omit request_id for a new image. Retain both for recovery. Use only reference_image explicitly provided or selected in THIS conversation; never use a recovered image as a reference unless the user asks. Supply reference_image to preserve a reference photo; without it this is text-to-image. Returns the image and saved path when ready. For slow Claude calls, follow the returned recovery instructions. Generation may take several minutes. Two image jobs run across all clients; further jobs queue automatically. Preserve requested quality; do not lower it on an error unless the user asks. For queued/running results, wait 20–30 seconds before fetching the same ticket; never submit duplicate requests. Usage may be billed.',
            'inputSchema': text_schema}, {'name': 'edit_reference_image', 'description': 'Edit an explicitly selected reference using prompt and either reference_image (accessible Mac path or PNG/JPEG/WebP data URL) or reference_ticket (exact ticket returned in THIS chat). Omit session_id and request_id: server assigns them. Never select latest files or another chat output. Never replace a requested edit with text-only generation. Two jobs run concurrently across all apps. Further requests enter a shared first-in-first-out queue and start automatically. Never resubmit queued jobs. If queued/running, wait 20–30 seconds before fetch_image with the same ticket; never resubmit.', 'inputSchema': edit_schema}, {'name': 'recover_saved_image', 'description': 'Recover completed images after a connector timeout without generating again. Requires the exact session_id and request_id from the original generation call. Never recover by newest image, filename, or another conversation. There is no global image list. Use only on user request or once after a timeout. Do not repeatedly poll.', 'inputSchema': {'type': 'object', 'properties': {'session_id': {'type': 'string'}, 'request_id': {'type': 'string'}}, 'required': ['session_id', 'request_id'], 'additionalProperties': False}}]}
    if method == 'tools/call':
        try:
            if params.get('name') == 'create_image_session':
                return create_session(params.get('arguments', {}))
            if params.get('name') == 'fetch_image':
                args = params.get('arguments', {})
                if set(args) != {'ticket'} or not isinstance(args['ticket'], str) or args['ticket'].count(':') != 1:
                    raise ValueError('Copy the full ticket returned by this image request.')
                sid, rid = args['ticket'].split(':')
                return recover({'session_id':sid, 'request_id':rid})
            if params.get('name') == 'recover_saved_image':
                args = params.get('arguments', {})
                aliases = {'sessionId':'session_id','requestId':'request_id','Session id':'session_id','Request id':'request_id'}
                normalized = {aliases.get(k,k):v for k,v in args.items()}
                if len(normalized) != len(args):
                    raise ValueError('Conflicting recovery field names.')
                return recover(normalized)
            if params.get('name') in ('generate_image', 'edit_reference_image'):
                if params.get('name') == 'edit_reference_image' and not (params.get('arguments', {}).get('reference_image') or params.get('arguments', {}).get('reference_ticket')):
                    raise ValueError('edit_reference_image requires reference_image: an accessible absolute Mac path or image data URL.')
                lock_file = None
                admission_guard = None
                try:
                    admission_guard = (ROOT / '.generation.lock').open('a')
                    try:
                        fcntl.flock(admission_guard.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise ValueError('Image connector maintenance in progress; wait before submitting.') from None
                    args = dict(params.get('arguments', {}))
                    if args.get('model', 'gpt') not in SCHEMA['properties']['model']['enum']:
                        raise ValueError('Choose model gpt or gemini.')
                    if params.get('name') == 'edit_reference_image':
                        ticket = args.pop('reference_ticket', None)
                        if ticket is not None:
                            if args.get('reference_image') or not isinstance(ticket, str) or ticket.count(':') != 1:
                                raise ValueError('Supply exactly one selected reference: reference_image or reference_ticket.')
                            sid, rid = ticket.split(':')
                            keys = {'session_id':sid, 'request_id':rid}
                            source = saved_images(keys)
                            if source.get('isError') or len(source['content']) < 2:
                                raise ValueError('Reference ticket has no completed image in this client.')
                            if 'session_id' in args and args['session_id'] != sid:
                                raise ValueError('Reference ticket conflicts with session_id.')
                            folder, _ = request_paths(keys)
                            args['session_id'] = sid
                            args['reference_image'] = str(folder / (rid + '.png'))
                        if 'session_id' not in args:
                            if 'request_id' in args:
                                raise ValueError('Omit both IDs for a new edit, or supply the original session_id for an explicit request_id.')
                            args['session_id'] = new_session_id()
                        if 'request_id' not in args:
                            args['request_id'] = str(uuid.uuid4())
                    if params.get('name') == 'generate_image':
                        if 'session_id' not in args:
                            if 'request_id' in args or args.get('reference_image') is not None:
                                raise ValueError('An explicit request_id or reference_image requires the original session_id. For a new text-only image omit both IDs.')
                            args['session_id'] = new_session_id()
                        if 'request_id' not in args:
                            args['request_id'] = str(uuid.uuid4())
                    return run_detached(args, lock_file, admission_guard)
                finally:
                    if lock_file:
                        lock_file.close()
                    if admission_guard:
                        admission_guard.close()
            raise ValueError('Unknown tool.')
        except ValueError as exc:
            return {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
        except Exception:
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Unable to process or save the image response.'}]}
    raise LookupError('Method not found')


def main():
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError()
        except (ValueError, TypeError):
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON-RPC message'}})
            continue
        if 'id' not in message:
            continue
        if message.get('method') == 'tools/call':
            threading.Thread(target=handle_request, args=(message,), daemon=True).start()
        else:
            handle_request(message)


if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) in (3, 4) and sys.argv[1] == '--worker':
        worker(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]) if len(sys.argv) == 4 else None)
    else:
        main()
