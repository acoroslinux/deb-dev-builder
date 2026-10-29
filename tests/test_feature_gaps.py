import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

import cli
from deb_dev_builder.core.apt_manager import APTManager
from deb_dev_builder.core.container_engine import ContainerEngine
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.disk_engine import DiskEngine, DiskEngineError
from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.toolchain_manager import ToolchainManager, ToolchainManagerError


class RecordingChroot:
    mode = "real"

    def __init__(self, root):
        self.target_root = root
        self.calls = []

    def run_in_chroot(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)


@pytest.mark.parametrize("groups", [[], ["audio", "video"], ["sudo"]])
def test_live_group_membership_controls_sudo(tmp_path, groups):
    chroot = RecordingChroot(tmp_path)
    sudoers = tmp_path / "etc/sudoers.d/live_user_nopasswd"
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text("stale unrestricted permissions")
    customizer = SystemCustomizer(chroot, {"live_user": {"name": "guest", "groups": groups}})
    customizer.setup_live_users()
    assert sudoers.exists() == ("sudo" in groups)
    command = next(cmd for cmd, _ in chroot.calls if cmd[0] == "useradd")
    assert command[command.index("-G") + 1].split(",") == [*groups, "nopasswdlogin"]
    if sudoers.exists():
        assert sudoers.stat().st_mode & 0o777 == 0o440
    live_config = (tmp_path / "etc/live/config.conf.d/10-deb-dev-user.conf").read_text()
    assert "LIVE_USERNAME=guest" in live_config
    assert "LIVE_CONFIG_NOCOMPONENTS=user-setup,sudo" in live_config


def test_autologin_preserves_interactive_pam_authentication(tmp_path):
    normal = tmp_path / "etc/pam.d/lightdm"
    normal.parent.mkdir(parents=True)
    normal.write_text("@include common-auth\n")
    customizer = SystemCustomizer(RecordingChroot(tmp_path), {"session_type": "wayland"})
    customizer.configure_autologin()
    assert normal.read_text() == "@include common-auth\n"
    assert "common-session" in (normal.parent / "lightdm-autologin").read_text()
    assert "DisplayServer=wayland" in (tmp_path / "etc/sddm.conf").read_text()
    assert "pam-service=lightdm\n" in (tmp_path / "etc/lightdm/lightdm.conf").read_text()


@pytest.mark.parametrize("init_system,expected", [("sysvinit", "update-rc.d"), ("openrc", "rc-update")])
def test_non_systemd_service_names_are_normalized(tmp_path, init_system, expected):
    chroot = RecordingChroot(tmp_path)
    SystemCustomizer(chroot, {"init_system": init_system, "services": {
        "enable": ["NetworkManager.service"], "disable": ["cups.service"],
    }}).setup_services()
    assert all(cmd[0] == expected for cmd, _ in chroot.calls)
    assert any("network-manager" in cmd for cmd, _ in chroot.calls)
    assert any("cups" in cmd for cmd, _ in chroot.calls)
    assert not any("cups.service" in cmd for cmd, _ in chroot.calls)


def test_runit_can_disable_previously_enabled_services(tmp_path):
    (tmp_path / "etc/sv/cron").mkdir(parents=True)
    chroot = RecordingChroot(tmp_path)
    customizer = SystemCustomizer(chroot, {"init_system": "runit", "services": {"enable": ["cron.service"]}})
    customizer.setup_services()
    link = tmp_path / "etc/service/cron"
    assert link.resolve() == tmp_path / "etc/sv/cron"
    customizer.config["services"] = {"disable": ["cron.service"]}
    customizer.setup_services()
    assert not link.is_symlink()


def test_theme_and_icon_overlays_install_to_standard_locations(tmp_path, monkeypatch):
    source = tmp_path / "project/configs/custom_files"
    for name in ("themes/demo/gtk.css", "icons/demo/index.theme"):
        asset = source / name
        asset.parent.mkdir(parents=True)
        asset.write_text("asset")
    monkeypatch.setattr("deb_dev_builder.core.path_utils.resolve_from_project", lambda _: tmp_path / "project")
    SystemCustomizer(RecordingChroot(tmp_path / "root"), {}).copy_custom_files()
    assert (tmp_path / "root/usr/share/themes/demo/gtk.css").read_text() == "asset"
    assert (tmp_path / "root/usr/share/icons/demo/index.theme").is_file()


