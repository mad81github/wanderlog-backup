"""Read-only Wanderlog archive. One execution, one immutable S3 snapshot."""
import argparse
import concurrent.futures
import hashlib
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse, quote
import uuid
import zipfile

import boto3
from botocore.config import Config
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STATIC = 'https://itin-dev.wanderlogstatic.com'

def log(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)

def fetch(url, limit=40 * 1024 * 1024):
    # Only fetch known content hosts. Never follow arbitrary links in notes/emails.
    allowed = {'wanderlog.com', 'itin-dev.wanderlogstatic.com', 'maps.gstatic.com'}
    allowed.update(f'lh{i}.googleusercontent.com' for i in range(1, 7))
    with requests.Session() as session:
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.port not in (None, 443):
                raise ValueError('Unapproved content host')
            with session.get(url, timeout=(15, 60), stream=True, allow_redirects=False) as r:
                if r.is_redirect:
                    from urllib.parse import urljoin
                    url = urljoin(url, r.headers['Location'])
                    continue
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Content exceeds size limit')
                    chunks.append(chunk)
                return b''.join(chunks), r.headers.get('Content-Type', '').split(';')[0]
    raise ValueError('Too many redirects')

def parse_plan(raw):
    source = raw.decode('utf-8')
    marker = re.search(r'window\.__MOBX_STATE__\s*=\s*', source)
    if not marker:
        raise ValueError('Wanderlog state missing; login or page format changed')
    state = json.JSONDecoder().raw_decode(source[marker.end():])[0]
    data = state['tripPlanStore']['data']
    plan = data['tripPlan']
    if not plan.get('id') or not plan.get('title') or not plan.get('itinerary', {}).get('sections'):
        raise ValueError('Incomplete trip response')
    return data

def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)

