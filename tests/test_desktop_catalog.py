"""Check session wiring and lifecycle without starting a graphical server."""
import os
from pathlib import Path
import subprocess

import pytest

from deb_dev_builder.core.desktop_integration import configure_desktop_integration
from deb_dev_builder.core.runtime_config import load_runtime
from deb_dev_builder.core.wayfire_session import WAYFIRE_SESSION_SCRIPT
from deb_dev_builder.core.config_loader import ConfigLoader, ConfigLoaderError
from deb_dev_builder.core.orchestrator import BuildOrchestrator


POLICY = load_runtime()['desktop']['integration']


@pytest.mark.parametrize('distro', ['debian-14', 'debian-testing', 'debian-sid', 'devuan-testing', 'devuan-ceres'])
def test_recent_budgie_selects_its_native_wayland_stack(distro):
    build = BuildOrchestrator(distro=distro, desktop='budgie', variant='live', mode='mock')
    assert build.config['session_type'] == 'wayland'
    assert build.config['display_manager'] == 'sddm'
    assert {'sddm', 'labwc', 'swaylock', 'xdg-desktop-portal-wlr', 'xwayland'} <= set(build.config['software'])
    assert not {'lightdm', 'lightdm-gtk-greeter'} & set(build.config['software'])


@pytest.mark.parametrize('distro', ['debian-12', 'debian-13', 'devuan-5', 'devuan-6'])
def test_stable_budgie_keeps_its_native_x11_stack(distro):
    build = BuildOrchestrator(distro=distro, desktop='budgie', mode='mock')
    assert build.config['session_type'] == 'x11'
    assert build.config['display_manager'] == 'lightdm'
    assert 'labwc' not in build.config['software']


@pytest.mark.parametrize('value', ['invalid', [], None])
def test_desktop_override_rejects_invalid_protocol(value):
    with pytest.raises(ConfigLoaderError):
        ConfigLoader()._desktop_distribution_profile(
            {'distribution_overrides': {'debian': {'session_type': value}}}, 'debian', 'debian-13')


def prepare(root):
    for name in [POLICY['polkit_agent'], POLICY['sddm_session_candidates'][0]]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nexec "$@"\n')
    for seeds in POLICY['native_seeds'].values():
        for seed in seeds:
            source = root / seed['sources'][0]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('# Native example, including appearance and shortcuts\n')


def test_lxde_starts_power_management_and_retains_its_native_panel(tmp_path):
    relative = POLICY['lxde_autostart']
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text('@lxpanel --profile LXDE\n@pcmanfm --desktop --profile LXDE\n@xscreensaver -no-splash\n')
    for _ in range(2):
        configure_desktop_integration(tmp_path, {'desktop': 'lxde'}, load_runtime())
    assert path.read_text() == '@lxpanel --profile LXDE\n@pcmanfm --desktop --profile LXDE\n@xfce4-power-manager\n'


@pytest.mark.parametrize('desktop', sorted(POLICY['x11_sessions']))
def test_complete_x11_sessions_are_selectable_and_keep_native_examples(tmp_path, desktop):
    prepare(tmp_path)
    config = {'desktop': desktop, 'init_system': 'sysvinit'}
    for _ in range(2):
        configure_desktop_integration(tmp_path, config, load_runtime())
    entry = tmp_path / ('usr/share/xsessions/deb-dev-' + desktop + '.desktop')
    launcher = tmp_path / ('usr/local/libexec/deb-dev-x11-' + desktop)
    assert 'Exec=/usr/local/libexec/deb-dev-x11-' + desktop in entry.read_text()
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert subprocess.run(['sh', '-n', str(launcher)], capture_output=True).returncode == 0
    assert 'systemctl' not in launcher.read_text()
    for seed in POLICY['native_seeds'].get(desktop, []):
        target = tmp_path / seed['target']
        assert target.read_text() == (tmp_path / seed['sources'][0]).read_text()
        target.write_text('User configuration\n')
        configure_desktop_integration(tmp_path, config, load_runtime())
        assert target.read_text() == 'User configuration\n'


