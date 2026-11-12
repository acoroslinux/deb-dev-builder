import io
import os
import subprocess
import tarfile

import pytest

from deb_dev_builder.core.live_media import configure_live_media, prompt_helper
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.runtime_config import load_runtime


def test_native_eject_patch_preserves_guards_devices_and_console_fallback(tmp_path):
    packages = sorted(resolve_from_project('cache/debian').glob('*/amd64/apt/archives/live-tools_*.deb'))
    if not packages:
        pytest.skip('Native live-tools package required')
    payload = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(packages[-1])], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        member = next(item for item in archive if item.name.endswith('/bin/live-medium-eject'))
        original = archive.extractfile(member).read().decode()
    runtime = load_runtime()
    policy = runtime['customization']['live_media']
    source = tmp_path / policy['native_script']
    source.parent.mkdir(parents=True)
    source.write_text(original)
    (tmp_path / 'usr/lib/systemd/system').mkdir(parents=True)
    configure_live_media(tmp_path, runtime)
    content = source.read_text()
    configure_live_media(tmp_path, runtime)
    assert source.read_text() == content
    subprocess.run(['sh', '-n', str(source)], check=True)
    assert content.split('# Reading configuration')[0] == original.split('# Reading configuration')[0]
    assert 'ignoring usb mass storage devices' in content
    assert '(sleep 1; eject -p -m /dev/${_DEVICE})' in content
    assert 'read x < /dev/console' in content
    assert content.index(':-prepare}') < content.index('(sleep 1; eject')
    assert ' wait\n' in content
    assert content.index('!= snapshot ] || exit 0') < content.index('(sleep 1; eject')
    unit = tmp_path / policy['service_path']
    assert unit.read_text() == policy['service_content']
    for target in policy['service_targets']:
        assert (unit.parent / (target + '.wants') / unit.name).resolve() == unit
    from deb_dev_builder.core.installer_lifecycle import live_cleanup_paths
    assert policy['service_path'] in live_cleanup_paths(runtime)
    for options in ('', 'boot=live toram', 'boot=live noeject', 'boot=live netboot'):
        cmdline = tmp_path / 'cmdline'
        cmdline.write_text(options)
        script = content.replace('/proc/cmdline', str(cmdline))
        # All of these paths must exit before attempting graphical/device work.
        subprocess.run(['sh', '-e'], input=script, text=True, check=True)


@pytest.mark.parametrize('failure', [None, 'show', 'watch', 'disabled', 'vt', 'hang'])
def test_prompt_reactivates_splash_waits_for_enter_and_releases_failed_graphics(tmp_path, failure):
    runtime = load_runtime()
    policy = runtime['customization']['live_media']
    policy['presentation'] = 'plymouth'
    policy['shutdown_kernel_options'] = ['plymouth.ignore-udev']
    policy['state_file'] = str(tmp_path / 'visible')
    policy['timeout_command'] = ['timeout', '0.2']
    cmdline = tmp_path / 'cmdline'
    cmdline.write_text('quiet splash' if failure != 'disabled' else 'quiet nosplash')
    policy['cmdline'] = str(cmdline)
    tools = tmp_path / 'bin'
    tools.mkdir()
    client = tools / 'plymouth'
    client.write_text('''#!/bin/sh
printf '%s\n' "$1" >> "$EVENTS"
case "$1" in
--ping) test -f "$RUNNING" ;;
reactivate) if [ "$FAILURE" = hang ]; then sleep 3; fi ;;
--has-active-vt) test -f "$ACTIVE" ;;
show-splash) test "$FAILURE" != show ;;
watch-keystroke)
    test "$2" = --keys || exit 99
    printf '%s' "$3" > "$KEYS"
    test "$FAILURE" != watch ;;
quit) rm -f "$RUNNING" ;;
esac
''')
    client.chmod(0o755)
    daemon = tools / 'plymouthd'
    daemon.write_text('#!/bin/sh\ntouch "$RUNNING"\n[ "$FAILURE" = vt ] || touch "$ACTIVE"\nprintf "start\\n" >> "$EVENTS"\nprintf "%s\\n" "$@" > "$DAEMON_ARGS"\n')
    daemon.chmod(0o755)
    vt = tools / 'chvt'
    vt.write_text('#!/bin/sh\nprintf "switch-vt\\n" >> "$EVENTS"\ntouch "$ACTIVE"\n')
    vt.chmod(0o755)
    release = tools / 'release-renderer'
    release.write_text('#!/bin/sh\nprintf "release-renderer\\n" >> "$EVENTS"\n')
    release.chmod(0o755)
    policy['release_renderer_command'] = [str(release)]
    helper = tmp_path / 'prompt'
    helper.write_text(prompt_helper(runtime))
    subprocess.run(['sh', '-n', str(helper)], check=True)
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'FAILURE': failure or '', 'EVENTS': str(tmp_path / 'events'),
           'KEYS': str(tmp_path / 'keys'), 'RUNNING': str(tmp_path / 'running'), 'ACTIVE': str(tmp_path / 'active'), 'DAEMON_ARGS': str(tmp_path / 'daemon-args')}
    prepare = subprocess.run(['sh', str(helper), 'prepare'], env=env)
    if failure in (None, 'vt'):
        assert 'display-message' in (tmp_path / 'events').read_text().splitlines()
    if failure is None:
        # A responsive existing daemon must keep its renderer ownership.
        subprocess.run(['sh', str(helper), 'prepare'], env=env, check=True)
        again = (tmp_path / 'events').read_text().splitlines()
        assert again.count('release-renderer') == again.count('start') == 1
    wait = subprocess.run(['sh', str(helper), 'wait'], env=env)
    assert wait.returncode == (0 if failure in (None, 'vt') else 1)
    if failure == 'disabled':
        assert not (tmp_path / 'events').exists()
        return
    events = (tmp_path / 'events').read_text().splitlines()
    assert 'start' in events
    arguments = (tmp_path / 'daemon-args').read_text().splitlines()
    assert '--mode=shutdown' in arguments
    assert '--tty=/dev/tty1' in arguments
    assert arguments[arguments.index('--kernel-command-line') + 1] == 'quiet splash plymouth.ignore-udev'
    assert events.index('release-renderer') < events.index('start')
    if failure != 'hang':
        assert events.index('reactivate') < events.index('show-splash')
    if failure in (None, 'vt'):
        assert prepare.returncode == 0
        assert events.index('display-message') < events.index('watch-keystroke') < events.index('hide-message')
    elif failure in ('show', 'watch', 'hang'):
        assert events[-2:] == ['quit', 'switch-vt']
    if failure not in ('show', 'hang'):
        assert (tmp_path / 'keys').read_bytes() == b'\r\n'
    if failure == 'vt':
        assert events.index('switch-vt') < events.index('display-message')


