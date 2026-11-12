"""Minimum session plumbing for standalone window managers; no visual settings."""
import shlex

from .path_utils import rootfs_destination
from .desktop_x11 import configure_x11_session
from .wayfire_session import WAYFIRE_SESSION_SCRIPT


def session_launcher(policy, init_system):
    systemd_option = ' --systemd' if init_system == 'systemd' else ''
    return '''#!/bin/sh
set -eu
runtime=${XDG_RUNTIME_DIR-}
[ -n "$runtime" ] && [ -d "$runtime" ] && [ -O "$runtime" ] || exit 1
exec 9>"$runtime/deb-dev-minimal-session.lock"
flock --nonblock 9 || exit 0
session=${1-}
case "$session" in i3|sway|Hyprland|labwc|Wayfire|weston) ;; *) exit 1 ;; esac
export XDG_CURRENT_DESKTOP="$session"
dbus-update-activation-environment''' + systemd_option + ''' DISPLAY WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE 9>&- || true
if [ "$session" != i3 ]; then
    dex --autostart --environment "$session" 9>&- || true
fi
children=""
cleanup() {
    trap - EXIT INT TERM
    for child in $children; do kill "$child" 2>/dev/null || true; done
    wait || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
''' + shlex.quote('/' + policy['polkit_agent']) + ''' 9>&- &
agent=$!
children="$agent"
if [ "$session" != i3 ] && [ "$session" != weston ] && [ "$session" != Wayfire ]; then
    swayidle -w before-sleep 'swaylock -f' 9>&- &
    children="$children $!"
fi
if [ "$session" = labwc ]; then
    mako 9>&- &
    children="$children $!"
fi
if [ "$session" = weston ]; then
    env -u WAYLAND_DISPLAY dunst 9>&- &
    children="$children $!"
fi
wait "$agent"
'''


