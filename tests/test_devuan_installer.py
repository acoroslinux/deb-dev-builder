import gzip
import hashlib
import io

import pytest

from deb_dev_builder.core.initrd_editor import Entry, pack_initrd, unpack_initrd
from deb_dev_builder.core.installer_media import LiveInstallerMedia
from deb_dev_builder.core.installer_source import default_preseed, init_preseed, offline_package_sets
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.runtime_config import load_runtime


def configuration(mode="live", init="sysvinit"):
    return {"architecture": "amd64", "dpkg_arch": "amd64", "base_distro": "devuan",
            "suite": "excalibur", "mirror": "http://deb.devuan.org/merged",
            "di_mode": mode, "init_system": init, "with_debian_installer": True}


@pytest.mark.parametrize("mode,source", [("live", "cdrom"), ("netinstall", "netboot"), ("netboot", "netboot")])
def test_native_images_and_merged_package_origins_are_separate(tmp_path, mode, source):
    config = configuration(mode)
    engine = ISOEngine(tmp_path, tmp_path / "root", "native", config, "mock", None)
    url = engine._installer_image_url("initrd.gz")
    assert url == f"https://pkgmaster.devuan.org/devuan/dists/excalibur/main/installer-amd64/current/images/{source}/initrd.gz"
    media = LiveInstallerMedia(config, engine.runtime["installer"])
    assert media.mirror == "https://pkgmaster.devuan.org/merged"
    assert engine.runtime["installer"]["release_fields"]["Origin"] == "Devuan"
    assert engine.runtime["installer"]["cache_directory"] == "cache/installer/devuan/{suite}/{architecture}"


def test_image_and_archive_mirrors_can_be_overridden_independently(tmp_path):
    config = {**configuration(), "installer_mirror": "https://packages.example/merged",
              "installer_image_mirror": "https://images.example/devuan"}
    engine = ISOEngine(tmp_path, tmp_path / "root", "native", config, "mock", None)
    assert engine._installer_image_url().startswith("https://images.example/devuan/")
    assert LiveInstallerMedia(config, engine.runtime["installer"]).mirror == "https://packages.example/merged"


@pytest.mark.parametrize("init,packages", [("sysvinit", ["sysvinit", "sysv-rc", "sysvinit-utils"]), ("openrc", ["openrc"]), ("runit", ["runit-init"])])
def test_native_init_answer_and_offline_packages_follow_selected_init(init, packages):
    config = configuration(init=init)
    policy = load_runtime(config)["installer"]
    assert init_preseed(config, policy) == f"d-i choose-init/select_init select {init}\nd-i choose-init/select_init seen false\n"
    assert offline_package_sets(config, policy)[-3:] == list(policy["init_packages"].values())
    unattended = {**policy, "init_ask": False}
    assert init_preseed(config, unattended) == f"d-i choose-init/select_init select {init}\n"
    assert offline_package_sets(config, unattended)[-1] == packages
    assert "choose-init" in policy["live_modules"]
    with pytest.raises(ValueError, match="does not support"):
        init_preseed({**config, "init_system": "systemd"}, policy)


def test_default_network_profile_uses_devuan_answers_and_preserves_explicit_preseed():
    config = configuration("netinstall")
    policy = load_runtime(config)["installer"]
    source = "d-i mirror/http/hostname string deb.debian.org\nd-i mirror/http/directory string /debian\nd-i passwd/make-user boolean true\n"
    result = default_preseed(source, config, policy)
    assert "deb.debian.org" not in result
    assert "d-i mirror/http/directory string /merged" in result
    assert "d-i mirror/suite string excalibur" in result
    assert "d-i choose-init/select_init select sysvinit" in result
    assert "d-i choose-init/select_init seen false" in result
    assert "d-i passwd/make-user boolean true" in result
    assert default_preseed(source, {**config, "preseed": "/custom.cfg"}, policy) == source


@pytest.mark.parametrize("distro", ["devuan-5", "devuan-6", "devuan-testing", "devuan-ceres"])
def test_profile_composition_keeps_native_distribution_policy(distro):
    build = BuildOrchestrator(distro=distro, with_debian_installer=True, mode="mock")
    assert build.validate()["valid"]
    assert build.runtime["installer"]["image_mirror"] == "https://pkgmaster.devuan.org/devuan"
    assert build.runtime["installer"]["release_fields"]["Label"] == "Devuan"


def test_merged_repository_uses_both_native_and_inherited_udebs(tmp_path, monkeypatch):
    config = configuration()
    policy = load_runtime(config)["installer"]
    policy["cache_directory"] = str(tmp_path / "cache")
    policy["offline_package_sets"] = {}
    payloads = {"live-installer": b"Debian inherited component", "choose-init": b"Devuan native component"}
    records = []
    downloads = {}
    for package, data in payloads.items():
        origin = "DEVUAN" if package == "choose-init" else "DEBIAN"
        path = f"pool/{origin}/main/{package[0]}/{package}/{package}_1_all.udeb"
        downloads[path] = data
        records.append(f"Package: {package}\nVersion: 1\nArchitecture: all\nFilename: {path}\nSHA256: {hashlib.sha256(data).hexdigest()}\n")
    index = "\n".join(records)
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        assert url.startswith("https://pkgmaster.devuan.org/merged/")
        if url.endswith("Packages.gz"):
            return io.BytesIO(gzip.compress(index.encode()))
        if url.endswith("/Release"):
            return io.BytesIO(b"Architecture: amd64\nComponent: main\n")
        return io.BytesIO(downloads[url.split("/merged/", 1)[1]])

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    media = LiveInstallerMedia(config, policy)
    release_commands = []

    def archive(toolchain, staging, args, output):
        release_commands.append(args)
        output.write_text("Origin: Devuan\n")

    monkeypatch.setattr(media, "_archive_tool", archive)
    staging = tmp_path / "iso"
    staging.mkdir()
    media.prepare(staging, None)
    assert len([url for url in calls if url.endswith(".udeb")]) == 2
    assert any("APT::FTPArchive::Release::Origin=Devuan" in args for args in release_commands)
    assert set((staging / ".disk/udeb_include").read_text().split()) == set(payloads)
    local = (staging / "dists/excalibur/main/debian-installer/binary-amd64/Packages").read_text()
    assert "Filename: pool-udeb/main/c/choose-init/" in local
    assert "Filename: pool-udeb/main/l/live-installer/" in local


def test_live_init_answer_is_embedded_with_mandatory_hooks(tmp_path):
    config = configuration(init="runit")
    runtime = load_runtime(config)
    original = Entry("lib/modules/native-devuan-kernel", b"", 0o40755)
    for name in ("initrd.gz", "gtk/initrd.gz"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(pack_initrd({original.name: original}))
    LiveInstallerMedia(config, runtime["installer"]).embed_preseed(tmp_path)
    for name in ("initrd.gz", "gtk/initrd.gz"):
        entries = unpack_initrd((tmp_path / name).read_bytes())
        assert b"d-i choose-init/select_init select runit\n" in entries["preseed.cfg"].data
        assert b"d-i choose-init/select_init seen false\n" in entries["preseed.cfg"].data
        assert runtime["installer"]["target_boot_hook"] in entries
        assert original.name in entries
