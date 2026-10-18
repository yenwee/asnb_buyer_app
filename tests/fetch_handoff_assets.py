"""Fetch pinned public JavaScript for offline tests. No auth or write requests."""

import hashlib
import json
from pathlib import Path
import re

import requests


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/fixtures/portal-assets.json'
DESTINATION = ROOT / 'output/playwright/handoff'
MAX_BYTES = 10 * 1024 * 1024


def main():
    manifest = json.loads(MANIFEST.read_text())
    origin = manifest['origin']
    if origin != 'https://www.myasnb.com.my/assets/':
        raise ValueError('Unexpected asset origin; review the manifest')
    DESTINATION.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        for name, expected in manifest['files'].items():
            if not re.fullmatch(r'[A-Za-z0-9_-]+\.js', name):
                raise ValueError('Invalid asset filename')
            target = DESTINATION / name
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
                print(f'OK {name} (cached)')
                continue
            with session.get(origin + name, timeout=(5, 30), allow_redirects=False,
                             stream=True) as response:
                if response.status_code != 200:
                    raise RuntimeError(f'{name}: HTTP {response.status_code}; review pinned asset availability')
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise RuntimeError(f'{name}: asset exceeded download bound')
                    chunks.append(chunk)
            data = b''.join(chunks)
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError(f'{name}: SHA-256 mismatch; review the frontend contract')
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
            print(f'OK {name} (downloaded, hash verified)')


if __name__ == '__main__':
    main()