def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def archive(url, root):
    raw, _ = fetch(url)
    data = parse_plan(raw)
    plan = data['tripPlan']
    expected = os.getenv('EXPECTED_PLAN_ID')
    if expected and str(plan['id']) != expected:
        raise ValueError('Unexpected trip ID')
    (root / 'source.html').write_bytes(raw)
    save_json(root / 'trip.json', plan)
    save_json(root / 'resources.json', data.get('resources', {}))
    sections = plan['itinerary']['sections']
    emails = sorted({a['id'] for a in walk(plan) if isinstance(a, dict) and a.get('type') == 'email' and 'id' in a})
    files = {a['key']: a for a in walk(plan) if isinstance(a, dict) and a.get('type') == 'file' and 'key' in a}
    key = urlparse(url).path.split('/')[2]
    manifest = {'schemaVersion': 1, 'createdAt': datetime.now(timezone.utc).isoformat(),
                'planId': plan['id'], 'title': plan['title'], 'source': url,
                'sections': len(sections), 'blocks': sum(len(s.get('blocks', [])) for s in sections),
                'emailCount': len(emails), 'attachmentCount': len(files), 'errors': [], 'assets': []}
    log('trip_read', sections=manifest['sections'], blocks=manifest['blocks'], emails=len(emails), attachments=len(files))
    for email_id in emails:
        try:
            body, _ = fetch(f'https://wanderlog.com/api/tripPlans/{quote(key, safe="")}/emails/{email_id}')
            result = json.loads(body)
            if result.get('success') is not True or not isinstance(result.get('data'), dict):
                raise ValueError('Email response not successful')
            email = result['data']
            if not email.get('text') and not email.get('sanitizedHtml'):
                raise ValueError('Email body missing')
            save_json(root / 'emails' / f'{email_id}.json', email)
            # Escape the original body rather than executing HTML from received mail.
            content = email.get('text') or email.get('sanitizedHtml', '')
            (root / 'emails' / f'{email_id}.txt').write_text(content, encoding='utf-8')
            for a in walk(email):
                if isinstance(a, dict) and a.get('type') == 'file' and 'key' in a:
                    files[a['key']] = a
        except Exception as exc:
            manifest['errors'].append({'kind': 'email', 'id': email_id, 'error': type(exc).__name__})
    jobs = []
    for file_key, attachment in files.items():
        name = re.sub(r'[^\w. -]', '_', attachment.get('fileName', file_key))[:180]
        jobs.append((f'{STATIC}/attachment/{quote(file_key, safe="")}', 'attachment', name, attachment.get('contentType')))
    image_urls = {v for v in walk(plan) if isinstance(v, str) and v.startswith('https://')
                  and (urlparse(v).hostname or '') in {f'lh{i}.googleusercontent.com' for i in range(1, 7)}}
    for obj in walk(plan):
        if isinstance(obj, dict):
            for field in ('imageKey', 'headerImageKey'):
                if isinstance(obj.get(field), str) and obj[field]:
                    image_urls.add(f'{STATIC}/freeImage/{quote(obj[field], safe="")}')
    for image_key in plan.get('topImageKeys', []):
        if isinstance(image_key, str):
            image_urls.add(f'{STATIC}/freeImage/{quote(image_key, safe="")}')
    jobs.extend((u, 'image', '', None) for u in sorted(image_urls))
    def download(job):
        source, kind, name, expected_type = job
        ident = hashlib.sha256(source.encode()).hexdigest()[:24]
        try:
            body, content_type = fetch(source)
            if not body or (kind == 'image' and not content_type.startswith('image/')):
                raise ValueError('Unexpected asset content')
            if kind == 'attachment' and expected_type == 'application/pdf' and not body.startswith(b'%PDF'):
                raise ValueError('Invalid PDF response')
            extension = mimetypes.guess_extension(content_type) or '.bin'
            relative = f'{kind}s/{ident}-{name}' if name else f'images/{ident}{extension}'
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {'kind': kind, 'source': source, 'path': relative, 'bytes': len(body), 'sha256': sha(destination)}
        except Exception as exc:
            return {'kind': kind, 'source': source, 'error': type(exc).__name__,
                    'httpStatus': exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(download, jobs):
            manifest['errors' if 'error' in result else 'assets'].append(result)
    manifest['attachmentCount'] = len(files)
    manifest['imageCount'] = len(image_urls)
    core_errors = [e for e in manifest['errors'] if e['kind'] != 'image']
    manifest['status'] = 'partial' if core_errors else ('complete_with_media_errors' if manifest['errors'] else 'complete')
    # Standalone offline reading page; preserves every field as escaped JSON too.
    parts = ['<!doctype html><html lang="es"><meta charset="utf-8">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'">',
             '<title>Respaldo de Wanderlog</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:240px;max-height:180px}details{margin:18px 0}</style>',
             f'<h1>{html.escape(plan["title"])}</h1><p>{html.escape(manifest["createdAt"])}</p>',
             '<p>Copia independiente para lectura. Los JSON conservan todos los campos; no es una importación automática a Wanderlog.</p>',
             '<a href="trip.json">Datos completos</a> · <a href="manifest.json">Inventario y errores</a>']
    for section in sections:
        heading = section.get('heading') or section.get('date') or section.get('type', '')
        parts.append(f'<details><summary>{html.escape(str(heading))}</summary><pre>{html.escape(json.dumps(section,ensure_ascii=False,indent=2))}</pre></details>')
    parts.append('<h2>Correos de reservas</h2>')
    for email_file in sorted((root / 'emails').glob('*.txt')) if (root / 'emails').exists() else []:
        parts.append(f'<p><a href="emails/{email_file.name}">{email_file.name}</a></p>')
    parts.append('<h2>Archivos y fotografías</h2>')
    for asset in manifest['assets']:
        path = html.escape(asset['path'], quote=True)
        parts.append(f'<p><a href="{path}">{path}</a></p>' if asset['kind'] == 'attachment' else f'<a href="{path}"><img loading="lazy" src="{path}"></a>')
    (root / 'index.html').write_text('\n'.join(parts) + '</html>', encoding='utf-8')
    manifest['inventory'] = [{'path': str(p.relative_to(root)), 'bytes': p.stat().st_size, 'sha256': sha(p)}
                             for p in sorted(root.rglob('*')) if p.is_file()]
    save_json(root / 'manifest.json', manifest)
    log('archive_built', status=manifest['status'], files=len(manifest['inventory']), images=len(image_urls), errors=len(manifest['errors']))
    return manifest

def upload(root, manifest):
    style = os.getenv('AWS_S3_URL_STYLE', 'virtual')
    style = {'virtual-host': 'virtual', 'path-style': 'path'}.get(style, style)
    s3 = boto3.client('s3', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                      region_name=os.getenv('AWS_DEFAULT_REGION', 'auto'),
                      config=Config(s3={'addressing_style': style}, retries={'max_attempts': 4}))
    bucket = os.environ['AWS_S3_BUCKET_NAME']
    prefix = os.getenv('BACKUP_PREFIX', 'wanderlog').strip('/') + '/' + str(manifest['planId'])
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ') + '-' + uuid.uuid4().hex[:8]
    archive_key = f'{prefix}/{stamp}/backup.zip'
    archive_path = root.parent / 'backup.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(root))
    digest = sha(archive_path)
    s3.upload_file(str(archive_path), bucket, archive_key,
                   ExtraArgs={'ContentType': 'application/zip', 'Metadata': {'sha256': digest}})
    head = s3.head_object(Bucket=bucket, Key=archive_key)
    if head['ContentLength'] != archive_path.stat().st_size or head['Metadata'].get('sha256') != digest:
        raise ValueError('Uploaded archive verification failed')
    # Verify actual stored bytes before publishing the latest-success pointer.
    remote = s3.get_object(Bucket=bucket, Key=archive_key)['Body']
    h = hashlib.sha256()
    try:
        for chunk in remote.iter_chunks(1024 * 1024):
            h.update(chunk)
    finally:
        remote.close()
    if h.hexdigest() != digest:
        raise ValueError('Stored archive checksum mismatch')
    manifest.update({'archiveKey': archive_key, 'archiveSha256': digest, 'archiveBytes': archive_path.stat().st_size})
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    s3.put_object(Bucket=bucket, Key=f'{prefix}/{stamp}/manifest.json', Body=body, ContentType='application/json')
    if manifest['status'] != 'partial':
        s3.put_object(Bucket=bucket, Key=f'{prefix}/latest.json', Body=body, ContentType='application/json')
    log('backup_uploaded_verified', archiveKey=archive_key, status=manifest['status'], bytes=archive_path.stat().st_size,
        files=len(manifest['inventory']), errors=len(manifest['errors']))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-dir', help='Build archive locally without S3 upload')
    args = parser.parse_args()
    url = os.environ['WANDERLOG_URL']
    parsed = urlparse(url)
    if parsed.hostname != 'wanderlog.com' or not re.fullmatch(r'/plan/[a-z0-9]+/[^/]+', parsed.path):
        raise ValueError('Expected a Wanderlog plan URL')
    if args.local_dir:
        root = Path(args.local_dir)
        root.mkdir(parents=True, exist_ok=False)
        manifest = archive(url, root)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'snapshot'
            root.mkdir()
            manifest = archive(url, root)
            upload(root, manifest)
    if manifest['status'] == 'partial':
        raise RuntimeError('Core attachments missing; partial snapshot saved, latest preserved')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never log raw request URLs, email bodies, credentials or confirmation codes.
        log('backup_failed', error=type(exc).__name__)
        sys.exit(1)
