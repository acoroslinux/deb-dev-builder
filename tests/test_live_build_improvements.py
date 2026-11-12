import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from deb_dev_builder.core.initrd_editor import Entry, pack_initrd, unpack_initrd, customize_initrd
from deb_dev_builder.core.installer_media import preseed_archive
from deb_dev_builder.core.runtime_config import load_runtime, RuntimeConfigError
from deb_dev_builder.core.package_policy import installed_packages, live_removals
from deb_dev_builder.core.build_report import write_build_report
from deb_dev_builder.core.iso_engine import ISOEngine, ISOEngineError


def test_merge_multiple_gzip_cpio_members_and_preserve_special_metadata(tmp_path):
    entries = {
        "old": Entry("old", b"old", stat.S_IFREG | 0o751, 12, 34, 10),
        "link": Entry("link", b"/usr/lib", stat.S_IFLNK | 0o777),
        "dev/console": Entry("dev/console", b"", stat.S_IFCHR | 0o600, rdevmajor=5, rdevminor=1),
    }
    path = tmp_path / "initrd.gz"
    path.write_bytes(pack_initrd(entries) + preseed_archive("one") + preseed_archive("two"))
    customize_initrd(path, load_runtime()["installer"]["customization"], "text")
    result = unpack_initrd(path.read_bytes())
    assert result["preseed.cfg"].data == b"two"
    assert result["old"] == entries["old"]
    assert result["link"] == entries["link"]
    assert result["dev/console"] == entries["dev/console"]
    if shutil.which("cpio"):
        extracted = subprocess.run(["cpio", "-i", "--to-stdout", "preseed.cfg"],
                                   input=gzip.decompress(path.read_bytes()), capture_output=True, check=True)
        assert extracted.stdout == b"two"


def test_overlays_frontends_removals_patches_and_generated_priority(tmp_path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "banner").write_text("custom")
    (overlay / "banner").chmod(0o640)
    (overlay / "link").symlink_to("/absolute/guest/path")
    original = pack_initrd({"theme": Entry("theme", b"Clearlooks", 0o100644),
                            "obsolete": Entry("obsolete", b"", 0o100644)})
    policy = {**load_runtime()["installer"]["customization"], "enabled": True,
              "overlays": [{"source": str(overlay), "frontends": ["gtk"], "uid": 0, "gid": 0}],
              "remove": ["obsolete"],
              "patches": [{"path": "theme", "search": "Clearlooks", "replace": "dark", "count": 1, "frontends": ["gtk"]}]}
    for frontend in ("text", "gtk"):
        path = tmp_path / (frontend + ".gz")
        path.write_bytes(original)
        customize_initrd(path, policy, frontend, generated=[Entry("preseed.cfg", b"mandatory", 0o100644)], epoch=123)
        entries = unpack_initrd(path.read_bytes())
        assert "obsolete" not in entries
        assert entries["theme"].data == (b"dark" if frontend == "gtk" else b"Clearlooks")
        assert ("banner" in entries) == (frontend == "gtk")
        assert entries["preseed.cfg"].data == b"mandatory"
        assert all(entry.mtime == 123 for entry in entries.values())
        if frontend == "gtk":
            assert entries["banner"].mode & 0o777 == 0o640
            assert entries["link"].data == b"/absolute/guest/path"


def test_bad_initrd_and_failed_patch_do_not_replace_original(tmp_path):
    bad = Entry("../outside", b"bad", 0o100644)
    with pytest.raises(ValueError, match="Unsafe"):
        unpack_initrd(pack_initrd({bad.name: bad}))
    path = tmp_path / "initrd.gz"
    original = preseed_archive("data")
    path.write_bytes(original)
    policy = {**load_runtime()["installer"]["customization"], "enabled": True,
              "patches": [{"path": "preseed.cfg", "search": "absent", "replace": "x", "count": 1, "frontends": ["text"]}]}
    with pytest.raises(ValueError, match="match count"):
        customize_initrd(path, policy, "text")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".initrd-*"))


def test_hardlinks_from_real_cpio_are_materialized(tmp_path):
    if not shutil.which("cpio"):
        pytest.skip("GNU cpio required")
    (tmp_path / "first").write_bytes(b"hardlink data")
    os.link(tmp_path / "first", tmp_path / "second")
    archive = subprocess.run(["cpio", "-o", "-H", "newc"], cwd=tmp_path,
                             input=b"first\nsecond\n", capture_output=True, check=True).stdout
    entries = unpack_initrd(gzip.compress(archive))
    assert entries["first"].data == entries["second"].data == b"hardlink data"


def test_inventory_version_changes_and_uninstalled_paragraphs(tmp_path):
    status = tmp_path / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text("Package: base\nStatus: install ok installed\nVersion: 1\nArchitecture: amd64\n\n"
                      "Package: removed\nStatus: deinstall ok config-files\nVersion: 1\nArchitecture: amd64")
    before = installed_packages(tmp_path)
    status.write_text(status.read_text().replace("Version: 1", "Version: 2") +
                      "\n\nPackage: installer\nStatus: install ok installed\nVersion: 1\nArchitecture: amd64")
    after = installed_packages(tmp_path)
    assert live_removals(before, after, []) == ["installer"]
    assert live_removals(before, after, ["installer"]) == []
    assert "removed:amd64" not in after


