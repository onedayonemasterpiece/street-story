#!/usr/bin/env python3
"""Verify and materialize the immutable shared native SDK for Android builds.

No network access, executable archive entries, product forks or runtime secrets.
The source archive is a versioned dependency, not a disposable diagnostic copy.
"""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def prepare(root: Path = ROOT) -> dict:
    lock = json.loads((root / 'live-framework.lock.json').read_text(encoding='utf-8'))
    archive = (root / lock['archive']).resolve()
    archive.relative_to(root.resolve())
    raw = archive.read_bytes()
    if len(raw) > 5_000_000 or hashlib.sha256(raw).hexdigest() != lock['sha256']:
        raise ValueError('shared Live archive digest mismatch')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as bundle:
        files = [item for item in bundle.getmembers() if item.isfile()]
        package = [item for item in files if len(PurePosixPath(item.name).parts) == 2 and item.name.endswith('/package.json')]
        if len(package) != 1:
            raise ValueError('shared Live archive package manifest missing')
        manifest = json.load(bundle.extractfile(package[0]))
        if manifest.get('name') != '@onedayonemasterpiece/live-interaction' or manifest.get('version') != lock['version']:
            raise ValueError('shared Live archive identity/version mismatch')
        selected = []
        for item in files:
            parts = PurePosixPath(item.name).parts[1:]
            if parts[:4] != ('android', 'src', 'main', 'java') or not item.name.endswith('.java'):
                continue
            if '..' in parts or item.size > 200_000:
                raise ValueError('unsafe shared Live archive member')
            relative = Path(*parts)
            target = root / '.live-framework' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = bundle.extractfile(item).read(200_001)
            if len(content) != item.size:
                raise ValueError('shared Live source length mismatch')
            if not target.exists() or target.read_bytes() != content:
                target.write_bytes(content)
            selected.append(str(relative))
        if not selected:
            raise ValueError('shared Live native SDK missing')
    receipt = {'version': lock['version'], 'sha256': lock['sha256'], 'source_files': selected}
    (root / '.live-framework' / 'verified.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


if __name__ == '__main__':
    print(json.dumps(prepare(), sort_keys=True))
