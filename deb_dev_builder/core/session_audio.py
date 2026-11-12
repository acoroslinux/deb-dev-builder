"""Start the user audio stack on desktops without a systemd user manager."""
import shlex

from .path_utils import rootfs_destination


def audio_launcher(policy):
    lines = ['#!/bin/sh', 'set -eu', 'umask 077',
             'uid=$(id -u)', '[ "$uid" != 0 ] || exit 0',
             'runtime=${XDG_RUNTIME_DIR-}',
             'if [ -z "$runtime" ] || [ ! -d "$runtime" ] || [ ! -O "$runtime" ]; then',
             '    echo "PipeWire: a user-owned XDG_RUNTIME_DIR is required" >&2',
             '    exit 1', 'fi']
    lines.extend('export ' + key + '=' + shlex.quote(value) for key, value in policy['environment'].items())
    lines.extend([
        'exec 9>"$runtime"/' + shlex.quote(policy['lock_file']),
        shlex.join(policy['lock_command']) + ' 9 || exit 0',
        'children=""',
        'cleanup() {',
        '    trap - EXIT INT TERM',
        '    for pid in $children; do kill "$pid" 2>/dev/null || true; done',
        '    wait || true', '}',
        'trap cleanup EXIT', 'trap "exit 0" INT TERM',
    ])
    ready = []
    for arg in policy['readiness_command']:
        if '@@RUNTIME@@' in arg:
            before, after = arg.split('@@RUNTIME@@')
            ready.append(shlex.quote(before) + '"$runtime"' + shlex.quote(after))
        else:
            ready.append(shlex.quote(arg))
    for index, command in enumerate(policy['processes']):
        # Keep the singleton lock in the session supervisor, never its children.
        lines.extend([shlex.join(command) + ' 9>&- &',
                      'child' + str(index) + '=$!',
                      'children="$children $child' + str(index) + '"'])
        if index == 0:
            lines.extend(['attempt=0', 'while ! ' + ' '.join(ready) + '; do',
                          '    kill -0 "$child0" 2>/dev/null || exit 1',
                          '    attempt=$((attempt + 1))',
                          '    [ "$attempt" -lt ' + str(policy['readiness_attempts']) + ' ] || exit 1',
                          '    sleep ' + shlex.quote(policy['readiness_interval']), 'done'])
    lines.append('wait "$child0"')
    return '\n'.join(lines) + '\n'


def configure_session_audio(root, runtime, init_system):
    policy = runtime['customization']['session_audio']
    active = policy['enabled'] and init_system in policy['init_systems'] and all((root / path).is_file() for path in policy['required_binaries'])
    if not active:
        for relative in (policy['launcher'], policy['autostart_path']):
            destination = rootfs_destination(root, relative)
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
        return
    outputs = {
        policy['launcher']: (audio_launcher(policy), 0o755),
        policy['autostart_path']: ('[Desktop Entry]\n' + ''.join(f'{key}={value.replace("@@LAUNCHER@@", "/" + policy["launcher"])}\n' for key, value in policy['autostart'].items()), 0o644),
    }
    for relative, (content, mode) in outputs.items():
        destination = rootfs_destination(root, relative)
        if destination.is_symlink():
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        destination.chmod(mode)