def test_flathub_imports_official_descriptor(tmp_path):
    chroot = RecordingChroot(tmp_path)
    SystemCustomizer(chroot, {"with_flathub": True}).configure_flathub()
    assert chroot.calls == [([
        "flatpak", "remote-add", "--system", "--if-not-exists", "flathub",
        "https://dl.flathub.org/repo/flathub.flatpakrepo",
    ], {"check": True})]


def test_calamares_removes_selected_live_user_and_limits_privileges(tmp_path):
    SystemCustomizer(RecordingChroot(tmp_path), {
        "with_calamares": True, "live_user": {"name": "demo"},
    }).configure_calamares()
    assert "username: demo" in (tmp_path / "etc/calamares/modules/removeuser.conf").read_text()
    assert "subject.isInGroup('sudo')" in (tmp_path / "etc/polkit-1/rules.d/49-calamares.rules").read_text()


@pytest.mark.parametrize("arch,suffix", [("amd64", "x64"), ("i386", "ia32"), ("arm64", "aa64"), ("armhf", "arm"), ("riscv64", "riscv64")])
@pytest.mark.parametrize("bootloader", ["grub2-uefi", "systemd-boot"])
def test_disk_boot_files_match_target_architecture(tmp_path, arch, suffix, bootloader):
    root = tmp_path / "root"
    boot = root / "boot"
    boot.mkdir(parents=True)
    for version in ("6.9", "6.12"):
        (boot / f"vmlinuz-{version}").write_bytes(b"kernel")
        (boot / f"initrd.img-{version}").write_bytes(b"initrd")
    (boot / "vmlinuz-7.0").write_bytes(b"unpaired")
    if bootloader == "systemd-boot":
        binary = root / f"usr/lib/systemd/boot/efi/systemd-boot{suffix}.efi"
    else:
        binary = boot / f"efi/EFI/debian/grub{suffix}.efi"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"EFI executable")
    engine = DiskEngine(tmp_path / "work", root, "disk", {
        "dpkg_arch": arch, "bootloader": {"type": bootloader},
        "boot": {"kernel_params": "boot=live components union=overlay quiet root=/dev/wrong"},
    }, "real")
    engine._stage_boot_files()
    staged = tmp_path / "work/efi_tmp"
    assert (staged / f"EFI/BOOT/BOOT{suffix.upper()}.EFI").read_bytes() == b"EFI executable"
    config = (staged / ("loader/entries/debian.conf" if bootloader == "systemd-boot" else "EFI/BOOT/grub.cfg")).read_text()
    assert "6.12" in config and "6.9" not in config
    assert "root=LABEL=ROOTFS" in config
    assert "boot=live" not in config and "root=/dev/wrong" not in config
    if bootloader == "systemd-boot":
        assert (staged / "loader/loader.conf").read_text().splitlines() == ["default debian.conf", "timeout 3"]


def test_disk_fstab_preserves_other_mounts_and_is_idempotent(tmp_path):
    (tmp_path / "root/etc").mkdir(parents=True)
    fstab = tmp_path / "root/etc/fstab"
    fstab.write_text("# comment\n/dev/old / ext4 defaults 0 1\nLABEL=DATA /srv ext4 defaults 0 2\n")
    engine = DiskEngine(tmp_path / "work", tmp_path / "root", "disk", {"fs_type": "btrfs"}, "real")
    engine._configure_fstab()
    first = fstab.read_text()
    engine._configure_fstab()
    assert fstab.read_text() == first
    assert "LABEL=DATA /srv" in first and "/dev/old" not in first
    assert "LABEL=ROOTFS / btrfs" in first and "LABEL=EFI /boot/efi" in first


