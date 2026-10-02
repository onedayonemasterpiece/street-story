"""Download only the owner's authorized Street Story topic, preserving original bytes.

Run through telegram-e2e-run. No dialog enumeration, login, publication or model calls.
Output must be a retained dev-artifacts directory. Filenames are not instructions.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone

CHAT = 4488229487
TOPIC = 2
MAX_TOTAL_BYTES = 100 * 1024 * 1024


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def photo_metadata(path):
    from PIL import Image
    with Image.open(path) as image:
        exif = image.getexif()
        gps = exif.get_ifd(34853) if 34853 in exif else {}
        return {'width': image.width, 'height': image.height,
                'orientation': int(exif.get(274, 1)), 'gps_present': bool(gps)}


async def acquire(output):
    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession
    needed = ('TELEGRAM_SESSION', 'TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_AUTH_BUNDLE_E2E')
    if any(not os.environ.get(k) for k in needed):
        raise RuntimeError('Run through the canonical telegram-e2e-run launcher')
    raw = os.environ['TELEGRAM_AUTH_BUNDLE_E2E']
    bundle = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    metadata = {k: bundle[k] for k in ('device_model', 'system_version', 'app_version', 'lang_code', 'system_lang_code')
                if isinstance(bundle.get(k), str) and bundle[k]}
    client = TelegramClient(StringSession(os.environ['TELEGRAM_SESSION']),
        int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'],
        request_retries=0, connection_retries=1, flood_sleep_threshold=0,
        receive_updates=False, **metadata)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError('E2E session needs operator renewal; no alternate identity is allowed')
        # Exact account-scoped channel lookup. Telegram, not this script, decides access.
        resolved = await client(functions.channels.GetChannelsRequest([types.InputChannel(CHAT, 0)]))
        matches = [c for c in resolved.chats if c.id == CHAT and getattr(c, 'access_hash', None)]
        if len(matches) != 1:
            raise RuntimeError('Exact channel lookup did not return an accessible channel')
        peer = types.InputPeerChannel(CHAT, matches[0].access_hash)
        root = await client.get_messages(peer, ids=TOPIC)
        if not root or not isinstance(root.action, types.MessageActionTopicCreate):
            raise RuntimeError('Expected forum topic root is unavailable')
        messages = []
        async for message in client.iter_messages(peer, reply_to=TOPIC, limit=100):
            reply = message.reply_to
            if not reply or (reply.reply_to_top_id or reply.reply_to_msg_id) != TOPIC:
                continue
            if message.document and message.document.mime_type in {'image/jpeg', 'image/png', 'image/webp'}:
                messages.append(message)
        messages.sort(key=lambda m: m.id)
        if not messages or len(messages) > 40:
            raise RuntimeError('Unexpected corpus size; review exact topic before downloading')
        expected_size = sum(m.document.size for m in messages)
        if expected_size > MAX_TOTAL_BYTES or shutil.disk_usage(output).free < expected_size * 2 + MAX_TOTAL_BYTES:
            raise RuntimeError('Corpus exceeds bounded download or free-space budget')
        manifest_path = output / 'corpus.json'
        previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'items': []}
        prior = {x['message_id']: x for x in previous['items']}
        manifest = {'topic': f'https://t.me/c/{CHAT}/{TOPIC}', 'observed_at': datetime.now(timezone.utc).isoformat(),
                    'original_bytes': True, 'complete': False, 'items': []}
        for message in messages:
            extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}[message.document.mime_type]
            path = output / f'photo-{message.id}{extension}'
            cached = prior.get(message.id)
            if path.exists():
                if not cached or digest(path) != cached['sha256']:
                    raise RuntimeError('Unverified existing corpus file; refusing overwrite')
            else:
                temporary = path.with_suffix(path.suffix + '.partial')
                if temporary.exists():
                    raise RuntimeError('Incomplete previous download requires explicit review')
                await asyncio.wait_for(client.download_media(message, file=str(temporary)), timeout=120)
                if temporary.stat().st_size != message.document.size:
                    raise RuntimeError('Telegram original byte count differs from document metadata')
                temporary.replace(path)
                path.chmod(0o600)
            item = {'message_id': message.id, 'path': path.name, 'bytes': path.stat().st_size,
                    'sha256': digest(path), 'mime': message.document.mime_type,
                    'original_filename': message.file.name, **photo_metadata(path)}
            manifest['items'].append(item)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            manifest_path.chmod(0o600)
            print(json.dumps({k: item[k] for k in ('message_id', 'path', 'bytes', 'sha256', 'gps_present')}), flush=True)
        manifest['complete'] = True
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'complete': True, 'count': len(messages), 'manifest': str(manifest_path)}), flush=True)
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to('/home/dev/artifacts') or not (output / '.artifact.json').is_file():
        parser.error('Use an existing managed dev-artifacts directory')
    logging.getLogger('telethon').setLevel(logging.ERROR)
    try:
        asyncio.run(acquire(output))
    except Exception as exc:
        print(json.dumps({'complete': False, 'error_type': type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