def test_unknown_native_eject_script_is_rejected(tmp_path):
    runtime = load_runtime()
    source = tmp_path / runtime['customization']['live_media']['native_script']
    source.parent.mkdir(parents=True)
    source.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(ValueError, match='Unsupported live-tools'):
        configure_live_media(tmp_path, runtime)


@pytest.mark.parametrize('settings_fail', [False, True])
def test_retained_notice_keeps_original_vt_and_reads_enter_after_daemon_exits(tmp_path, settings_fail):
    import pty
    import time

    runtime = load_runtime()
    policy = runtime['customization']['live_media']
    policy['presentation'] = 'retained'
    if settings_fail:
        policy['terminal_settings_command'] = ['false']
    policy['state_file'] = str(tmp_path / 'visible')
    policy['cmdline'] = str(tmp_path / 'cmdline')
    (tmp_path / 'cmdline').write_text('boot=live quiet splash')
    events = tmp_path / 'events'
    client = tmp_path / 'client'
    client.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$EVENTS"\ntest ! -f "$DEAD"\n')
    client.chmod(0o755)
    console = tmp_path / 'fgconsole'
    console.write_text('#!/bin/sh\necho 7\n')
    console.chmod(0o755)
    launcher = tmp_path / 'launcher'
    launcher.write_text('#!/bin/sh\ntest "$1" = 7\n')
    launcher.chmod(0o755)
    keyboard = tmp_path / 'keyboard'
    keyboard.write_text('#!/bin/sh\nprintf "keyboard\\n" >> "$EVENTS"\n')
    keyboard.chmod(0o755)
    policy['client'] = [str(client)]
    policy['reactivate_command'] = [str(client), 'reactivate']
    policy['active_vt_command'] = [str(client), '--has-active-vt']
    policy['active_console_command'] = [str(console)]
    policy['retained_switch_command'] = [str(launcher), '{vt}']
    policy['keyboard_command'] = [str(keyboard)]
    helper = tmp_path / 'prompt'
    policy['helper'] = str(helper).lstrip('/')
    helper.write_text(prompt_helper(runtime))
    helper.chmod(0o755)
    env = {**os.environ, 'EVENTS': str(events), 'DEAD': str(tmp_path / 'dead')}
    subprocess.run([str(helper), 'snapshot'], check=True, env=env)
    assert (tmp_path / 'visible').read_text() == '7'
    before = events.read_text().splitlines()
    (tmp_path / 'dead').touch()
    subprocess.run([str(helper), 'prepare'], check=True, env=env)
    master, slave = pty.openpty()
    terminal = tmp_path / 'tty7'
    terminal.symlink_to(os.ttyname(slave))
    policy['terminal_path'] = str(tmp_path / 'tty{vt}')
    helper.write_text(prompt_helper(runtime))
    process = subprocess.Popen([str(helper), 'wait'], stdin=slave, stdout=slave, stderr=slave, env=env)
    try:
        deadline = time.monotonic() + 3
        while 'keyboard' not in events.read_text().splitlines() and time.monotonic() < deadline:
            time.sleep(.01)
        assert events.read_text().splitlines() == before + ['keyboard']
        os.write(master, b'x')
        time.sleep(.05)
        assert process.poll() is None
        os.write(master, b'\n')
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)