def test_boot_artifact_overlap_fails_before_writing(tmp_path):
    (tmp_path / "root/boot").mkdir(parents=True)
    (tmp_path / "root/boot/blob").write_bytes(b"bad")
    image = tmp_path / "disk.img"
    image.write_bytes(b"unchanged")
    engine = DiskEngine(tmp_path / "work", tmp_path / "root", "disk", {"hardware": {
        "boot_artifacts": [{"source": "/boot/blob", "offset_kib": 1}],
    }}, "real")
    with pytest.raises(DiskEngineError, match="overlaps GPT"):
        engine._inject_boot_artifacts(image)
    assert image.read_bytes() == b"unchanged"


def test_isolated_tools_never_fall_back_to_the_host(tmp_path, monkeypatch):
    manager = ToolchainManager(tmp_path / "work/amd64", mode="real")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(ToolchainManagerError, match="mounted isolated"):
        manager.run_tool("tar", ["--version"])
    with pytest.raises(ToolchainManagerError, match="mounted isolated"):
        manager.run_in_build_host(["apt-get", "install", "-y", "xz-utils"])
    assert not calls


def test_repo_profiles_reach_apt_with_signing_and_architecture_options(tmp_path):
    config = {"repos": [{"url": "https://example.invalid/debian", "suite": "stable", "components": ["main"],
                         "signed_by": "/usr/share/keyrings/custom.gpg", "architectures": ["amd64"]}]}
    APTManager(RecordingChroot(tmp_path), config).configure_sources_list()
    assert "deb [signed-by=/usr/share/keyrings/custom.gpg arch=amd64] https://example.invalid/debian stable main" in (tmp_path / "etc/apt/sources.list").read_text()


def test_oci_preserves_empty_runtime_directories_without_their_contents(tmp_path):
    root = tmp_path / "root"
    (root / "tmp").mkdir(parents=True)
    (root / "tmp").chmod(0o1777)
    (root / "tmp/secret").write_text("excluded")
    result = ContainerEngine(root, str(tmp_path / "image"), {"dpkg_arch": "armhf"}).build_oci_archive()
    with tarfile.open(result) as archive:
        index = json.load(archive.extractfile("index.json"))
        descriptor = index["manifests"][0]
        assert descriptor["platform"] == {"architecture": "arm", "os": "linux", "variant": "v7"}
        manifest = json.load(archive.extractfile("blobs/" + descriptor["digest"].replace(":", "/")))
        layer = archive.extractfile("blobs/" + manifest["layers"][0]["digest"].replace(":", "/"))
        with tarfile.open(fileobj=io.BytesIO(layer.read())) as contents:
            assert "tmp" in contents.getnames() and "tmp/secret" not in contents.getnames()
            assert contents.getmember("tmp").mode == 0o1777


