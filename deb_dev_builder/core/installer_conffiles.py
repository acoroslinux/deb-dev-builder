"""Temporarily preserve selected ucf-managed files during live d-i installs."""
from pathlib import Path
import shlex

from .initrd_editor import archive_path


def ucf_preservation_hooks(target, policy):
    if not target or not Path(target).is_absolute() or Path(target) == Path("/") or ".." in Path(target).parts:
        raise ValueError("Installer ucf policy requires an explicit target root")
    for key in ("config_file", "backup_directory", "restored_marker"):
        if archive_path(policy[key]) == ".":
            raise ValueError("Installer ucf paths must not select the root")
    files = policy["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("Installer ucf preservation needs explicit absolute file paths")
    for name in files:
        if not isinstance(name, str) or not name.startswith("/") or archive_path(name[1:]) == ".":
            raise ValueError("Installer ucf preservation needs explicit absolute file paths")
    config = Path(target) / policy["config_file"]
    backup = Path(target) / policy["backup_directory"]
    restored = Path(target) / policy["restored_marker"]
    if config == backup or backup in config.parents or config in backup.parents:
        raise ValueError("Installer ucf backup must be separate from its configuration")
    if restored == config or restored == backup or backup in restored.parents:
        raise ValueError("Installer ucf restored marker must be outside the backup and configuration")
    variables = "\n".join([
        "ucf_config=" + shlex.quote(str(config)),
        "ucf_backup=" + shlex.quote(str(backup)),
        "ucf_restored=" + shlex.quote(str(restored)),
    ])
    # ucf.conf is sourced after native ucf resolves dest_file. Override only
    # these destinations, including an inherited force-new setting, in this
    # invocation. Other package files retain their normal ucf policy.
    snippet = "\n".join([
        "", "# Temporary live-installer policy; removed before completion.",
        'case "${dest_file-}" in',
        "    " + "|".join(shlex.quote(name) for name in files) + ")",
        "        unset conf_force_conffnew UCF_FORCE_CONFFNEW",
        "        conf_force_conffold=YES",
        "        UCF_FORCE_CONFFOLD=1",
        "        DPKG_FORCE=confold",
        "        ;;", "esac", "",
    ])
    prepare = variables + '''
test ! -L "$ucf_config"
test ! -L "$ucf_backup"
test ! -e "$ucf_config" || test -f "$ucf_config"
mkdir -p "$ucf_backup" "$(dirname "$ucf_config")"
chmod 0700 "$ucf_backup"
rm -f -- "$ucf_restored" "$ucf_backup/.ready"
if [ ! -f "$ucf_backup/.saved" ]; then
    if [ -f "$ucf_config" ]; then
        cp -p -- "$ucf_config" "$ucf_backup/original"
    fi
    touch "$ucf_backup/.saved"
fi
if [ -f "$ucf_backup/original" ]; then
    cp -p -- "$ucf_backup/original" "$ucf_config"
else
    : > "$ucf_config"
    chmod 0644 "$ucf_config"
fi
''' + "printf '%s' " + shlex.quote(snippet) + ''' >> "$ucf_config"
touch "$ucf_backup/.ready"
'''
    finish = variables + '''
if [ -f "$ucf_backup/.saved" ]; then
    if [ -f "$ucf_backup/original" ]; then
        mv -f -- "$ucf_backup/original" "$ucf_config"
    else
        rm -f -- "$ucf_config"
    fi
    rm -f -- "$ucf_backup/.ready" "$ucf_backup/.saved"
    rmdir "$ucf_backup"
    mkdir -p "$(dirname "$ucf_restored")"
    touch "$ucf_restored"
fi
'''
    return prepare, finish