def configure_desktop_integration(root, config, runtime):
    desktop = config.get('desktop')
    policy = runtime['desktop']['integration']
    def write(relative, text, mode=0o644):
        destination = rootfs_destination(root, relative)
        if destination.is_symlink():
            raise ValueError(f'Desktop session output must not be a symlink: {relative}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        destination.chmod(mode)

    if desktop == 'lxde':
        relative = policy['lxde_autostart']
        destination = rootfs_destination(root, relative)
        content = destination.read_text()
        # LXDE's native file names xscreensaver, while this profile selects
        # light-locker through XDG autostart. Its power manager is XFCE-only
        # in the packaged XDG entry, so LXDE needs an explicit startup line.
        lines = [line for line in content.splitlines()
                 if line.strip().split(' ', 1)[0] not in {'@xscreensaver', 'xscreensaver'}]
        if not any(line.strip().lstrip('@').split(' ', 1)[0] == 'xfce4-power-manager' for line in lines):
            lines.append('@xfce4-power-manager')
        write(relative, '\n'.join(lines) + '\n')
        return
    if desktop in policy.get('x11_sessions', {}):
        configure_x11_session(root, desktop, policy, config.get('init_system', runtime['system']['init_system']))
        return
    if desktop in policy.get('native_wayland', []) and config.get('session_type') == 'wayland':
        configure_sddm_wayland(root, policy, write)
        return
    if desktop not in [*policy['minimal_desktops'], *policy.get('additional_wayland', [])]:
        return
    agent = next((name for name in [policy['polkit_agent'], *policy['polkit_agent_fallbacks']]
                  if (root / name).is_file()), None)
    if agent is None:
        raise ValueError('The selected window manager requires an installed PolicyKit authentication agent')

    write(policy['launcher'], session_launcher({**policy, 'polkit_agent': agent}, config.get('init_system', runtime['system']['init_system'])), 0o755)
    launcher = '/' + policy['launcher']
    if desktop == 'i3':
        # Debian's native i3 configuration already runs XDG autostart via dex.
        write(policy['autostart'], '[Desktop Entry]\nType=Application\nName=Session Services\nExec=' + launcher + ' i3\nOnlyShowIn=i3;\nNoDisplay=true\nTerminal=false\n')
        return
    if desktop == 'sway':
        write(policy['sway_dropin'], '# Session services; keep the native Sway appearance and bindings.\nexec ' + launcher + ' sway\n')
    elif desktop == 'labwc':
        write('usr/share/wayland-sessions/deb-dev-labwc.desktop',
              '[Desktop Entry]\nType=Application\nName=labwc (complete session)\n'
              'Exec=labwc -s "' + launcher + ' labwc"\nDesktopNames=labwc;wlroots;\n')
    elif desktop == 'wayfire':
        write('usr/local/libexec/deb-dev-wayfire-session', WAYFIRE_SESSION_SCRIPT, 0o755)
        write('usr/share/wayland-sessions/deb-dev-wayfire.desktop',
              '[Desktop Entry]\nType=Application\nName=Wayfire (complete session)\n'
              'Exec=/usr/local/libexec/deb-dev-wayfire-session\nDesktopNames=Wayfire;wlroots;\n')
        relative = 'etc/skel/.config/wayfire.ini'
        destination = rootfs_destination(root, relative)
        example = root / 'usr/share/doc/wayfire/examples/wayfire.ini'
        if not destination.exists():
            content = example.read_text()
            if '[autostart]' not in content:
                raise ValueError('Wayfire native example must include its autostart plugin')
            write(relative, content.replace('[autostart]', '[autostart]\ndeb_dev_session = ' + launcher + ' Wayfire', 1))
    elif desktop == 'weston':
        write('usr/local/libexec/deb-dev-weston-services', '#!/bin/sh\nexec ' + launcher + ' weston\n', 0o755)
        relative = 'etc/skel/.config/weston.ini'
        if not rootfs_destination(root, relative).exists():
            write(relative, '[core]\nxwayland=true\n\n[autolaunch]\npath=/usr/local/libexec/deb-dev-weston-services\n')
    else:
        example = next((root / name for name in policy['hyprland_examples'] if (root / name).is_file()), None)
        if example is None:
            raise ValueError('Hyprland package must provide its native example configuration')
        relative = policy['hyprland_skel'] + '/hyprland' + example.suffix
        destination = rootfs_destination(root, relative)
        content = destination.read_text() if destination.is_file() else example.read_text()
        if launcher not in content:
            if example.suffix == '.lua':
                content += '\n-- Use the installed system keyboard settings and session services.\nlocal layout = os.getenv("XKB_DEFAULT_LAYOUT")\nif layout then\n    hl.config({ input = {\n        kb_layout = layout,\n        kb_variant = os.getenv("XKB_DEFAULT_VARIANT") or "",\n        kb_options = os.getenv("XKB_DEFAULT_OPTIONS") or "",\n    } })\nend\nhl.on("hyprland.start", function()\n    hl.exec_cmd("' + launcher + ' Hyprland")\nend)\n'
            else:
                content += '\n# Minimum session services.\nexec-once = ' + launcher + ' Hyprland\n'
            write(relative, content)

    configure_sddm_wayland(root, policy, write)


def configure_sddm_wayland(root, policy, write):
    # SDDM's vendor Wayland script does not create a session bus itself. Reuse
    # the PAM/systemd user bus when available, otherwise create one whose
    # lifetime is bound to the compositor. Keep the vendor command handling.
    vendor = next((name for name in policy['sddm_session_candidates'] if (root / name).is_file()), None)
    if vendor is None:
        raise ValueError('SDDM must provide its native Wayland session script')
    vendor = shlex.quote('/' + vendor)
    write(policy['sddm_launcher'], '''#!/bin/sh
set -eu
if [ -r /etc/default/keyboard ]; then
    . /etc/default/keyboard
    export XKB_DEFAULT_LAYOUT="${XKBLAYOUT:-us}"
    export XKB_DEFAULT_VARIANT="${XKBVARIANT-}"
    export XKB_DEFAULT_OPTIONS="${XKBOPTIONS-}"
    export XKB_DEFAULT_MODEL="${XKBMODEL:-pc105}"
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS-}" ] && [ -n "${XDG_RUNTIME_DIR-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
if [ -n "${DBUS_SESSION_BUS_ADDRESS-}" ]; then
    exec ''' + vendor + ''' "$@"
fi
exec dbus-run-session -- ''' + vendor + ''' "$@"
''', 0o755)
    write(policy['sddm_config'], '[Wayland]\nSessionCommand=/' + policy['sddm_launcher'] + '\n')