@pytest.mark.parametrize("arguments,output_format", [
    (["--vm-profile=qemu", "--format=img"], "img"),
    (["--device=generic-uefi", "-fqcow2"], "qcow2"),
    (["--di-mode=netboot", "--format=iso"], "iso"),
])
def test_cli_preserves_explicit_format_with_equals_or_short_options(monkeypatch, arguments, output_format):
    captured = {}
    class FakeOrchestrator:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def validate(self):
            return {"valid": True}
    monkeypatch.setattr(cli, "BuildOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("sys.argv", ["cli.py", *arguments, "--validate"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert captured["output_format"] == output_format


def test_systemd_boot_profile_is_available_for_disks():
    orch = BuildOrchestrator(distro="debian-13", bootloader="systemd-boot", output_format="img")
    assert orch.validate()["valid"]
    assert "systemd-boot" in orch.config["software"]


@pytest.mark.parametrize("option", ["with_debian_installer", "with_offline_repo"])
def test_iso_only_features_are_not_silently_ignored_on_containers(option):
    assert not BuildOrchestrator(distro="debian-13", output_format="oci", **{option: True}).validate()["valid"]


def test_grub_disk_builds_a_standalone_loader_without_host_filesystem_probing(tmp_path):
    boot = tmp_path / "root/boot"
    boot.mkdir(parents=True)
    (boot / "vmlinuz-6.12").write_bytes(b"kernel")
    (boot / "initrd.img-6.12").write_bytes(b"initrd")
    calls = []
    class Toolchain:
        def run_in_build_host(self, command, **kwargs):
            calls.append(command)
            Path(command[command.index("-o") + 1]).write_bytes(b"standalone EFI")
    engine = DiskEngine(tmp_path / "work", tmp_path / "root", "disk", {
        "dpkg_arch": "arm64", "bootloader": {"type": "grub2-uefi"},
    }, "real", Toolchain())
    engine._stage_boot_files()
    assert calls[0][:3] == ["grub-mkstandalone", "-O", "arm64-efi"]
    assert (tmp_path / "work/efi_tmp/EFI/BOOT/BOOTAA64.EFI").is_file()
    assert (boot / "grub/grub.cfg").is_file()


@pytest.mark.parametrize("fail_install", [False, True])
def test_bios_install_releases_loop_and_mount_even_on_failure(tmp_path, fail_install):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "commands"
    for name in ("losetup", "mount", "umount", "grub-install"):
        script = '#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$TEST_LOG"\n'
        if name == "losetup":
            script += '[ "$1" != "--detach" ] && printf "/dev/loop-test\\n"\nexit 0\n'
        if name == "grub-install" and fail_install:
            script += "exit 7\n"
        binary = fakebin / name
        binary.write_text(script)
        binary.chmod(0o755)
    class IsolatedRunner:
        def run_in_build_host(self, command, **kwargs):
            return subprocess.run(command, env={"PATH": str(fakebin), "TEST_LOG": str(log)}, **kwargs)
    engine = DiskEngine(tmp_path / "work with spaces", tmp_path / "root", "disk", {}, "real", IsolatedRunner())
    if fail_install:
        with pytest.raises(subprocess.CalledProcessError):
            engine._install_bios_bootloader(tmp_path / "image with spaces.img")
    else:
        engine._install_bios_bootloader(tmp_path / "image with spaces.img")
    lines = log.read_text().splitlines()
    assert lines[0].startswith("losetup --find --show --partscan -- ")
    assert "mount /dev/loop-testp2 " in lines[1]
    assert lines[-2].startswith("umount ")
    assert lines[-1] == "losetup --detach /dev/loop-test"


def test_bootstrap_retries_debootstrap_and_no_seed_disables_cache_creation(tmp_path, monkeypatch):
    root = tmp_path / "root"
    commands = []
    class IsolatedRunner:
        def _tool_in_build_host(self, tool):
            return tool in {"mmdebstrap", "debootstrap"}
        def run_in_build_host(self, command, **kwargs):
            commands.append(command)
            if command[0] == "mmdebstrap":
                root.mkdir()
                (root / "partial").write_text("failed bootstrap")
                return subprocess.CompletedProcess(command, 1)
            assert command[0] == "debootstrap"
            assert list(root.iterdir()) == []
            (root / "etc").mkdir()
            (root / "etc/os-release").write_text("ID=debian\n")
            status = root / "var/lib/dpkg/status"
            status.parent.mkdir(parents=True)
            status.write_text("Package: base-files\nStatus: install ok installed\n\n")
            return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("deb_dev_builder.core.path_utils.unmount_all_under", lambda _: None)
    monkeypatch.setattr("deb_dev_builder.core.path_utils.mountpoints_under", lambda _: [])
    manager = APTManager(RecordingChroot(root), {
        "distro": "debian-13", "suite": "trixie", "mirror": "https://example.invalid",
    }, IsolatedRunner())
    manager.bootstrap_rootfs("trixie", "amd64", use_seed=False)
    assert [command[0] for command in commands] == ["mmdebstrap", "debootstrap"]
    assert not list(tmp_path.rglob("seed-*.tar.gz"))


def test_repository_signing_key_is_staged_before_apt_runs(tmp_path):
    source = tmp_path / "company.gpg"
    source.write_bytes(b"test signing key")
    root = tmp_path / "root"
    manager = APTManager(RecordingChroot(root), {"repos": [{
        "url": "https://example.invalid", "key_source": str(source),
        "signed_by": "/etc/apt/keyrings/company.gpg",
    }]})
    manager.configure_sources_list()
    key = root / "etc/apt/keyrings/company.gpg"
    assert key.read_bytes() == source.read_bytes()
    assert key.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("compression,suffix", [("zstd", "zst"), ("xz", "xz"), ("gzip", "gz"), ("lz4", "lz4")])
def test_mock_disk_compression_matches_real_artifact_names(tmp_path, compression, suffix):
    engine = DiskEngine(tmp_path / "work", tmp_path / "root", str(tmp_path / "disk"), {"compression": compression}, "mock")
    assert engine.build_disk_image("qcow2").name == f"disk.qcow2.{suffix}"


def test_syslinux_disk_stages_kernel_entry_and_required_module(tmp_path):
    root = tmp_path / "root"
    (root / "boot").mkdir(parents=True)
    (root / "boot/vmlinuz-6.12").write_bytes(b"kernel")
    (root / "boot/initrd.img-6.12").write_bytes(b"initrd")
    module = root / "usr/lib/syslinux/modules/bios/ldlinux.c32"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"module")
    engine = DiskEngine(tmp_path / "work", root, "disk", {
        "architecture": "amd64", "bootloader": {"type": "syslinux"},
        "boot": {"kernel_params": "boot=live quiet"},
    }, "real")
    engine._stage_boot_files()
    content = (root / "boot/syslinux/syslinux.cfg").read_text()
    assert "LINUX /boot/vmlinuz-6.12" in content
    assert "root=LABEL=ROOTFS rw quiet" in content and "boot=live" not in content
    assert (root / "boot/syslinux/ldlinux.c32").read_bytes() == b"module"
    assert not engine._uses_bios()  # GPT MBR boot code uses legacy_boot on the root partition.


