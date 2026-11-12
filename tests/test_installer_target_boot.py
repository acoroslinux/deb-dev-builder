import os
from pathlib import Path
import subprocess
import io
import shlex
import tarfile

import pytest

from deb_dev_builder.core.installer_media import target_boot_hook, preseed_archive
from deb_dev_builder.core.runtime_config import load_runtime, installer_storage_packages
from deb_dev_builder.core.path_utils import resolve_from_project


@pytest.mark.parametrize("failure,legacy_paths", [(None, False), (None, True), ("crypto", False), ("lvm", False), ("crypttab", False), ("empty-table", False), ("wrong-target", False), ("wrong-source", False), ("no-image", False), ("apt", False), ("fstab", False), ("grub", False), ("live-options", False), ("no-linux", False), ("marker", False)])
def test_target_boot_hook_regenerates_and_validates_after_offline_package_install(tmp_path, failure, legacy_paths):
    runtime = load_runtime()
    storage = runtime["build"]["installer_storage"]
    packages = installer_storage_packages({}, runtime)
    target = tmp_path / "installed system"
    (target / "sys/dev/block").mkdir(parents=True)
    (target / "etc").mkdir(parents=True)
    (target / "boot").mkdir()
    (target / "tmp").mkdir()
    (target / "etc/crypttab").write_text("cryptroot UUID=test none luks,discard\n")
    (target / "etc/fstab").write_text("/dev/mapper/debian--vg-root / ext4 defaults 0 1\n")
    (target / "boot/grub").mkdir()
    (target / "boot/grub/grub.cfg").write_text("linux /boot/vmlinuz-test root=UUID=root ro\ninitrd /boot/initrd.img-test\n")
    for suffix in (".bak", ".old", ".new", ".dpkg-dist"):
        (target / ("boot/initrd.img-test" + suffix)).write_bytes(b"not an active initramfs")
    if failure == "fstab":
        (target / "etc/fstab").unlink()
    elif failure == "grub":
        (target / "boot/grub/grub.cfg").unlink()
    elif failure == "live-options":
        (target / "boot/grub/grub.cfg").write_text("linux /boot/vmlinuz-test boot=live\n")
    elif failure == "no-linux":
        (target / "boot/grub/grub.cfg").write_text("# no Linux boot entries\n")
    if failure != "no-image":
        table = {"empty-table": "# no devices\n", "wrong-target": "other UUID=test none luks,initramfs\n",
                 "wrong-source": "cryptroot UUID=other none luks,initramfs\n"}.get(failure, "cryptroot UUID=test none luks,initramfs\n")
        (target / "boot/initrd.img-test").write_bytes(preseed_archive("", [("cryptroot/crypttab", table.encode(), 0o100600)]))
    paths = ["scripts/local-top/cryptroot", "usr/sbin/lvm", "cryptroot/crypttab",
             "usr/lib/udev/rules.d/56-lvm.rules", "usr/lib/udev/rules.d/69-lvm.rules"]
    removed = {"crypto": paths[0], "lvm": paths[1], "crypttab": paths[2]}.get(failure)
    if legacy_paths:
        paths = [path.removeprefix("usr/") for path in paths]
    (target / "listing").write_text("\n".join(path for path in paths if path != removed) + "\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    chroot = tools / "chroot"
    chroot.write_text('''#!/bin/sh
target=$1
shift
case "$1" in
    lsblk) printf '%s\\n' 'lvm debian--vg-root' 'crypt cryptroot' 'part sda3'; exit 0 ;;
esac
printf '%s\n' "$*" >> "$target/events"
case "$1" in
    apt-get) [ "$FAIL_APT" != 1 ] ;;
    apt-mark|update-initramfs) exit 0 ;;
    lsinitramfs) cat "$target/listing" ;;
    unmkinitramfs) exec /usr/bin/unmkinitramfs "$target$2" "$target$3" ;;
    *) exit 99 ;;
esac
''')
    chroot.chmod(0o755)
    state = "prepared" if legacy_paths else "restored"
    if failure != "marker":
        (target / state).touch()
    script = target_boot_hook(str(target), storage, packages, required_markers=[["prepared", "restored"]])
    subprocess.run(["sh", "-n"], input=script, check=True, capture_output=True)
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"], "FAIL_APT": "1" if failure == "apt" else "0"}
    result = subprocess.run(["sh", "-e"], input=script, env=env, capture_output=True)
    assert (result.returncode == 0) == (failure is None)
    if failure is not None:
        assert result.returncode == 10  # native finish-install must stop, not just log a warning
    if failure == "marker":
        assert not (target / "events").exists()
        return
    events = (target / "events").read_text().splitlines()
    assert not any(suffix in event for event in events for suffix in (".bak", ".old", ".new", ".dpkg-dist"))
    assert events[0].startswith("apt-get --yes --no-remove install ")
    assert "cryptsetup-initramfs" in events[0] and "lvm2" in events[0]
    if failure != "apt":
        assert events[1].startswith("apt-mark manual ")
        assert events[2] == "update-initramfs -u -k all"
        assert "CRYPTSETUP=y" in (target / storage["cryptsetup_config"]["path"]).read_text()
        config_value = subprocess.run(["sh", "-ec", '. "$1"; printf "%s" "${CRYPTSETUP-}"', "check",
                                       str(target / storage["cryptsetup_config"]["path"])], capture_output=True, text=True, check=True)
        assert config_value.stdout == "y"
        expected_options = "luks,discard" if failure == "fstab" else "luks,discard,initramfs"
        assert (target / "etc/crypttab").read_text() == f"cryptroot UUID=test none {expected_options}\n"
        assert not list((target / "tmp").iterdir())  # extracted keys/configuration are removed even on failure


def test_target_boot_validation_runs_after_native_live_cleanup_before_cd_restore():
    policy = load_runtime()["installer"]
    target = Path(policy["target_boot_hook"])
    assert target.parent.as_posix() == "usr/lib/finish-install.d"
    assert "14remove-live-packages" < target.name < Path(policy["offline_apt_cleanup_hook"]).name


def test_calamares_validation_uses_explicit_target_without_reinstalling_or_rebuilding(tmp_path):
    runtime = load_runtime()
    storage = runtime["build"]["installer_storage"]
    target = tmp_path / "installed target"
    (target / "etc").mkdir(parents=True)
    (target / "boot/grub").mkdir(parents=True)
    (target / "etc/fstab").write_text("UUID=root / ext4 defaults 0 1\n")
    (target / "boot/grub/grub.cfg").write_text("linux /boot/vmlinuz-test root=UUID=root ro\n")
    (target / "boot/initrd.img-test").write_bytes(b"image")
    tools = tmp_path / "tools"
    tools.mkdir()
    chroot = tools / "chroot"
    chroot.write_text('#!/bin/sh\n[ "$2" = lsinitramfs ] || exit 99\nprintf "%s\\n" scripts/local-top/cryptroot usr/sbin/lvm usr/lib/udev/rules.d/56-lvm.rules usr/lib/udev/rules.d/69-lvm.rules\n')
    chroot.chmod(0o755)
    script = tmp_path / "validate"
    script.write_bytes(target_boot_hook(None, storage, installer_storage_packages({}, runtime),
                                        package_operations=False, regenerate=False, failure_code=None))
    result = subprocess.run(["sh", str(script), str(target)], capture_output=True,
                            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]})
    assert result.returncode == 0, result.stderr.decode()
    assert not (target / storage["cryptsetup_config"]["path"]).exists()