@pytest.mark.parametrize('desktop', ['labwc', 'wayfire', 'weston', 'phosh', 'lomiri', 'budgie'])
def test_additional_wayland_sessions_have_bus_and_keyboard_plumbing(tmp_path, desktop):
    prepare(tmp_path)
    example = tmp_path / 'usr/share/doc/wayfire/examples/wayfire.ini'
    example.parent.mkdir(parents=True)
    example.write_text('# Native appearance\n[core]\nplugins = autostart command\n[autostart]\nnotifications = mako\n')
    config = {'desktop': desktop, 'init_system': 'systemd', 'session_type': 'wayland'}
    for _ in range(2):
        configure_desktop_integration(tmp_path, config, load_runtime())
    wrapper = tmp_path / POLICY['sddm_launcher']
    assert 'dbus-run-session' in wrapper.read_text()
    assert 'XKB_DEFAULT_LAYOUT' in wrapper.read_text()
    assert subprocess.run(['sh', '-n', str(wrapper)], capture_output=True).returncode == 0
    if desktop == 'wayfire':
        text = (tmp_path / 'etc/skel/.config/wayfire.ini').read_text()
        assert '# Native appearance' in text
        assert 'plugins = autostart command' in text
        assert text.count('deb_dev_session =') == 1
    if desktop == 'labwc':
        text = (tmp_path / 'usr/share/wayland-sessions/deb-dev-labwc.desktop').read_text()
        assert 'labwc -s' in text


def test_x11_logout_reaps_owned_services_even_if_an_autostart_app_fails(tmp_path):
    prepare(tmp_path)
    configure_desktop_integration(tmp_path, {'desktop': 'awesome', 'init_system': 'sysvinit'}, load_runtime())
    binary = tmp_path / 'bin'
    binary.mkdir()
    agent = tmp_path / POLICY['polkit_agent']
    agent.write_text('#!/bin/sh\necho $$ >> "$CHILD_PIDS"\nexec sleep 30\n')
    agent.chmod(0o755)
    for name, script in {
        'dbus-update-activation-environment': 'exit 0',
        'dex': 'exit 1',
        'dunst': 'echo $$ >> "$CHILD_PIDS"\nexec sleep 30',
        'xss-lock': 'echo $$ >> "$CHILD_PIDS"\nexec sleep 30',
        'awesome': 'sleep 0.2\necho started > "$WM_STARTED"\nexit 37',
        'dbus-run-session': 'shift\nexport DBUS_SESSION_BUS_ADDRESS=test-bus\nexec "$@"',
    }.items():
        path = binary / name
        path.write_text('#!/bin/sh\n' + script + '\n')
        path.chmod(0o755)
    launcher = tmp_path / 'usr/local/libexec/deb-dev-x11-awesome'
    launcher.write_text(launcher.read_text().replace('/' + POLICY['polkit_agent'], str(agent)))
    started, children = tmp_path / 'started', tmp_path / 'children'
    env = {**os.environ, 'PATH': str(binary) + ':' + os.environ['PATH'],
           'WM_STARTED': str(started), 'CHILD_PIDS': str(children)}
    env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    result = subprocess.run([str(launcher)], env=env, timeout=5, capture_output=True)
    assert result.returncode == 37
    assert started.exists()
    for pid in children.read_text().splitlines():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_wayfire_uses_installed_keyboard_and_preserves_explicit_user_choices():
    namespace = {'__name__': 'test_wayfire_session'}
    exec(compile(WAYFIRE_SESSION_SCRIPT, '<wayfire-session>', 'exec'), namespace)
    render = namespace['keyboard_config']
    native = '# Native style\n[core]\nplugins = autostart\n[command]\ncommand_terminal = alacritty\n'
    result = render(native, {'XKB_DEFAULT_LAYOUT': 'pt', 'XKB_DEFAULT_VARIANT': 'nodeadkeys'})
    assert result.startswith(native)
    assert 'xkb_layout = pt\n' in result
    assert 'xkb_variant = nodeadkeys\n' in result
    user = native + '[input]\nxkb_layout = de\n[output:HDMI-A-1]\nscale = 2\n'
    result = render(user, {'XKB_DEFAULT_LAYOUT': 'pt'})
    assert 'xkb_layout = de\n' in result
    assert 'xkb_layout = pt\n' not in result
    assert result.count('[input]') == 1
    assert '[output:HDMI-A-1]\nscale = 2\n' in result
