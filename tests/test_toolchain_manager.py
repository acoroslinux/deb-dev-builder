from pathlib import Path
import hashlib
import tarfile
import subprocess

import pytest

from deb_dev_builder.core.toolchain_manager import ToolchainManager, ToolchainManagerError


@pytest.mark.parametrize("repaired", [True, False])
def test_setup_upgrades_old_cache_before_using_new_build_tools(tmp_path, monkeypatch, repaired):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real", required_tools=["apt-ftparchive"], toolchain_config={"packages": ["apt-utils"], "foreign_architectures": []})
    events = []
    status = manager.build_host_dir / "var/lib/dpkg/status"
    binary = manager.build_host_dir / "usr/bin/apt-ftparchive"
    def restore():
        events.append("restore")
        for relative in ("etc/os-release", "usr/bin/apt-get", "usr/bin/xorriso", "var/lib/dpkg/status"):
            path = manager.build_host_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
    def mount():
        events.append("mount")
        manager.is_mounted = True
    def unmount():
        events.append("unmount")
        manager.is_mounted = False
    def run(command, check=True):
        assert check
        events.append(command)
        if command[:2] == ["apt-get", "install"] and repaired:
            binary.write_text("tool")
            status.write_text("Package: apt-utils\nArchitecture: amd64\nStatus: install ok installed\n")
    monkeypatch.setattr(manager, "restore_toolchain_tarball", restore)
    monkeypatch.setattr(manager, "mount_virtual_fs", mount)
    monkeypatch.setattr(manager, "umount_virtual_fs", unmount)
    monkeypatch.setattr(manager, "run_in_build_host", run)
    monkeypatch.setattr(manager, "cache_build_host", lambda: events.append("cache"))
    if repaired:
        manager.setup()
        assert "cache" in events
        assert events.index(["apt-get", "install", "-y", "apt-utils"]) < events.index("cache")
    else:
        with pytest.raises(ToolchainManagerError, match="apt-ftparchive"):
            manager.setup()
        assert "cache" not in events
    assert events[0] == "restore"
    assert ["apt-get", "update"] in events
    assert events[-1] == "unmount"


def test_up_to_date_toolchain_needs_no_network_or_remount(tmp_path, monkeypatch):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real", required_tools=["apt-ftparchive"], toolchain_config={"packages": ["apt-utils"]})
    binary = manager.build_host_dir / "usr/bin/apt-ftparchive"
    binary.parent.mkdir(parents=True)
    binary.write_text("tool")
    status = manager.build_host_dir / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text("Package: apt-utils\nArchitecture: amd64\nStatus: install ok installed\n")
    def unexpected(*args, **kwargs):
        pytest.fail("An up-to-date toolchain must be reused without APT/network operations")
    monkeypatch.setattr(manager, "run_in_build_host", unexpected)
    monkeypatch.setattr(manager, "mount_virtual_fs", unexpected)
    manager.ensure_build_dependencies()


def test_build_dependencies_distinguish_architecture_and_unconfigured_packages(tmp_path):
    manager = ToolchainManager(tmp_path / "work" / "amd64", toolchain_config={"packages": ["tool:arm64", "broken", "tool:amd64"]})
    status = manager.build_host_dir / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text("Package: tool\nArchitecture: arm64\nStatus: install ok installed\n\nPackage: broken\nArchitecture: amd64\nStatus: install ok unpacked\n")
    assert manager._missing_build_packages() == ["broken", "tool:amd64"]


