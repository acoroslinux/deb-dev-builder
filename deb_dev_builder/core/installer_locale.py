"""Apply d-i's selected locale after copying an already configured live root."""
import shlex

from .initrd_editor import Entry, archive_path
from .installer_lifecycle import rooted, target_header


def locale_entries(config, runtime):
    policy = runtime["installer"]["locale"]
    if not policy["enabled"]:
        return []
    archive_path(policy["finish_hook"])
    lines = [*target_header(runtime["installer"]["target_root"], 10),
             '. /usr/share/debconf/confmodule',
             'db_get ' + shlex.quote(policy['locale_question']), 'selected="$RET"',
             '[ -n "$selected" ] || exit 0',
             'case "$selected" in *[!a-zA-Z0-9_@.+-]*) exit 10 ;; esac',
             'db_get ' + shlex.quote(policy['language_question']) + ' || RET=""',
             'language="$RET"',
             'case "$language" in *[!a-zA-Z0-9_@.:+-]*) exit 10 ;; esac',
             'case "$selected" in',
             '    ' + '|'.join(shlex.quote(value) for value in policy['builtin_locales']) + ') language=""; builtin=true ;;',
             '    *)',
             '        builtin=false',
             '        supported=$(awk -v wanted="$selected" \'$1 == wanted { print; exit }\' ' + rooted(policy['supported_file']) + ')',
             '        [ -n "$supported" ] || { echo "Unsupported selected locale: $selected" >&2; exit 10; }',
             '        generation=' + rooted(policy['generation_file']),
             '        mkdir -p "$(dirname "$generation")"',
             '        touch "$generation"',
             '        if ! awk -v wanted="$selected" \'$1 == wanted { found=1 } END { exit !found }\' "$generation"; then',
             r'''            printf '%s\n' "$supported" >> "$generation"''',
             '        fi',
             '        LC_ALL=C chroot "$target" ' + shlex.join(policy['generate_command']),
             '        [ -n "$language" ] || language="${selected%%.*}"',
             '        ;;',
             'esac']
    for path in policy['files']:
        lines.extend(['mkdir -p "$(dirname ' + rooted(path) + ')"',
                      # Native minimal C installs need not include locales or update-locale.
                      'if [ "$builtin" = true ]; then',
                      r'''    printf 'LANG=%s\nLANGUAGE=%s\n' "$selected" "$language" > ''' + rooted(path),
                      'else',
                      '    LC_ALL=C chroot "$target" ' + shlex.join([*policy['update_command'], '--locale-file', '/' + path]) + ' "LANG=$selected" "LANGUAGE=$language"',
                      'fi'])
    lines.extend(['mkdir -p "$(dirname ' + rooted(policy['log_path']) + ')"',
                  r'''printf 'LANG=%s\nLANGUAGE=%s\n' "$selected" "$language" > ''' + rooted(policy['log_path'])])
    return [Entry(policy['finish_hook'], ('\n'.join(lines) + '\n').encode(), 0o100755)]
