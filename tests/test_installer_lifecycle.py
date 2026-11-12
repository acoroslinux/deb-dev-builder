import os
from pathlib import Path
import subprocess
import pytest
import shutil
import shlex

from deb_dev_builder.core.chroot_cleaner import ChrootCleaner
from deb_dev_builder.core.installer_lifecycle import prepare_target_script, target_header, identity_lines, copied_target_layout_script, target_sysfs_scope
from deb_dev_builder.core.runtime_config import load_runtime


@pytest.mark.parametrize('existing,failure', [
    (False, ''), (False, 'body'), (False, 'mount'), (False, 'umount'),
    (True, ''), (True, 'body'),
])
def test_installer_sysfs_scope_preserves_caller_mounts_and_failure_status(tmp_path, existing, failure):
    root = tmp_path / 'installed system'
    (root / 'etc').mkdir(parents=True)
    if existing:
        (root / 'sys/dev/block').mkdir(parents=True)
    tools = tmp_path / 'bin'
    tools.mkdir()
    scripts = {
        'mount': '''printf '%s\\n' "mount $*" >> "$EVENTS"
[ "$FAILURE" != mount ] || exit 1
test "$1" = -o && test "$2" = bind && test "$3" = /sys
mkdir -p "$4/dev/block"
''',
        'umount': '''printf '%s\\n' "umount $*" >> "$EVENTS"
[ "$FAILURE" != umount ] || exit 1
rmdir "$1/dev/block"
''',
    }
    for name, body in scripts.items():
        path = tools / name
        path.write_text('#!/bin/sh\nset -eu\n' + body)
        path.chmod(0o755)
    body = ['test -d "$target/sys/dev/block"', 'exit ' + ('3' if failure == 'body' else '0')]
    script = '\n'.join([*target_header(str(root), 10), *target_sysfs_scope(body)])
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'EVENTS': str(tmp_path / 'events'), 'FAILURE': failure}
    result = subprocess.run(['sh', '-e'], input=script, text=True, env=env, capture_output=True)
    assert result.returncode == (10 if failure else 0), result.stderr
    events = (tmp_path / 'events').read_text().splitlines() if (tmp_path / 'events').exists() else []
    if existing:
        assert not events
        assert (root / 'sys/dev/block').is_dir()
    else:
        assert events[0].startswith('mount -o bind /sys ')
        assert len(events) == (1 if failure == 'mount' else 2)
        if failure not in ('mount', 'umount'):
            assert events[1].startswith('umount ')
            assert not (root / 'sys/dev/block').exists()


@pytest.mark.parametrize('state', ['empty-directory', 'correct-link', 'nonempty-directory', 'conflicting-link'])
def test_copied_merged_usr_loader_layout_is_restored_without_discarding_files(tmp_path, state):
    policy = load_runtime()['installer']
    source = tmp_path / 'source'
    target = tmp_path / 'installed system'
    source.mkdir()
    (source / 'lib64').symlink_to('usr/lib64')
    (target / 'etc').mkdir(parents=True)
    (target / 'usr/lib64').mkdir(parents=True)
    (target / 'usr/lib64/ld-linux-x86-64.so.2').write_text('loader fixture')
    alias = target / 'lib64'
    if state.endswith('directory'):
        alias.mkdir()
        if state == 'nonempty-directory':
            (alias / 'keep').write_text('must survive')
    else:
        alias.symlink_to('usr/lib64' if state == 'correct-link' else 'other')
    tools = tmp_path / 'bin'
    tools.mkdir()
    chroot = tools / 'chroot'
    chroot.write_text('#!/bin/sh\ntest "$2" = /usr/bin/true && test -f "$1/lib64/ld-linux-x86-64.so.2"\n')
    chroot.chmod(0o755)
    script = copied_target_layout_script(source, str(target), policy)
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']}
    success = state in ('empty-directory', 'correct-link')
    for _ in range(2):
        result = subprocess.run(['sh', '-e'], input=script, env=env, capture_output=True)
        assert (result.returncode == 0) == success, result.stderr
        assert (target / policy['layout_marker']).exists() == success
    if success:
        assert alias.is_symlink() and alias.readlink() == Path('usr/lib64')
    elif state == 'nonempty-directory':
        assert (alias / 'keep').read_text() == 'must survive'
    else:
        assert alias.readlink() == Path('other')


