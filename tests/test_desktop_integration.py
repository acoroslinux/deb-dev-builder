import configparser
import os
from pathlib import Path
import runpy
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.apt_manager import APTManager
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.desktop_integration import configure_desktop_integration, session_launcher
from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.runtime_config import load_runtime


@pytest.mark.parametrize('desktop', ['xfce', 'kde', 'gnome', 'mate', 'cinnamon', 'lxqt', 'i3', 'sway', 'hyprland'])
def test_desktop_baseline_is_available_outside_the_live_variant(desktop):
    build = BuildOrchestrator(distro='devuan-6', desktop=desktop, mode='mock')
    packages = set(build.config['software'])
    assert {'network-manager', 'wpasupplicant', 'pipewire-pulse', 'wireplumber',
            'pulseaudio-utils', 'dbus-x11', 'libpam-elogind', 'polkitd', 'udisks2', 'upower'} <= packages
    if desktop in {'sway', 'hyprland'}:
        assert {'xwayland', 'console-setup', 'kbd', 'xkb-data'} <= packages


@pytest.mark.parametrize('distro', ['debian-14', 'debian-sid', 'devuan-testing', 'devuan-ceres'])
def test_current_suite_packages_replace_removed_names(distro):
    build = BuildOrchestrator(distro=distro, desktop='xfce', variant='live', mode='mock')
    packages = set(build.config['software'])
    assert not {'network-manager-gnome', 'libsmbclient', 'p7zip-full'} & packages
    assert {'network-manager-applet', 'nm-connection-editor', 'libsmbclient0', '7zip'} <= packages


@pytest.mark.parametrize('distro,mirror,suite', [
    ('debian-13', 'https://deb.debian.org/debian', 'trixie-backports'),
    ('devuan-6', 'https://pkgmaster.devuan.org/merged', 'excalibur-backports'),
])
def test_hyprland_uses_its_own_distribution_backports(distro, mirror, suite):
    build = BuildOrchestrator(distro=distro, desktop='hyprland', mode='mock')
    assert build.config['backports_mirror'] == mirror
    assert build.config['backports_suite'] == suite
    assert build.config['apt_target_release'] == suite


@pytest.mark.parametrize('release', [None, 'trixie-backports'])
def test_apt_selects_backports_only_for_profiles_that_request_them(tmp_path, release, monkeypatch):
    chroot = ChrootManager(tmp_path, mode='real')
    calls = []
    monkeypatch.setattr(chroot, 'run_in_chroot', lambda command, **kwargs: (calls.append(command) or SimpleNamespace(returncode=0)))
    manager = APTManager(chroot, {'apt_target_release': release})
    monkeypatch.setattr(manager, 'sync_cache_to_target', lambda: None)
    monkeypatch.setattr(manager, 'sync_cache_from_target', lambda: None)
    manager.install_packages(['hyprland' if release else 'sway'])
    command, = calls
    assert ('-t' in command) == bool(release)
    if release:
        assert command[command.index('-t') + 1] == release
    assert command[command.index('install') + 1] == '-y'


@pytest.mark.parametrize('distro', ['devuan-testing', 'devuan-ceres'])
def test_gnome_rejects_suites_with_incompatible_gdm_elogind_dependencies(distro):
    build = BuildOrchestrator(distro=distro, desktop='gnome', mode='mock')
    assert not build.validate()['valid']


@pytest.mark.parametrize('desktop', ['i3', 'sway', 'hyprland'])
def test_minimal_session_configuration_preserves_native_appearance(tmp_path, desktop):
    runtime = load_runtime()
    policy = runtime['desktop']['integration']
    agent = tmp_path / policy['polkit_agent']
    agent.parent.mkdir(parents=True)
    agent.touch()
    vendor = tmp_path / policy['sddm_session_candidates'][0]
    vendor.parent.mkdir(parents=True)
    vendor.write_text('#!/bin/sh\nexec "$@"\n')
    native = tmp_path / 'etc/sway/config'
    native.parent.mkdir(parents=True)
    native.write_text('font monospace 10\ninclude /etc/sway/config.d/*\n')
    example = tmp_path / policy['hyprland_examples'][0]
    example.parent.mkdir(parents=True)
    original = '-- Native appearance and key bindings\nhl.config({ general = { gaps_in = 5 } })\n'
    example.write_text(original)
    config = {'desktop': desktop, 'init_system': 'sysvinit'}
    for _ in range(2):
        configure_desktop_integration(tmp_path, config, runtime)
    assert native.read_text() == 'font monospace 10\ninclude /etc/sway/config.d/*\n'
    if desktop == 'i3':
        autostart = (tmp_path / policy['autostart']).read_text()
        assert 'OnlyShowIn=i3;' in autostart
    elif desktop == 'sway':
        assert 'exec /' + policy['launcher'] + ' sway' in (tmp_path / policy['sway_dropin']).read_text()
    else:
        content = (tmp_path / policy['hyprland_skel'] / 'hyprland.lua').read_text()
        assert content.startswith(original)
        assert content.count(policy['launcher']) == 1
        assert 'os.getenv("XKB_DEFAULT_LAYOUT")' in content
    if desktop != 'i3':
        wrapper = tmp_path / policy['sddm_launcher']
        assert wrapper.stat().st_mode & 0o777 == 0o755
        assert 'dbus-run-session --' in wrapper.read_text()


