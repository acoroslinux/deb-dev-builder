"""Expose the already mounted installer medium to direct target APT calls."""
from pathlib import Path
import shlex
import re

from .initrd_editor import archive_path


def target_cdrom_hooks(target, policy):
    if type(policy["enabled"]) is not bool:
        raise ValueError("Target CD-ROM enablement must be boolean")
    for key in ("mount", "config_file", "state_directory", "release_hook"):
        if archive_path(policy[key]) == "." or not re.fullmatch(r"[A-Za-z0-9_./-]+", policy[key]):
            raise ValueError("Target CD-ROM paths must be nonempty paths without whitespace")
    for key in ("source", "mounts_file"):
        path = Path(policy[key])
        if not path.is_absolute() or path == Path("/") or ".." in path.parts:
            raise ValueError("Installer CD-ROM source and mount table must be absolute paths")
    if not target or not Path(target).is_absolute() or Path(target) == Path("/") or ".." in Path(target).parts:
        raise ValueError("Target CD-ROM requires an explicit target root")
    if not policy["enabled"]:
        return "", ""
    values = {"media_source": policy["source"], "media_mounts": policy["mounts_file"],
              "media_target": str(Path(target) / policy["mount"]),
              "media_config": str(Path(target) / policy["config_file"]),
              "media_state": str(Path(target) / policy["state_directory"])}
    variables = "\n".join(name + "=" + shlex.quote(value) for name, value in values.items())
    mounted = '''
media_is_mounted() {
    while read -r device mount_path remainder; do
        [ "$(printf '%b' "$mount_path")" != "$media_target" ] || return 0
    done < "$media_mounts"
    return 1
}
'''
    # The installer continues to own the physical medium. APT sees the same
    # filesystem in the target and must not eject/remount it independently.
    config = '\n'.join([
        'Acquire::cdrom::mount "/' + policy["mount"] + '/";',
        'Acquire::cdrom::AutoDetect "false";',
        'APT::CDROM::NoMount "true";', "",
    ])
    prepare = variables + mounted + '''
test -r "$media_source/.disk/info"
test ! -L "$media_target"
test ! -L "$media_state"
test ! -L "$media_config"
mkdir -p "$media_state" "$(dirname "$media_config")"
chmod 0700 "$media_state"
rm -f -- "$media_state/.ready"
if [ ! -f "$media_state/.saved" ]; then
    if [ -e "$media_config" ]; then
        test -f "$media_config"
        cp -p -- "$media_config" "$media_state/config.original"
    fi
    touch "$media_state/.saved"
fi
if media_is_mounted; then
    test -f "$media_state/.mounted"
    cmp -s "$media_source/.disk/info" "$media_target/.disk/info"
else
    if [ ! -d "$media_target" ]; then
        mkdir -p "$media_target"
        touch "$media_state/.created"
    fi
    mount --bind "$media_source" "$media_target"
    touch "$media_state/.mounted"
fi
''' + "printf '%s' " + shlex.quote(config) + ''' > "$media_config"
chmod 0644 "$media_config"
touch "$media_state/.ready"
'''
    finish = variables + mounted + '''
if [ -f "$media_state/.saved" ]; then
    if media_is_mounted; then
        test -f "$media_state/.mounted"
        umount "$media_target"
    fi
    if [ -f "$media_state/config.original" ]; then
        mv -f -- "$media_state/config.original" "$media_config"
    else
        rm -f -- "$media_config"
    fi
    if [ -f "$media_state/.created" ]; then
        rmdir "$media_target"
    fi
    rm -f -- "$media_state/.ready" "$media_state/.mounted" "$media_state/.created" "$media_state/.saved"
    rmdir "$media_state"
fi
'''
    return prepare, finish
