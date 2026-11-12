"""Functional session supervision for standalone X11 window managers."""
import shlex

from .path_utils import rootfs_destination


def configure_x11_session(root, desktop, policy, init_system):
    session = policy['x11_sessions'][desktop]
    agent = next((name for name in [policy['polkit_agent'], *policy['polkit_agent_fallbacks']]
                  if (root / name).is_file()), None)
    if agent is None:
        raise ValueError('Standalone X11 sessions require an installed PolicyKit agent')

    def write(relative, content, mode=0o644):
        path = rootfs_destination(root, relative)
        if path.is_symlink():
            raise ValueError(f'Desktop session output must not be a symlink: {relative}')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)

    for seed in policy.get('native_seeds', {}).get(desktop, []):
        target = rootfs_destination(root, seed['target'])
        if target.exists():
            continue
        source = next((root / name for name in seed['sources'] if (root / name).is_file()), None)
        if source is None:
            raise ValueError(f'{desktop} requires its packaged native example: {seed["target"]}')
        write(seed['target'], source.read_text(), 0o755 if seed.get('executable') else 0o644)

    relative = 'usr/local/libexec/deb-dev-x11-' + desktop
    systemd = ' --systemd' if init_system == 'systemd' else ''
    script = '''#!/bin/sh
set -eu
if [ -z "${DBUS_SESSION_BUS_ADDRESS-}" ]; then
    exec dbus-run-session -- "$0" "$@"
fi
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=''' + shlex.quote(desktop) + '''
dbus-update-activation-environment''' + systemd + ''' DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE || true
children=""
cleanup() {
    trap - EXIT INT TERM
    for child in $children; do kill "$child" 2>/dev/null || true; done
    wait || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
''' + shlex.quote('/' + agent) + ''' &
children="$!"
xss-lock --transfer-sleep-lock -- i3lock --nofork &
children="$children $!"
dex --autostart --environment "$XDG_CURRENT_DESKTOP" || true
'''
    if session.get('notifications', True):
        script += 'dunst &\nchildren="$children $!"\n'
    if session.get('start_terminal'):
        script += 'xterm &\nchildren="$children $!"\n'
    script += shlex.join(session['command']) + ' &\nmanager=$!\nchildren="$children $manager"\nwait "$manager"\n'
    write(relative, script, 0o755)
    write('usr/share/xsessions/deb-dev-' + desktop + '.desktop',
          '[Desktop Entry]\nType=Application\nName=' + desktop + ' (complete session)\n'
          'Exec=/' + relative + '\nDesktopNames=' + desktop + ';\n')