def test_conffile_policy_preserves_config_without_changing_package_authentication():
    config = load_runtime()["installer"]["offline_apt_config"]["content"]
    assert '"--force-confdef"' in config and '"--force-confold"' in config
    assert "AllowUnauthenticated" not in config and "TrustCDROM" not in config


def test_native_finish_install_stops_before_later_hooks_when_validation_requests_backup(tmp_path):
    packages = sorted(resolve_from_project("cache/installer").glob("*/amd64/finish-install_*.udeb"))
    if not packages:
        pytest.skip("Cached finish-install udeb required for native runner test")
    payload = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(packages[-1])], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        member = next(item for item in archive if item.name.endswith("/postinst"))
        source = archive.extractfile(member).read().decode()
    conf = tmp_path / "debconf"
    conf.write_text("db_progress() { :; }\ndb_subst() { :; }\n")
    hooks = tmp_path / "finish-install.d"
    hooks.mkdir()
    target = tmp_path / "target"
    (target / "sys/dev/block").mkdir(parents=True)
    (target / "etc").mkdir(parents=True)
    runtime = load_runtime()
    failure = hooks / "14zz-deb-dev-target-boot"
    failure.write_bytes(target_boot_hook(str(target), runtime["build"]["installer_storage"], [],
                                         required_markers=["missing-cleanup-marker"]))
    failure.chmod(0o755)
    later = hooks / "15-cd-restore"
    marker = tmp_path / "incorrectly-finished"
    later.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
    later.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in {"logger": "exit 0", "log-output": 'shift 2\nexec "$@"'}.items():
        executable = tools / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o755)
    assert 'partsdir="/usr/lib/finish-install.d"' in source
    source = source.replace("/usr/share/debconf/confmodule", shlex.quote(str(conf)))
    source = source.replace('partsdir="/usr/lib/finish-install.d"', "partsdir=" + shlex.quote(str(hooks)))
    result = subprocess.run(["sh"], input=source.encode(), capture_output=True,
                            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]})
    assert result.returncode == 10
    assert not marker.exists()
