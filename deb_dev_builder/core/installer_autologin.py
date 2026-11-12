"""An optional native debconf question and target autologin for d-i desktops."""
import shlex

from .initrd_editor import Entry, archive_path
from .installer_lifecycle import target_header, rooted
from .desktop_sessions import session_candidates


INI_FUNCTION = r'''
set_ini() {
    file=$1 section=$2 key=$3 value=$4
    mkdir -p "$(dirname "$file")"
    [ -f "$file" ] || : > "$file"
    pending=$(mktemp "$file.XXXXXX")
    awk -v section="$section" -v key="$key" -v value="$value" '
        BEGIN { inside=0; seen=0; section_seen=0 }
        /^\[/ {
            if (inside && !seen) { print key "=" value; seen=1 }
            inside=($0 == "[" section "]")
            if (inside) section_seen=1
        }
        {
            if (inside && $0 ~ "^[ \t]*" key "[ \t]*=") {
                if (!seen) print key "=" value
                seen=1
                next
            }
            print
        }
        END {
            if (!section_seen) print "[" section "]"
            if (!seen) print key "=" value
        }
    ' "$file" > "$pending"
    chmod 0644 "$pending"
    mv -f -- "$pending" "$file"
}
'''


def autologin_entries(config, runtime):
    policy = runtime["installer"]["autologin"]
    manager = config.get("display_manager")
    manager = runtime["desktop"]["display_manager_aliases"].get(manager, manager)
    if not policy["enabled"] or not config.get("desktop") or manager not in policy["managers"]:
        return []
    spec = policy["managers"][manager]
    for path in (policy["template_path"], policy["startup_hook"], policy["finish_hook"], spec["path"]):
        archive_path(path)
    question = policy["question"]
    template = (
        f"Template: {question}\nType: boolean\nDefault: {str(policy['default']).lower()}\n"
        f"Description: {policy['description']}\n {policy['details']}\n\n"
    )
    load = shlex.join([*policy["load_template_command"], policy["owner"], "/" + policy["template_path"]])
    startup = "#!/bin/sh\nset -eu\n" + load + "\n"
    lines = [*target_header(runtime["installer"]["target_root"], 10), load,
             ". /usr/share/debconf/confmodule"]
    # Only ask when the selected manager actually exists in the installed OS.
    binaries = runtime["desktop"]["display_managers"][manager]["binaries"]
    lines.extend(["available=false", *["[ ! -x " + rooted(binary) + ' ] || available=true' for binary in binaries],
                  '[ "$available" = true ] || exit 0',
                  "db_get " + shlex.quote(policy["make_user_question"]), '[ "$RET" = true ] || exit 0',
                  "db_get " + shlex.quote(policy["user_question"]), 'username="$RET"',
                  'case "$username" in ""|root) exit 0 ;; *[!a-zA-Z0-9_-]*) exit 10 ;; esac'])
    if policy["ask"]:
        lines.extend(["db_input " + shlex.join([policy["priority"], question]) + " || true", "db_go || exit 10"])
    lines.extend(["db_get " + shlex.quote(question), 'autologin="$RET"', 'session=""', INI_FUNCTION])
    def ini_commands(sections):
        commands = []
        for section, values in sections.items():
            for key, value in values.items():
                value = shlex.quote(value).replace("{user}", "'\"$username\"'").replace("{session}", "'\"$session\"'")
                commands.append("set_ini " + rooted(spec["path"]) + " " + shlex.join([section, key]) + " " + value)
        return commands
    lines.extend(['if [ "$autologin" != true ]; then', *ini_commands(spec["disable_sections"]), 'exit 0', 'fi',
                  'entry=$(chroot "$target" getent passwd "$username")',
                  'uid=$(printf "%s\\n" "$entry" | cut -d: -f3)',
                  '[ "$uid" -ge ' + str(policy["minimum_uid"]) + ' ] || exit 10'])
    directories = runtime["desktop"]["session_directories"]
    for preferred in session_candidates(config, runtime):
        for directory in directories:
            lines.append('[ -n "$session" ] || [ ! -f ' + rooted(directory + "/" + preferred + ".desktop") + ' ] || session=' + shlex.quote(preferred))
    for directory in directories:
        lines.extend(['if [ -z "$session" ]; then', '    for candidate in ' + rooted(directory) + '/*.desktop; do',
                      '        [ -f "$candidate" ] || continue', '        session=${candidate##*/}; session=${session%.desktop}; break',
                      "    done", "fi"])
    lines.extend(['case "$session" in ""|*[!a-zA-Z0-9_.-]*) exit 10 ;; esac', *ini_commands(spec["sections"])])
    return [Entry(policy["template_path"], template.encode(), 0o100644),
            Entry(policy["startup_hook"], startup.encode(), 0o100755),
            Entry(policy["finish_hook"], ("\n".join(lines) + "\n").encode(), 0o100755)]
