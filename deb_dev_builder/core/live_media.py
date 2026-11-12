"""Bridge the native live-tools eject prompt to an active graphical splash."""
import shlex


def prompt_helper(runtime, *, locale=None):
    policy = runtime['customization']['live_media']
    client = shlex.join(policy['client'])
    timed = shlex.join(policy['timeout_command'])
    bounded = timed + ' ' + client
    message = runtime['customization']['plymouth']['visual']['media_removal_message']
    disabled = '|'.join(shlex.quote(option) for option in policy['disabled_options'])
    retained = ' '.join('"$vt"' if arg == '{vt}' else shlex.quote(arg) for arg in policy['retained_switch_command'])
    prefix, suffix = policy['terminal_path'].split('{vt}')
    terminal = shlex.quote(prefix) + '"$vt"' + shlex.quote(suffix)
    start = timed + ' ' + shlex.join(policy['start_command'])
    if policy['shutdown_kernel_options']:
        options = ' ' + ' '.join(policy['shutdown_kernel_options'])
        start += ' --kernel-command-line "$kernel_line"' + shlex.quote(options)
    lines = ['#!/bin/sh', 'set -eu',
             'kernel_line=$(cat ' + shlex.quote(policy['cmdline']) + ')',
             'for option in $kernel_line; do',
             '    case "$option" in ' + disabled + ') exit 1 ;; esac', 'done',
             'activate_splash() {',
             '    ' + timed + ' ' + shlex.join(policy['reactivate_command']) + ' || return 1',
             '    ' + bounded + ' show-splash || return 1',
             '    if ! ' + timed + ' ' + shlex.join(policy['active_vt_command']) + '; then',
             '        ' + timed + ' ' + shlex.join(policy['switch_vt_command']) + ' || return 1',
             '        ' + timed + ' ' + shlex.join(policy['active_vt_command']) + ' || return 1',
             '    fi', '}',
             'case "${1-}" in',
             '    snapshot)',
             '        ' + bounded + ' --ping && activate_splash || exit 1',
             '        ' + bounded + ' display-message --text ' + shlex.quote(message) + ' || exit 1',
             '        vt=$(' + timed + ' ' + shlex.join(policy['active_console_command']) + ') || exit 1',
             '        case "$vt" in ""|*[!0-9]*) exit 1 ;; esac',
             '        printf %s "$vt" > ' + shlex.quote(policy['state_file']),
             '        exit 0 ;;',
             '    retained-console)',
             '        ' + shlex.join(policy['keyboard_command']),
             '        ' + shlex.join(policy['terminal_settings_command']) + ' || true',
             '        IFS= read -r answer',
             '        exit 0 ;;',
             '    prepare)',
             '        [ ! -f ' + shlex.quote(policy['state_file']) + ' ] || exit 0',

             '        if ! ' + bounded + ' --ping 2>/dev/null; then',
             # Native Plymouth retains DRM master in its fd-escrow process after
             # SIGTERM. Release only that executable before replacing the daemon.
             '            ' + timed + ' ' + shlex.join(policy['release_renderer_command']) + ' || exit 1',
             '            ' + start + ' || exit 1',
             '        fi',
             '        activate_splash || exit 1',
             # Render the notice while the live medium is still available.
             # The native caller schedules ejection only after prepare returns.
             '        ' + bounded + ' display-message --text ' + shlex.quote(message) + ' || exit 1',
             '        ;;',
             '    wait)',
             '        if [ -f ' + shlex.quote(policy['state_file']) + ' ]; then',
             '            vt=$(cat ' + shlex.quote(policy['state_file']) + ')',
             '            case "$vt" in ""|*[!0-9]*) exit 1 ;; esac',
             '            ' + timed + ' ' + retained + ' || exit 1',
             '            terminal=' + terminal,
             '            exec ' + shlex.quote('/' + policy['helper']) + ' retained-console <"$terminal" >"$terminal"',
             '        fi',
             '        if ' + bounded + ' --ping && activate_splash && ' + bounded + ' display-message --text ' + shlex.quote(message) + '; then',
             '            if ' + client + ' watch-keystroke --keys ' + shlex.quote(policy['confirm_keys']) + ' >/dev/null; then',
             '                ' + bounded + ' hide-message --text ' + shlex.quote(message) + ' || true',
             '                exit 0', '            fi', '        fi',
             # Release the graphics VT before native live-tools prints its
             # console fallback. Never leave a retained splash hiding a read.
             '        ' + bounded + ' quit 2>/dev/null || true',
             '        ' + timed + ' ' + shlex.join(policy['switch_vt_command']) + ' 2>/dev/null || true',
             '        exit 1',
             '        ;;', '    *) exit 64 ;;', 'esac']
    return '\n'.join(lines) + '\n'


def configure_live_media(root, runtime, *, locale=None):
    policy = runtime['customization']['live_media']
    source = root / policy['native_script']
    if not policy['enabled'] or not source.is_file():
        return
    content = source.read_text()
    helper = '/' + policy['helper']
    old_prepare = (policy['marker'] + '\nif [ "$LIVE_MEDIUM_EJECT_VERBOSE" = true ]; then\n    '
                   + shlex.quote(helper) + ' prepare || true\nfi\n\n')
    stage = policy['stage_environment']
    prepare = (policy['marker'] + '\nif [ "$LIVE_MEDIUM_EJECT_VERBOSE" = true ]; then\n    '
               + shlex.quote(helper) + ' "${' + stage + ':-prepare}" || true\nfi\n'
               + '[ "${' + stage + ':-prepare}" != snapshot ] || exit 0\n\n')
    if policy['marker'] not in content:
        if content.count(policy['eject_anchor']) != 1 or content.count(policy['prompt_block']) != 1:
            raise ValueError('Unsupported live-tools eject script; refusing to change its device/ejection logic')
        content = content.replace(policy['eject_anchor'], prepare + policy['eject_anchor'], 1)
        content = content.replace(policy['prompt_block'], '\t\tif ' + shlex.quote(helper) + ' wait\n\t\tthen\n\t\t\t:', 1)
    elif old_prepare in content:
        content = content.replace(old_prepare, prepare, 1)
    elif prepare not in content:
        raise ValueError('Unsupported patched live-tools eject script')
    source.write_text(content)
    if policy['presentation'] == 'retained' and (root / 'usr/lib/systemd/system').is_dir():
        unit = root / policy['service_path']
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(policy['service_content'])
        for target in policy['service_targets']:
            link = unit.parent / (target + '.wants') / unit.name
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                link.symlink_to('../' + unit.name)
    destination = root / policy['helper']
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(prompt_helper(runtime, locale=locale))
    destination.chmod(0o755)