def test_syslinux_rejects_unsupported_disk_filesystems_before_build():
    orch = BuildOrchestrator(distro="debian-13", bootloader="syslinux", output_format="img", fs_type="xfs")
    assert not orch.validate()["valid"]


@pytest.mark.parametrize("loader", ["grub2-hybrid", "grub2-uefi", "syslinux"])
def test_disk_partition_assembly_includes_selected_boot_mode(tmp_path, monkeypatch, loader):
    root = tmp_path / "root"
    (root / "boot").mkdir(parents=True)
    (root / "boot/vmlinuz-6.12").write_bytes(b"kernel")
    (root / "boot/initrd.img-6.12").write_bytes(b"initrd")
    module = root / "usr/lib/syslinux/modules/bios/ldlinux.c32"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"module")
    calls = []
    class Toolchain:
        def run_in_build_host(self, command, **kwargs):
            calls.append(command)
            if command[0] == "truncate":
                Path(command[-1]).write_bytes(b"filesystem image")
            elif command[0] == "grub-mkstandalone":
                Path(command[command.index("-o") + 1]).write_bytes(b"EFI executable")
            elif command[0] == "dd":
                for argument in command:
                    if argument.startswith("of="):
                        Path(argument[3:]).write_bytes(b"assembled image")
            return subprocess.CompletedProcess(command, 0)
    engine = DiskEngine(tmp_path / "work", root, str(tmp_path / "disk"), {
        "architecture": "amd64", "bootloader": {"type": loader},
    }, "real", Toolchain())
    monkeypatch.setattr(engine, "_calculate_image_size", lambda _: 1)
    result = engine.build_disk_image("img")
    assert result.read_bytes() == b"assembled image"
    assert (root / "etc/fstab").is_file()
    assert ("bios_grub" in [command[-2] for command in calls if command[0] == "parted" and "set" in command]) == (loader == "grub2-hybrid")
    scripts = [command[-1] for command in calls if command[0] == "/bin/sh"]
    if loader == "grub2-hybrid":
        assert "grub-install --target=i386-pc" in scripts[0]
    elif loader == "syslinux":
        assert "extlinux --install" in scripts[0] and "gptmbr.bin" in scripts[0]
    else:
        assert not scripts