@pytest.mark.parametrize("override", [
    {"build": {"reproducibility": {"source_date_epoch": -1}}},
    {"build": {"reproducibility": {"source_date_epoch": True}}},
    {"build": {"packages": {"local_repository": "../outside"}}},
    {"artifacts": {"iso": {"secure_boot": "yes"}}},
    {"installer": {"customization": {"compression_level": 20}}},
])
def test_invalid_pipeline_policy_fails_early(override):
    with pytest.raises(RuntimeConfigError):
        load_runtime({"runtime": override})


def test_report_has_evidence_without_live_password(tmp_path):
    artifact = tmp_path / "system.iso"
    artifact.write_bytes(b"image")
    config = {"architecture": "amd64", "live_user": {"password": "private"}, "preseed": "secret text"}
    write_build_report(artifact, config, load_runtime(config), tmp_path / "absent-root", "mock")
    data = (tmp_path / "system.iso.build.json").read_text()
    report = json.loads(data)
    assert "private" not in data and "secret text" not in data
    assert report["validation"]["installation_tested"] is False
    assert report["artifact"]["bytes"] == 5


def test_internal_checksums_include_other_isolinux_files_and_exclude_patched_image(tmp_path):
    engine = ISOEngine(tmp_path, tmp_path / "root", "out", {}, "mock", None)
    (engine.iso_staging / "isolinux").mkdir(parents=True)
    (engine.iso_staging / "isolinux/isolinux.bin").write_bytes(b"modified by xorriso")
    (engine.iso_staging / "isolinux/menu.cfg").write_text("menu")
    (engine.iso_staging / "name\nwith\\escapes").write_text("file")
    engine._write_media_checksums()
    for algorithm in ("md5", "sha256"):
        content = (engine.iso_staging / (algorithm + "sum.txt")).read_text()
        assert "./isolinux/menu.cfg" in content
        assert "isolinux.bin" not in content
        assert "sum.txt" not in content
        subprocess.run([algorithm + "sum", "-c", algorithm + "sum.txt"], cwd=engine.iso_staging, check=True, capture_output=True)


