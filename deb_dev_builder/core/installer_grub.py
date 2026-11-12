"""Shared, JSON-driven installed GRUB defaults and Plymouth activation."""
import json
import shlex

from .initrd_editor import Entry
from .installer_lifecycle import target_header, rooted, target_sysfs_scope


def shell_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False).replace('$', r'\$').replace('`', r'\`')


def grub_lines(runtime):
    policy = runtime['customization']['grub']
    plymouth = runtime['customization']['plymouth']
    lines = ['defaults=' + rooted(policy['defaults_file']),
             'mkdir -p "$(dirname "$defaults")"',
             'touch "$defaults"',
             # This file is a native shell configuration, sourced by GRUB too.
             'parameters=$( (. "$defaults"; printf "%s" "${' + policy['kernel_parameters_key'] + '-}") )',
             'set_default() {',
             '    pending=$(mktemp "$defaults.XXXXXX")',
             '    awk -v key="$1" \'$0 !~ "^[ \\t]*" key "[ \\t]*=" { print }\' "$defaults" > "$pending"',
             r'''    printf '%s=%s\n' "$1" "$2" >> "$pending"''',
             '    chmod 0644 "$pending"',
             '    mv -f -- "$pending" "$defaults"',
             '}',
             'append_parameter() { case " $parameters " in *" $1 "*) ;; *) parameters="${parameters:+$parameters }$1" ;; esac; }']
    for value in policy['kernel_parameters']:
        lines.append('append_parameter ' + shlex.quote(value))
    lines.extend(['if [ -x ' + rooted(plymouth['binary']) + ' ] && [ -f ' + rooted(plymouth['directory'] + '/' + plymouth['name'] + '.plymouth') + ' ]; then',
                  *['    append_parameter ' + shlex.quote(value) for value in policy['plymouth_parameters']],
                  'fi'])
    # Preserve the literal argument string when serializing it as shell syntax.
    lines.extend([r'''escaped=$(printf '%s' "$parameters" | sed "s/'/'\\\\''/g")''',
                  'set_default ' + shlex.quote(policy['kernel_parameters_key']) + ' "\'$escaped\'"'])
    for key, value in policy['settings'].items():
        # Kernel/crypto choices from the installer are not static defaults.
        if key == 'GRUB_CMDLINE_LINUX':
            lines.append('grep -Eq ' + shlex.quote(r'^[ \t]*' + key + r'[ \t]*=') + ' "$defaults" || set_default ' + shlex.join([key, shell_value(value)]))
        else:
            lines.append('set_default ' + shlex.join([key, shell_value(value)]))
    lines.extend(['if [ -f ' + rooted(policy['theme_file']) + ' ]; then',
                  '    set_default ' + shlex.join([policy['theme_key'], shell_value('/' + policy['theme_file'])]), 'fi'])
    header = '\n'.join('# ' + line for line in policy['header']) + '\n\n'
    lines.extend(['if ! grep -Fqx -- ' + shlex.quote('# ' + policy['header'][0]) + ' "$defaults"; then',
                  '    pending=$(mktemp "$defaults.XXXXXX")',
                  "    printf '%s' " + shlex.quote(header) + ' > "$pending"',
                  '    cat "$defaults" >> "$pending"', '    chmod 0644 "$pending"',
                  '    mv -f -- "$pending" "$defaults"', 'fi'])
    return lines


def grub_entries(config, runtime, *, regenerate=True):
    policy = runtime['customization']['grub']
    plymouth = runtime['customization']['plymouth']
    lines = [*grub_lines(runtime),
             'if [ -x ' + rooted(plymouth['binary']) + ' ] && [ -x ' + rooted(plymouth['selector']) + ' ] && [ -f ' + rooted(plymouth['directory'] + '/' + plymouth['name'] + '.plymouth') + ' ]; then',
             '    chroot "$target" ' + shlex.join([*plymouth['select_command'], plymouth['name']]), 'fi']
    if regenerate:
        lines.append('chroot "$target" ' + shlex.join(runtime['build']['installer_storage']['initramfs_update']))
    lines.append('chroot "$target" ' + shlex.join(policy['update_command']))
    script = [*target_header(runtime['installer']['target_root'], 10), *target_sysfs_scope(lines)]
    return [Entry(policy['installer_hook'], ('\n'.join(script) + '\n').encode(), 0o100755)]
