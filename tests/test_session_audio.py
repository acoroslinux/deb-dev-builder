import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from deb_dev_builder.core.runtime_config import load_runtime
from deb_dev_builder.core.session_audio import audio_launcher, configure_session_audio


@pytest.mark.parametrize('init_system', ['systemd', 'sysvinit', 'openrc'])
def test_audio_startup_is_user_session_only_and_depends_on_selected_init(tmp_path, init_system):
    runtime = load_runtime()
    policy = runtime['customization']['session_audio']
    for relative in policy['required_binaries']:
        binary = tmp_path / relative
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.touch()
    configure_session_audio(tmp_path, runtime, init_system)
    paths = [tmp_path / policy[key] for key in ('launcher', 'autostart_path')]
    if init_system == 'systemd':
        assert not any(path.exists() for path in paths)
        return
    assert all(path.is_file() for path in paths)
    assert paths[0].stat().st_mode & 0o111
    subprocess.run(['sh', '-n', str(paths[0])], check=True)
    assert 'Exec=/' + policy['launcher'] in paths[1].read_text()
    assert '@@LAUNCHER@@' not in paths[1].read_text()
    configure_session_audio(tmp_path, runtime, 'systemd')
    assert not any(path.exists() for path in paths)


def test_audio_overlay_does_not_enable_pipewire_when_packages_are_absent(tmp_path):
    runtime = load_runtime()
    configure_session_audio(tmp_path, runtime, 'sysvinit')
    assert not (tmp_path / runtime['customization']['session_audio']['launcher']).exists()


def test_audio_launcher_starts_once_per_user_runtime_and_releases_lock(tmp_path):
    policy = load_runtime()['customization']['session_audio']
    tools = tmp_path / 'bin'
    tools.mkdir()
    identity = tools / 'id'
    identity.write_text('#!/bin/sh\necho 1000\n')
    identity.chmod(0o755)
    runtime = tmp_path / 'user runtime'
    runtime.mkdir(mode=0o700)
    daemon = tools / 'daemon'
    events = tmp_path / 'events'
    daemon.write_text('#!/bin/sh\ntouch "$XDG_RUNTIME_DIR/ready"\nprintf "started\\n" >> "$EVENTS"\nexec sleep 30\n')
    daemon.chmod(0o755)
    policy['processes'] = [[str(daemon)]]
    policy['readiness_command'] = ['test', '-f', '@@RUNTIME@@/ready']
    launcher = tmp_path / 'launcher'
    launcher.write_text(audio_launcher(policy))
    launcher.chmod(0o755)
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'], 'XDG_RUNTIME_DIR': str(runtime), 'EVENTS': str(events)}
    process = subprocess.Popen([str(launcher)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not events.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert events.read_text().splitlines() == ['started']
        subprocess.run([str(launcher)], env=env, check=True, timeout=2)
        assert events.read_text().splitlines() == ['started']
        assert process.poll() is None
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    again = subprocess.Popen([str(launcher)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while len(events.read_text().splitlines()) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert events.read_text().splitlines() == ['started', 'started']
    finally:
        os.killpg(again.pid, signal.SIGTERM)
        again.wait(timeout=3)


def test_audio_startup_failure_cleans_up_core_and_unlocks_session(tmp_path):
    policy = load_runtime()['customization']['session_audio']
    identity = tmp_path / 'id'
    identity.write_text('#!/bin/sh\necho 1000\n')
    identity.chmod(0o755)
    daemon = tmp_path / 'daemon'
    daemon.write_text('#!/bin/sh\necho $$ > "$PID_FILE"\nexec sleep 30\n')
    daemon.chmod(0o755)
    policy['processes'] = [[str(daemon)]]
    policy['readiness_command'] = ['false']
    policy['readiness_attempts'] = 2
    policy['readiness_interval'] = '0.01'
    launcher = tmp_path / 'launcher'
    launcher.write_text(audio_launcher(policy))
    launcher.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'], 'XDG_RUNTIME_DIR': str(tmp_path), 'PID_FILE': str(tmp_path / 'pid')}
    for _ in range(2):
        assert subprocess.run([str(launcher)], env=env, timeout=3).returncode == 1
        pid = int((tmp_path / 'pid').read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
