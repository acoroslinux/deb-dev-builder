from pathlib import Path
import hashlib
import tarfile

import pytest

from deb_dev_builder.core.toolchain_manager import ToolchainManager, ToolchainManagerError


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
        names = handle.getnames()
    assert f"{project_root}/project-file" not in names
    assert "usr/bin/xorriso" in names