def test_cleaner_preserves_a_regular_empty_machine_id_after_customization(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/machine-id").write_text("a" * 32 + "\n")
    ChrootCleaner(tmp_path, {}).clean()
    machine = tmp_path / "etc/machine-id"
    assert machine.is_file() and not machine.is_symlink()
    assert machine.read_bytes() == b""
    assert (tmp_path / "var/lib/dbus/machine-id").readlink() == Path("/etc/machine-id")
    # Repeated cleanup must keep the required placeholder, not delete it.
    ChrootCleaner(tmp_path, {}).clean()
    assert machine.read_bytes() == b""


def test_prepare_removes_live_autologin_before_native_displaymanager_preserves_user_choice(tmp_path):
    runtime = load_runtime()
    target = tmp_path / "installed system"
    for path in runtime["desktop"]["display_managers"]["lightdm"]["files"]:
        file = target / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("[Seat:*]\nautologin-user=liveuser\n")
    sudoers = target / runtime["users"]["sudoers_path"]
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text("liveuser ALL=(ALL) NOPASSWD: ALL\n")
    script = tmp_path / "prepare"
    script.write_bytes(prepare_target_script(runtime))
    subprocess.run(["sh", str(script), str(target)], check=True, capture_output=True)
    assert not sudoers.exists()
    assert not (target / "etc/lightdm/lightdm.conf.d/50-autologin.conf").exists()
    # Native displaymanager will now write only the chosen user's configuration.
    (target / "etc/lightdm/lightdm.conf").write_text("[Seat:*]\nautologin-user=chosen-user\n")
    value = subprocess.run(["sh", "-ec", '. "$1"; printf "%s" "$CRYPTSETUP"', "check",
                            str(target / runtime["build"]["installer_storage"]["cryptsetup_config"]["path"])],
                           check=True, capture_output=True, text=True)
    assert value.stdout == "y"
    assert "chosen-user" in (target / "etc/lightdm/lightdm.conf").read_text()


@pytest.mark.parametrize("arguments", [
    lambda root: [str(root), "@@ROOT@@"],
    lambda root: ["", str(root)],
])
def test_prepare_target_accepts_current_and_legacy_calamares_root_arguments(tmp_path, arguments):
    runtime = load_runtime()
    target = tmp_path / "installed"
    (target / "etc").mkdir(parents=True)
    script = tmp_path / "prepare"
    script.write_bytes(prepare_target_script(runtime))

    result = subprocess.run(
        ["sh", str(script), *arguments(target)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (target / runtime["build"]["installer_storage"]["cryptsetup_config"]["path"]).is_file()


def test_prepare_target_reports_missing_root_when_both_calamares_placeholders_are_unresolved(tmp_path):
    runtime = load_runtime()
    target = tmp_path / "installed"
    (target / "etc").mkdir(parents=True)
    script = tmp_path / "prepare"
    script.write_bytes(prepare_target_script(runtime))

    result = subprocess.run(
        ["sh", str(script), "", "@@ROOT@@"],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 2
    assert "Missing target root" in result.stderr


@pytest.mark.parametrize("initial", [None, "0" * 32, "a" * 32])
def test_target_identity_regenerates_missing_id_and_restores_commit_service(tmp_path, initial):
    runtime = load_runtime()
    identity = runtime["customization"]["identity"]
    target = tmp_path / "installed"
    (target / "etc/systemd/system").mkdir(parents=True)
    (target / identity["commit_mask"]).symlink_to("/dev/null")
    if initial is not None:
        (target / identity["machine_id"]).write_text(initial + "\n")
    executable = target / identity["setup_binary"]
    executable.parent.mkdir(parents=True)
    executable.write_text("generator")
    executable.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    command = tools / "chroot"
    command.write_text('#!/bin/sh\nprintf "%s\\n" 0123456789abcdef0123456789abcdef > "$1/etc/machine-id"\n')
    command.chmod(0o755)
    script = "\n".join([*target_header(str(target)), *identity_lines(identity)]) + "\n"
    result = subprocess.run(["sh", "-e"], input=script.encode(), env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    expected = initial if initial == "a" * 32 else "0123456789abcdef0123456789abcdef"
    assert (target / identity["machine_id"]).read_text().strip() == expected
    assert not (target / identity["commit_mask"]).is_symlink()
    assert (target / identity["dbus_id"]).readlink() == Path(identity["dbus_target"])


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="OpenSSH key generator needed")
def test_installed_ssh_host_keys_are_created_once_without_copying_live_keys(tmp_path):
    identity = load_runtime()["customization"]["identity"]
    target = tmp_path / "target"
    (target / "etc/ssh").mkdir(parents=True)
    server = target / identity["ssh_server_binary"]
    server.parent.mkdir(parents=True)
    server.write_text("server")
    server.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    command = tools / "chroot"
    command.write_text("#!/bin/sh\nexec " + shlex.quote(shutil.which("ssh-keygen")) + ' -A -f "$1"\n')
    command.chmod(0o755)
    script = "\n".join([*target_header(str(target)), *identity_lines(identity)]) + "\n"
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
    subprocess.run(["sh", "-e"], input=script.encode(), env=env, check=True, capture_output=True)
    keys = {path.name: path.read_bytes() for path in (target / "etc/ssh").glob("ssh_host_*_key")}
    assert keys
    subprocess.run(["sh", "-e"], input=script.encode(), env=env, check=True, capture_output=True)
    assert keys == {path.name: path.read_bytes() for path in (target / "etc/ssh").glob("ssh_host_*_key")}