@pytest.mark.parametrize('existing_bus', [False, True])
def test_wayland_wrapper_preserves_vendor_arguments_and_system_keyboard(tmp_path, existing_bus):
    runtime = load_runtime()
    policy = runtime['desktop']['integration']
    agent = tmp_path / policy['polkit_agent']
    agent.parent.mkdir(parents=True)
    agent.touch()
    vendor = tmp_path / policy['sddm_session_candidates'][0]
    vendor.parent.mkdir(parents=True)
    vendor.write_text('#!/bin/sh\nprintf "%s|%s|%s|%s" "$1" "$DBUS_SESSION_BUS_ADDRESS" "$XKB_DEFAULT_LAYOUT" "$XKB_DEFAULT_VARIANT"\n')
    vendor.chmod(0o755)
    configure_desktop_integration(tmp_path, {'desktop': 'sway'}, runtime)
    keyboard = tmp_path / 'keyboard'
    keyboard.write_text('XKBLAYOUT=pt\nXKBVARIANT=nodeadkeys\n')
    wrapper = tmp_path / policy['sddm_launcher']
    wrapper.write_text(wrapper.read_text().replace('/' + policy['sddm_session_candidates'][0], str(vendor)).replace('/etc/default/keyboard', str(keyboard)))
    tools = tmp_path / 'bin'
    tools.mkdir()
    dbus = tools / 'dbus-run-session'
    dbus.write_text('#!/bin/sh\n[ "$1" = -- ] && shift\nexport DBUS_SESSION_BUS_ADDRESS=fixture-new-bus\nexec "$@"\n')
    dbus.chmod(0o755)
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'XDG_RUNTIME_DIR': str(tmp_path / 'runtime')}
    env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    if existing_bus:
        env['DBUS_SESSION_BUS_ADDRESS'] = 'fixture-existing-bus'
    result = subprocess.run([str(wrapper), 'sway --verbose'], env=env, capture_output=True, text=True, check=True)
    assert result.stdout == 'sway --verbose|fixture-' + ('existing' if existing_bus else 'new') + '-bus|pt|nodeadkeys'


def test_session_services_run_once_and_stop_owned_children(tmp_path):
    runtime = load_runtime()
    policy = dict(runtime['desktop']['integration'])
    tools = tmp_path / 'bin'
    tools.mkdir()
    events = tmp_path / 'events'
    agent = tools / 'polkit-agent'
    agent.write_text('#!/bin/sh\n[ ! -e /proc/$$/fd/9 ] || exit 9\necho $$ > "$AGENT_PID"\necho agent >> "$EVENTS"\nexec sleep 30\n')
    agent.chmod(0o755)
    policy['polkit_agent'] = str(agent).lstrip('/')
    for name in ('dex', 'dbus-update-activation-environment', 'swayidle'):
        tool = tools / name
        tool.write_text('#!/bin/sh\necho ' + name + ' >> "$EVENTS"\n' + ('exec sleep 30\n' if name == 'swayidle' else ''))
        tool.chmod(0o755)
    script = tmp_path / 'start'
    script.write_text(session_launcher(policy, 'sysvinit'))
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'XDG_RUNTIME_DIR': str(tmp_path), 'EVENTS': str(events), 'AGENT_PID': str(tmp_path / 'pid')}
    first = subprocess.Popen(['sh', str(script), 'sway'], env=env)
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / 'pid').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / 'pid').exists()
        subprocess.run(['sh', str(script), 'sway'], env=env, check=True, timeout=3)
        assert events.read_text().splitlines().count('agent') == 1
    finally:
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=3)
    pid = int((tmp_path / 'pid').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize('init_system', ['systemd', 'sysvinit', 'openrc', 'runit'])
def test_service_detection_uses_native_init_files(tmp_path, init_system):
    if init_system == 'systemd':
        native = tmp_path / 'usr/lib/systemd/system/NetworkManager.service'
    elif init_system == 'runit':
        native = tmp_path / 'etc/sv/network-manager/run'
    else:
        native = tmp_path / 'etc/init.d/network-manager'
    native.parent.mkdir(parents=True)
    native.touch()
    chroot = ChrootManager(tmp_path, mode='real')
    calls = []
    chroot.run_in_chroot = lambda command, **kwargs: (calls.append(command) or SimpleNamespace(returncode=0))
    SystemCustomizer(chroot, {'init_system': init_system}).setup_services()
    if init_system == 'runit':
        assert (tmp_path / 'etc/service/network-manager').resolve() == (tmp_path / 'etc/sv/network-manager').resolve()
    else:
        assert any(('NetworkManager' if init_system == 'systemd' else 'network-manager') in command for command in calls)


def test_sddm_greeter_does_not_require_a_second_wayland_compositor(tmp_path):
    customizer = SystemCustomizer(ChrootManager(tmp_path, mode='real'), {
        'desktop': 'sway', 'display_manager': 'sddm', 'session_type': 'wayland'})
    customizer.configure_autologin()
    parser = configparser.ConfigParser()
    parser.read(tmp_path / 'etc/sddm.conf')
    assert parser['General']['DisplayServer'] == 'x11'
    assert parser['Autologin']['Session'] == 'sway.desktop'


def test_package_snapshot_parser_reads_real_and_virtual_names(tmp_path):
    parser = runpy.run_path('tools/audit-desktops.py')['package_names']
    index = tmp_path / 'Packages'
    index.write_text('Package: native\nProvides: virtual (= 1), other-virtual\n\nPackage: second\n')
    assert parser(index) == {'native', 'second', 'virtual', 'other-virtual'}
