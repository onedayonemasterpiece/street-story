"""Exercise the real launcher up to adb's remote instrumentation boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('resume_id', ['', 'story_12345678abcdefgh'])
def test_fresh_and_resume_launch_have_complete_remote_instrumentation_args(tmp_path, resume_id):
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    (fixtures / 'config.json').write_text(json.dumps({'backend_url': 'https://street-story.kenigevents.ru'}))
    for name in ['token.txt', 'photo.jpg', *(f'voice-{i}.pcm' for i in range(1, 7))]:
        (fixtures / name).write_bytes(b'fixture')
    commands = tmp_path / 'bin'
    commands.mkdir()
    capture = tmp_path / 'instrumentation.json'
    adb = commands / 'adb'
    adb.write_text('''#!/usr/bin/env python3
import json, os, shlex, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['shell', 'ping']:
    print('PING street-story.kenigevents.ru (203.0.113.10)')
elif args[:3] == ['shell', 'am', 'instrument']:
    # adb shell joins argv before the device parses it. Reproduce that boundary.
    remote = shlex.split(' '.join(args[1:]))
    Path(os.environ['ADB_CAPTURE_PATH']).write_text(json.dumps(remote))
    sys.exit(91)  # Stop before any app, model, or publication operation.
elif args[:1] == ['exec-in']:
    sys.stdin.buffer.read()
''')
    adb.chmod(0o755)
    env = {**os.environ, 'PATH': f'{commands}:{Path(sys.executable).parent}:{os.environ["PATH"]}',
           'LIVE_E2E_FIXTURE_DIR': str(fixtures), 'LIVE_E2E_ARTIFACT_DIR': str(tmp_path / 'evidence'),
           'LIVE_E2E_RESUME_STORY_ID': resume_id, 'LIVE_E2E_KEEP_PUBLICATION': 'true',
           'LIVE_E2E_IDENTITY_ONLY': 'false', 'LIVE_E2E_MORE_ONLY': 'false',
           'ADB_CAPTURE_PATH': str(capture)}
    script = Path(__file__).resolve().parents[2] / '.github/scripts/live-e2e-android.sh'
    result = subprocess.run(['bash', str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 91, result.stderr
    remote = json.loads(capture.read_text())
    assert remote[:3] == ['am', 'instrument', '-w']
    component = 'com.onedayonemasterpiece.streetstory.test/androidx.test.runner.AndroidJUnitRunner'
    assert remote[-1] == component
    extras = remote[3:-1]
    assert len(extras) % 3 == 0
    decoded = {}
    for offset in range(0, len(extras), 3):
        flag, name, value = extras[offset:offset + 3]
        assert flag == '-e' and value and value != component
        decoded[name] = value
    assert decoded['keepPublication'] == 'true'
    assert decoded['identityOnly'] == decoded['moreOnly'] == 'false'
    if resume_id:
        assert decoded['resumeStoryId'] == resume_id
    else:
        assert 'resumeStoryId' not in decoded
