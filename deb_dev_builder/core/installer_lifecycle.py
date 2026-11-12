"""Shared target preparation and identity handling for disk installers."""
import shlex
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .initrd_editor import archive_path


def materialize_boot_hardlinks(source_root):
    """Keep live copying valid when /boot is a separate target filesystem.

    BusyBox tar cannot restore hardlinks across filesystems. A failed deferred
    hardlink also prevents later deferred symlinks from being restored.
    SquashFS can still deduplicate the contents of the separate regular files.
    """
    boot = Path(source_root) / 'boot'
    if boot.is_symlink():
        raise ValueError('Live installer boot directory must not be a symlink')
    count = 0
    for path in boot.rglob('*'):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 2:
            continue
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.deb-dev-boot-', delete=False) as stream:
                pending = Path(stream.name)
            shutil.copyfile(path, pending)
            os.chown(pending, metadata.st_uid, metadata.st_gid)
            shutil.copystat(path, pending)
            os.utime(pending, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            pending.replace(path)
            count += 1
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    return count


def rooted(path):
    if archive_path(path) == ".":
        raise ValueError("Installer file path must not select the target root")
    return '"$target"/' + shlex.quote(path)


def target_header(target=None, failure_code=None):
    lines = ["#!/bin/sh", "set -eu"]
    if failure_code is not None:
        lines.append(f"trap 'code=$?; trap - 0; if [ \"$code\" -ne 0 ]; then exit {int(failure_code)}; fi' 0")
    if target is not None:
        argument = shlex.quote(target)
    else:
        lines.extend([
            'target_arg=${1:-}',
            'case "$target_arg" in ""|@@ROOT@@) target_arg=${2:-} ;; esac',
            'case "$target_arg" in @@ROOT@@) target_arg= ;; esac',
            '[ -n "$target_arg" ] || { echo "Missing target root" >&2; exit 2; }',
        ])
        argument = '"$target_arg"'
    lines.extend(['target=$(readlink -f -- ' + argument + ')',
                  'test -n "$target" && test "$target" != / && test -d "$target/etc"'])
    return lines


def target_sysfs_scope(body):
    """Provide sysfs for chroot GRUB/device discovery and release only our mount.

    The subshell keeps the caller's exit-status mapping and cleanup traps intact.
    Native installer versions differ in which pseudo-filesystems remain mounted
    during finish-install, so the hook cannot assume /target/sys is available.
    """
    return [
        '(',
        '    mounted_target_sysfs=false',
        '''    trap 'status=$?; trap - 0; if [ "$mounted_target_sysfs" = true ]; then umount "$target/sys" || { [ "$status" -ne 0 ] || status=1; }; fi; exit "$status"' 0''',
        '    if [ ! -d "$target/sys/dev/block" ]; then',
        '        mkdir -p "$target/sys"',
        '        mount -o bind /sys "$target/sys"',
        '        mounted_target_sysfs=true',
        '        test -d "$target/sys/dev/block"',
        '    fi',
        *body,
        ')',
    ]


def live_cleanup_paths(runtime):
    paths = [*runtime["installer"]["live_remove_paths"], runtime["users"]["sudoers_path"]]
    paths.extend(path for manager in runtime["desktop"]["display_managers"].values() for path in manager.get("files", {}))
    paths.append(runtime["desktop"]["console_autologin"]["path"])
    paths.append(runtime['customization']['live_media']['helper'])
    media = runtime['customization']['live_media']
    paths.append(media['service_path'])
    unit = Path(media['service_path'])
    paths.extend(str(unit.parent / (target + '.wants') / unit.name) for target in media['service_targets'])
    return list(dict.fromkeys(paths))


def identity_lines(policy):
    """Keep a bootable placeholder; initialize installed systemd identities."""
    machine = rooted(policy["machine_id"])
    dbus = rooted(policy["dbus_id"])
    mask = rooted(policy["commit_mask"])
    lines = [
        'mkdir -p "$(dirname ' + machine + ')"',
        f'if [ -L {machine} ]; then rm -f -- {machine}; fi',
        f'if [ ! -e {machine} ]; then : > {machine}; fi',
        f'if [ -L {mask} ] && [ "$(readlink -- {mask})" = /dev/null ]; then rm -f -- {mask}; fi',
        f'if [ -x {rooted(policy["setup_binary"])} ]; then',
        f'    identity=$(cat {machine})',
        "    if ! printf '%s\\n' \"$identity\" | grep -Eq '^[0-9a-f]{32}$' || [ \"$identity\" = 00000000000000000000000000000000 ]; then",
        f'        : > {machine}',
        '        chroot "$target" ' + shlex.join(policy["setup_command"]),
        "    fi",
        f'    identity=$(cat {machine})',
        "    printf '%s\\n' \"$identity\" | grep -Eq '^[0-9a-f]{32}$'",
        '    test "$identity" != 00000000000000000000000000000000',
        '    mkdir -p "$(dirname ' + dbus + ')"',
        f'    rm -f -- {dbus}',
        '    ln -s -- ' + shlex.quote(policy["dbus_target"]) + ' ' + dbus,
        "fi",
        f'if [ -x {rooted(policy["ssh_server_binary"])} ]; then',
        '    chroot "$target" ' + shlex.join(policy["ssh_key_setup"]),
        'fi',
    ]
    return lines


def prepare_target_script(runtime):
    lines = target_header()
    paths = [*live_cleanup_paths(runtime), runtime["customization"]["offline_repository"]["source_file"]]
    lines.append("rm -f -- " + " ".join(rooted(path) for path in paths))
    conf = runtime["build"]["installer_storage"]["cryptsetup_config"]
    lines.extend(['mkdir -p "$(dirname ' + rooted(conf["path"]) + ')"',
                  r"printf '\n%s\n' " + shlex.quote(conf["line"]) + ' >> ' + rooted(conf["path"])])
    return ("\n".join(lines) + "\n").encode()


def copied_target_layout_script(source_root, target, policy):
    """Restore source usr-merge aliases left as empty installer directories.

    Never merge or delete nonempty directories: an unexpected layout must stop
    validation instead of silently discarding installed files.
    """
    lines = target_header(target)
    marker = rooted(policy['layout_marker'])
    lines.append('rm -f -- ' + marker)
    for alias in policy['merged_usr_aliases']:
        if '/' in alias or archive_path(alias) == '.':
            raise ValueError('Merged usr aliases must be root-level names')
        source = Path(source_root) / alias
        if not source.is_symlink():
            continue
        link = str(source.readlink())
        archive_path(link)
        destination = rooted(alias)
        lines.extend([
            'if [ -d ' + destination + ' ] && [ ! -L ' + destination + ' ]; then',
            '    rmdir -- ' + destination + ' || { echo ' + shlex.quote('Cannot restore merged usr alias over nonempty directory: /' + alias) + ' >&2; exit 1; }',
            'fi',
            'if [ -L ' + destination + ' ]; then',
            '    [ "$(readlink -- ' + destination + ')" = ' + shlex.quote(link) + ' ] || { echo "Conflicting merged usr alias" >&2; exit 1; }',
            'elif [ ! -e ' + destination + ' ]; then',
            '    ln -s -- ' + shlex.quote(link) + ' ' + destination,
            'else',
            '    echo "Unexpected file at merged usr alias" >&2; exit 1',
            'fi',
        ])
    lines.extend([
        'chroot "$target" /usr/bin/true',
        'mkdir -p "$(dirname ' + marker + ')"',
        'touch -- ' + marker,
    ])
    return ('\n'.join(lines) + '\n').encode()