def test_required_signed_loader_is_architecture_specific(tmp_path):
    cfg = {"architecture": "aarch64", "runtime": {"artifacts": {"iso": {"secure_boot": "require"}}}}
    engine = ISOEngine(tmp_path, tmp_path / "root", "out", cfg, "real", None)
    destination = tmp_path / "efi"
    destination.mkdir()
    with pytest.raises(ISOEngineError, match="arm64-efi"):
        engine._stage_signed_loaders([tmp_path / "root"], destination, [("arm64-efi", "BOOTAA64.EFI")])
    spec = engine.runtime["artifacts"]["iso"]["signed_loaders"]["arm64-efi"]
    for key in ("shim", "grub"):
        path = tmp_path / "root" / spec[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
    engine._stage_signed_loaders([tmp_path / "root"], destination, [("arm64-efi", "BOOTAA64.EFI")])
    assert (destination / "BOOTAA64.EFI").read_bytes() == b"shim"
    assert (destination / "grubaa64.efi").read_bytes() == b"grub"


def test_reproducible_tar_content_is_independent_of_output_name_and_source_mtime(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    item = source / "config"
    item.write_text("configuration")
    config = {"runtime": {"build": {"reproducibility": {"source_date_epoch": 1234567890}}}}
    engine = ISOEngine(tmp_path, source, "out", config, "real", None)
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    os.utime(item, (100, 100))
    engine._write_tar_archive(source, first)
    os.utime(item, (200, 200))
    engine._write_tar_archive(source, second)
    assert first.read_bytes() == second.read_bytes()


def test_local_debs_are_indexed_with_metadata_and_cleaned_without_touching_host(tmp_path, monkeypatch):
    from deb_dev_builder.core.apt_manager import APTManager
    from deb_dev_builder.core.chroot_manager import ChrootManager
    package = tmp_path / "local.deb"
    package.write_bytes(b"local archive")
    config = {"dpkg_arch": "amd64", "runtime": {"build": {"packages": {
        "local_debs": [str(package)], "foreign_architectures": ["i386"],
        "build_only_sources": ["deb https://build.example.test/debian trixie main"]}}}}
    root = tmp_path / "root"
    chroot = ChrootManager(root, mode="real")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Package: local-tool\nArchitecture: all\n")
    monkeypatch.setattr(chroot, "run_in_chroot", run)
    class Toolchain:
        def run_in_build_host(self, command, **kwargs):
            assert command[:2] == ["/bin/sh", "-ec"]
            assert "apt-ftparchive packages" in command[2]
    apt = APTManager(chroot, config, Toolchain())
    assert apt.prepare_package_inputs() == ["local-tool"]
    assert ["dpkg", "--add-architecture", "i386"] in calls
    policy = apt.runtime["build"]["packages"]
    assert (root / policy["local_source"]).is_file()
    assert (root / policy["build_sources_path"]).is_file()
    apt.remove_package_inputs()
    assert not (root / policy["local_source"]).exists()
    assert not (root / policy["build_sources_path"]).exists()
    assert not (root / policy["local_repository"]).exists()
    assert package.read_bytes() == b"local archive"


def test_bootstrap_identity_changes_with_repository_configuration(tmp_path):
    from deb_dev_builder.core.apt_manager import APTManager
    from deb_dev_builder.core.chroot_manager import ChrootManager
    root = tmp_path / "root"
    for name in ("etc/apt", "usr/bin"):
        (root / name).mkdir(parents=True)
    for name in ("etc/os-release", "usr/bin/dpkg", "usr/bin/apt-get"):
        (root / name).touch()
    apt = APTManager(ChrootManager(root), {"mirror": "https://first.test", "components": ["main"]})
    apt._write_bootstrap_marker("trixie", "amd64")
    assert apt._is_bootstrapped_rootfs("trixie", "amd64")
    apt.config["mirror"] = "https://other.test"
    assert not apt._is_bootstrapped_rootfs("trixie", "amd64")


@pytest.mark.parametrize("matching_kernel", [True, False])
def test_installer_module_udebs_match_the_original_initrd_kernel(tmp_path, monkeypatch, matching_kernel):
    import io
    import hashlib
    from deb_dev_builder.core.installer_media import LiveInstallerMedia
    config = {"suite": "trixie", "architecture": "amd64"}
    policy = load_runtime(config)["installer"]
    policy["offline_package_sets"] = {}
    policy["cache_directory"] = str(tmp_path / "cache")
    data = b"udeb"
    names = ["live-installer", "nic-modules-6.12-test-di", "nic-modules-6.1-old-di"]
    if not matching_kernel:
        names.remove("nic-modules-6.12-test-di")
    index = "\n\n".join(f"Package: {name}\nFilename: pool/{name}.udeb\nSHA256: {hashlib.sha256(data).hexdigest()}" for name in names)
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("Packages.gz"):
            return io.BytesIO(gzip.compress(index.encode()))
        if url.endswith("/Release"):
            return io.BytesIO(b"Architecture: amd64\nComponent: main\n")
        return io.BytesIO(data)
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    staging = tmp_path / "iso"
    (staging / "install").mkdir(parents=True)
    entry = Entry("lib/modules/6.12-test", b"", stat.S_IFDIR | 0o755)
    (staging / "install/initrd.gz").write_bytes(pack_initrd({entry.name: entry}))
    media = LiveInstallerMedia(config, policy)
    monkeypatch.setattr(media, "_archive_tool", lambda tool, staging, args, output: output.write_text("Release\n"))
    if not matching_kernel:
        with pytest.raises(ValueError, match="no matching module udebs"):
            media.prepare(staging, None)
        assert not any(url.endswith(".udeb") for url in calls)
        return
    media.prepare(staging, None)
    assert not any("6.1-old" in url for url in calls)
    assert any("6.12-test" in url for url in calls)


def test_calamares_consumes_the_shared_live_removal_inventory(tmp_path):
    from deb_dev_builder.core.chroot_manager import ChrootManager
    from deb_dev_builder.core.customizer import SystemCustomizer
    config = {"with_calamares": True, "live_remove_packages": ["installer", "live-helper"]}
    customizer = SystemCustomizer(ChrootManager(tmp_path, mode="real"), config)
    customizer.configure_calamares()
    destination = tmp_path / load_runtime(config)["customization"]["calamares"]["packages_config"]
    policy = json.loads(destination.read_text())
    assert policy["operations"] == [{"remove": config["live_remove_packages"]}]
    assert policy["skip_if_no_internet"] is False and policy["update_db"] is False


def test_binary_overlay_preserves_links_without_writing_through_external_parents(tmp_path):
    source = tmp_path / "overlay"
    source.mkdir()
    (source / "link").symlink_to("/usr/share/guest")
    (source / "directory").mkdir()
    (source / "directory/file").write_bytes(b"custom")
    config = {"runtime": {"artifacts": {"iso": {"binary_overlays": [{"source": str(source), "destination": "."}]}}}}
    engine = ISOEngine(tmp_path / "work", tmp_path / "root", "out", config, "mock", None)
    engine.iso_staging.mkdir(parents=True)
    (engine.iso_staging / "link").write_text("previous regular file")
    engine._apply_binary_overlays()
    assert (engine.iso_staging / "link").readlink() == Path("/usr/share/guest")
    assert (engine.iso_staging / "directory/file").read_bytes() == b"custom"
    outside = tmp_path / "outside"
    outside.mkdir()
    # A path already seeded by another overlay must not redirect a later copy.
    (engine.iso_staging / "directory/file").unlink()
    (engine.iso_staging / "directory").rmdir()
    (engine.iso_staging / "directory").symlink_to(outside, target_is_directory=True)
    # The symlink itself is replaced by the declared directory, not followed.
    engine._apply_binary_overlays()
    assert not list(outside.iterdir())
    assert not (engine.iso_staging / "directory").is_symlink()
