"""Open the owner-approved GPS104 fixture only on an authenticated CI runner.

The public repository stores ciphertext, never the original photo or its GPS.
The existing Live test credential supplies a domain-separated encryption key;
this creates no new credential or product endpoint. Rotation requires resealing.
"""

import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CONTEXT = b"street-story/owner-approved-gps104-fixture/v1"


def fixture_key(token: str) -> bytes:
    if not token.strip():
        raise ValueError("Live test credential is required")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=CONTEXT, info=b"photo-only").derive(token.strip().encode())


def main() -> None:
    root = Path("live-android-fixtures")
    sealed = (Path(__file__).resolve().parents[1] / "fixtures/owner-gps104.aesgcm").read_bytes()
    if not 32 < len(sealed) < 9_000_000:
        raise ValueError("Unexpected encrypted fixture size")
    payload = AESGCM(fixture_key(os.environ["STREET_STORY_LIVE_TOKEN"])).decrypt(sealed[:12], sealed[12:], CONTEXT)
    length = int.from_bytes(payload[:4], "big")
    if not 1 < length < 4096:
        raise ValueError("Invalid fixture metadata")
    meta = json.loads(payload[4:4 + length])
    photo = payload[4 + length:]
    if hashlib.sha256(photo).hexdigest() != meta["source_sha256"]:
        raise ValueError("Owner original digest mismatch")
    config = json.loads((root / "config.json").read_text())
    config.pop("photo_url", None)
    config.update(meta)
    config["owner_map_selection"] = True
    source = root / "source.original"
    source.write_bytes(photo)
    source.chmod(0o600)
    (root / "config.json").write_text(json.dumps(config))
    (root / "config.json").chmod(0o600)
    # No plaintext photo, coordinates, test token or evaluator ID in provenance.
    Path("backend/live-e2e-artifacts/android-fixture-provenance.json").write_text(json.dumps({
        "fixture_id": "owner104-internal-gps", "source_sha256": meta["source_sha256"],
        "photo_coordinates_source": "embedded_exif", "photo_pixel_transforms": False,
        "photo_input_mode": "actual_mediastore_provider_uri", "owner_map_selection": True,
        "physical_mic": False, "prepared_audio": True,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