def test_toolchain_unmounts_nested_bind_mounts_before_parents(tmp_path, monkeypatch):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    manager.build_host_dir.mkdir(parents=True)
    manager.project_mount.mkdir(parents=True)
    manager.workdir_mount = manager.project_mount / "workdir"
    manager.workdir_mount.mkdir()
    for name in ("dev", "sys", "proc"):
        (manager.build_host_dir / name).mkdir()
    calls = []

    # The helper is imported lazily by umount_virtual_fs, so patch its source.
    monkeypatch.setattr(
        "deb_dev_builder.core.path_utils.unmount_all_under",
        lambda path: calls.append(("recursive", path)),
    )
    monkeypatch.setattr(
        "deb_dev_builder.core.toolchain_manager.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    manager.umount_virtual_fs()

    assert [path for kind, path in calls if kind == "recursive"] == [
        manager.project_mount / "workdir",
        manager.project_mount,
        manager.build_host_dir / "dev",
        manager.build_host_dir / "sys",
        manager.build_host_dir / "proc",
    ]


def test_toolchain_tarball_requires_a_sha256_digest(tmp_path):
    archive = tmp_path / "build-host.tar.zst"
    archive.write_bytes(b"not an archive")
    manager = ToolchainManager(
        tmp_path / "work",
        mode="real",
        toolchain_config={"tarball_path": str(archive)},
    )

    with pytest.raises(ToolchainManagerError, match="SHA-256 is required"):
        manager.restore_toolchain_tarball()


def test_verified_external_toolchain_is_copied_to_the_workspace_cache(tmp_path):
    root = tmp_path / "source-root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text("ID=debian\n")
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "xorriso").write_text("")
    archive = tmp_path / "toolchain.tar.xz"
    with tarfile.open(archive, "w:xz") as handle:
        for entry in root.iterdir():
            handle.add(entry, arcname=entry.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manager = ToolchainManager(
        tmp_path / "work" / "amd64",
        mode="real",
        toolchain_config={"tarball_path": str(archive), "tarball_sha256": digest},
    )

    manager.restore_toolchain_tarball()

    cached = manager._cached_toolchain_archives()[0]
    assert cached.is_file()
    assert cached.with_name(f"{cached.name}.sha256").is_file()


def test_verified_toolchain_restore_allows_internal_absolute_symlinks(tmp_path):
    root = tmp_path / "source-root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text("ID=debian\n")
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "xorriso").write_text("")
    (root / "var").mkdir()
    (root / "var" / "run").symlink_to("/run")
    archive = tmp_path / "toolchain.tar.xz"
    with tarfile.open(archive, "w:xz") as handle:
        for entry in root.iterdir():
            handle.add(entry, arcname=entry.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manager = ToolchainManager(
        tmp_path / "work" / "amd64", mode="real",
        toolchain_config={"tarball_path": str(archive), "tarball_sha256": digest},
    )

    manager.restore_toolchain_tarball()

    assert (manager.build_host_dir / "var" / "run").is_symlink()


def test_oci_layer_whiteout_removes_a_file_without_escaping_build_host(tmp_path):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    manager.build_host_dir.mkdir(parents=True)
    removed = manager.build_host_dir / "etc" / "obsolete"
    removed.parent.mkdir()
    removed.write_text("old")
    layer = tmp_path / "layer.tar"
    with tarfile.open(layer, "w") as handle:
        marker = tarfile.TarInfo("./etc/.wh.obsolete")
        marker.size = 0
        handle.addfile(marker)

    manager._oci_extract_layer(layer)

    assert not removed.exists()


def test_oci_layer_accepts_the_root_directory_entry(tmp_path):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    manager.build_host_dir.mkdir(parents=True)
    layer = tmp_path / "layer.tar"
    with tarfile.open(layer, "w") as handle:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        handle.addfile(root)

    manager._oci_extract_layer(layer)


def test_oci_layer_allows_absolute_symlinks_inside_the_rootfs(tmp_path):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    manager.build_host_dir.mkdir(parents=True)
    layer = tmp_path / "layer.tar"
    with tarfile.open(layer, "w") as handle:
        directory = tarfile.TarInfo("etc/alternatives")
        directory.type = tarfile.DIRTYPE
        handle.addfile(directory)
        link = tarfile.TarInfo("etc/alternatives/awk")
        link.type = tarfile.SYMTYPE
        link.linkname = "/usr/bin/mawk"
        handle.addfile(link)

    manager._oci_extract_layer(layer)

    assert (manager.build_host_dir / "etc" / "alternatives" / "awk").is_symlink()


def test_build_host_cache_excludes_the_bound_project_directory(tmp_path):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    manager.build_host_dir.mkdir(parents=True)
    (manager.build_host_dir / "etc").mkdir()
    (manager.build_host_dir / "etc" / "os-release").write_text("ID=debian\n")
    (manager.build_host_dir / "usr" / "bin").mkdir(parents=True)
    (manager.build_host_dir / "usr" / "bin" / "xorriso").write_text("")
    project_root = manager.project_mount.relative_to(manager.build_host_dir).parts[0]
    (manager.build_host_dir / project_root).mkdir()
    (manager.build_host_dir / project_root / "project-file").write_text("must not be cached")

    archive = manager.cache_build_host()

    with tarfile.open(archive, "r:xz") as handle:
        names = [name.removeprefix("./") for name in handle.getnames()]
    assert f"{project_root}/project-file" not in names
    assert "usr/bin/xorriso" in names


def test_build_host_cache_uses_parallel_xz_and_excludes_virtual_roots(tmp_path, monkeypatch):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    manager.build_host_dir.mkdir(parents=True)
    (manager.build_host_dir / "etc").mkdir()
    (manager.build_host_dir / "etc" / "os-release").write_text("ID=debian\n")
    (manager.build_host_dir / "usr" / "bin").mkdir(parents=True)
    (manager.build_host_dir / "usr" / "bin" / "xorriso").write_text("")
    captured = []
    subprocess_module = __import__("subprocess")
    real_run = subprocess_module.run
    real_popen = subprocess_module.Popen

    def capture(command, *args, **kwargs):
        captured.append(command)
        return real_run(command, *args, **kwargs)

    def capture_popen(command, *args, **kwargs):
        captured.append(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr("deb_dev_builder.core.toolchain_manager.subprocess.run", capture)
    monkeypatch.setattr("deb_dev_builder.core.toolchain_manager.subprocess.Popen", capture_popen)
    manager.cache_build_host()

    assert any(command[0] == "tar" and "--exclude=./proc" in command for command in captured)
    assert ["xz", "--threads=0", "--compress", "--stdout", "--check=crc64"] in captured


@pytest.mark.parametrize(("machine", "expected"), [
    ("x86_64", ("amd64", None)), ("i686", ("386", None)),
    ("aarch64", ("arm64", None)), ("armv7l", ("arm", "v7")),
    ("ppc64le", ("ppc64le", None)), ("riscv64", ("riscv64", None)),
    ("s390x", ("s390x", None)),
])
def test_oci_platform_mapping(machine, expected):
    assert ToolchainManager._oci_platform(machine) == expected


def test_unsupported_oci_platform_is_rejected():
    with pytest.raises(ToolchainManagerError, match="Unsupported host architecture"):
        ToolchainManager._oci_platform("mystery-cpu")


def test_devuan_suite_alias_uses_the_ceres_script(tmp_path):
    manager = ToolchainManager(tmp_path / "work" / "amd64", mode="real")
    scripts = manager.build_host_dir / "usr" / "share" / "debootstrap" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sid").write_text("#!/bin/sh\n")
    (scripts / "ceres").symlink_to("sid")

    manager.ensure_devuan_debootstrap_scripts(["daedalus", "excalibur", "freia"])

    for suite in ("daedalus", "excalibur", "freia"):
        assert (scripts / suite).is_symlink()
        assert (scripts / suite).readlink() == Path("ceres")
