#!/usr/bin/env python3
"""Read-only selected-photo metadata inventory; never print GPS positions or secrets."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from street_story.photo_metadata import inspect_gps

GPS_FIELDS = {16: 'GPSImgDirectionRef', 17: 'GPSImgDirection', 31: 'GPSHPositioningError',
              14: 'GPSTrackRef', 15: 'GPSTrack', 11: 'GPSDOP', 18: 'GPSMapDatum'}
EXIF_FIELDS = {37386: 'FocalLength', 41989: 'FocalLengthIn35mmFilm', 41988: 'DigitalZoomRatio'}


def safe_number_or_ref(value):
    if isinstance(value, (bytes, str)):
        return (value.decode('ascii', errors='replace') if isinstance(value, bytes) else value)[:40]
    try:
        number = float(value)
        return number if math.isfinite(number) else 'invalid_nonfinite'
    except (TypeError, ValueError, ZeroDivisionError):
        return 'invalid'


def inventory(path: Path):
    gps_status = inspect_gps(path)['status']
    with Image.open(path) as image:
        exif = image.getexif()
        gps = exif.get_ifd(34853)
        camera = exif.get_ifd(34665)
        return {'bytes': path.stat().st_size, 'size': list(image.size), 'exif_present': bool(exif),
                'gps_status': gps_status, 'orientation': exif.get(274),
                'gps_tag_ids': sorted(gps),
                'optional_fields': {name: safe_number_or_ref(gps[key]) if key in gps else None
                                    for key, name in GPS_FIELDS.items()} |
                                   {name: safe_number_or_ref(camera[key]) if key in camera else None
                                    for key, name in EXIF_FIELDS.items()},
                'read_only': True, 'coordinates_disclosed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--photo', type=Path)
    target.add_argument('--story')
    args = parser.parse_args()
    path = args.photo
    if args.story:
        if not re.fullmatch(r'story_[a-zA-Z0-9]{8,80}', args.story):
            parser.error('invalid story ID')
        # Same deployment root as devcoveer_install.py; metadata-only, read-only DB.
        root = Path('/home/dev/.local/state/street-story/data').resolve()
        with closing(sqlite3.connect((root / 'street-story.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT photo_path FROM stories WHERE id=?', (args.story,)).fetchone()
        if row is None:
            raise SystemExit('story not found')
        path = Path(row[0]).resolve()
        if root not in path.parents:
            raise SystemExit('photo outside deployment data root')
    print(json.dumps(inventory(path), ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
