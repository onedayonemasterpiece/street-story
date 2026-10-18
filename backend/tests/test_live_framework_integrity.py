import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_native_and_python_use_same_versioned_archive(tmp_path):
    lock = json.loads((ROOT / 'live-framework.lock.json').read_text())
    raw = (ROOT / lock['archive']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == lock['sha256']
    requirements = (ROOT / 'backend/requirements.txt').read_text()
    assert f"live-interaction=={lock['python_version']}" in requirements
    spec = importlib.util.spec_from_file_location('prepare_shared_live', ROOT / 'scripts/prepare_live_framework.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'vendor').mkdir()
    shutil.copyfile(ROOT / 'live-framework.lock.json', tmp_path / 'live-framework.lock.json')
    target = tmp_path / lock['archive']
    target.write_bytes(raw)
    assert module.prepare(tmp_path)['version'] == lock['version']
    sdk = tmp_path / '.live-framework/android/src/main/java/org/onedayonemasterpiece/live/LiveSocketTransport.java'
    assert sdk.is_file()
    target.write_bytes(raw + b'changed')
    with pytest.raises(ValueError, match='digest mismatch'):
        module.prepare(tmp_path)


def test_android_live_has_no_http_audio_or_event_polling_fallback():
    controller = (ROOT / 'app/src/main/java/com/onedayonemasterpiece/streetstory/LiveSessionController.kt').read_text()
    assert 'org.onedayonemasterpiece.live.LiveSocketTransport' in controller
    assert 'startPoller' not in controller
    assert '.inputAudio(' not in controller
    assert '.events(' not in controller
    assert 'ACTION_TRANSPORT_FINISH' in controller
