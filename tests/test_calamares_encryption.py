"""Encrypted installations need both formatting tools and early-boot support."""
import pytest
import yaml
import copy
import io
import os
import shutil
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace

from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.path_utils import resolve_from_project


def test_calamares_build_includes_luks_creation_and_early_boot_tools():
    build = BuildOrchestrator(arch="amd64", distro="debian-13", desktop="kde", with_calamares=True, mode="mock")
    assert {"cryptsetup", "cryptsetup-bin", "cryptsetup-initramfs", "lvm2", "systemd-cryptsetup"} <= set(build.config["software"])


def test_debian_live_installer_has_the_same_target_storage_support():
    build = BuildOrchestrator(arch="amd64", distro="debian-13", desktop="xfce", with_debian_installer=True, mode="mock")
    assert {"cryptsetup", "cryptsetup-bin", "cryptsetup-initramfs", "lvm2", "systemd-cryptsetup"} <= set(build.config["software"])
    assert not set(build.config["software"]).intersection(build.runtime["build"]["packages"]["live_only"]) & {"lvm2", "cryptsetup-initramfs"}


@pytest.mark.parametrize("variant", ["systemd", "non-systemd"])
def test_encrypted_boot_initramfs_is_generated_after_target_configuration(variant):
    base = resolve_from_project("configs/custom_files/calamares")
    settings = yaml.safe_load((base / variant / "settings.conf").read_text())
    jobs = next(stage["exec"] for stage in settings["sequence"] if "exec" in stage)
    assert jobs.index("fstab") < jobs.index("initramfscfg")
    assert jobs.index("packages") < jobs.index("initramfscfg") < jobs.index("initramfs")
    assert jobs.index("initramfs") < jobs.index("grubcfg") < jobs.index("bootloader")
    policy = yaml.safe_load((base / "modules/initramfs.conf").read_text())
    assert policy["kernel"] == "all"
    assert policy["be_unsafe"] is False


def test_storage_packages_do_not_require_the_new_systemd_split_on_bookworm():
    build = BuildOrchestrator(arch="amd64", distro="debian-12", desktop="xfce", with_calamares=True, mode="mock")
    assert {"cryptsetup-initramfs", "lvm2"} <= set(build.config["software"])
    assert "systemd-cryptsetup" not in build.config["software"]


def test_grub_configuration_targets_the_file_consumed_by_grub_install():
    policy = yaml.safe_load(resolve_from_project("configs/custom_files/calamares/modules/grubcfg.conf").read_text())
    # Calamares' alternate filename is 00calamares, without Debian's .cfg suffix.
    assert policy["prefer_grub_d"] is False


@pytest.fixture(scope="module")
def native_grubcfg_source():
    """Exercise the actual Debian Calamares module when its DEB is cached."""
    packages = sorted(resolve_from_project("cache/debian").glob("*/amd64/apt/archives/calamares_*.deb"))
    if not packages or not shutil.which("dpkg-deb"):
        pytest.skip("Native Calamares DEB required for installer contract test")
    payload = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(packages[-1])],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        member = next(entry for entry in archive if entry.name.endswith("/calamares/modules/grubcfg/main.py"))
        return archive.extractfile(member).read().decode()


@pytest.mark.parametrize("encrypted,separate_boot,expected", [
    (True, None, "y"),
    (True, "encrypted", "y"),
    (True, "plain", ""),
    (False, None, ""),
])
def test_native_grubcfg_exports_cryptodisk_for_encrypted_boot_only(
        tmp_path, monkeypatch, native_grubcfg_source, encrypted, separate_boot, expected):
    policy = yaml.safe_load(resolve_from_project("configs/custom_files/calamares/modules/grubcfg.conf").read_text())
    lib = ModuleType("libcalamares")
    lib.job = SimpleNamespace(configuration=copy.deepcopy(policy))
    lib.utils = SimpleNamespace(gettext_path=lambda: str(tmp_path), gettext_languages=lambda: ["en"],
                                target_env_call=lambda command: 1)
    monkeypatch.setitem(sys.modules, "libcalamares", lib)
    namespace = {"__name__": "native_grubcfg"}
    exec(compile(native_grubcfg_source, "calamares/grubcfg/main.py", "exec"), namespace)
    root = tmp_path / "target"
    (root / "etc/default/grub.d").mkdir(parents=True)
    (root / "etc/default/grub").write_text("GRUB_TIMEOUT=3\n")
    partition = {"mountPoint": "/", "fs": "ext4"}
    if encrypted:
        partition.update(luksMapperName="cryptroot", luksUuid="test-root-uuid")
    partitions = [partition]
    if separate_boot:
        boot = {"mountPoint": "/boot", "fs": "ext4"}
        if separate_boot == "encrypted":
            boot.update(luksMapperName="cryptboot", luksUuid="test-boot-uuid")
        partitions.append(boot)
    namespace["modify_grub_default"](partitions, str(root), "Deb-Dev")
    environment = os.environ.copy()
    environment.pop("GRUB_ENABLE_CRYPTODISK", None)
    result = subprocess.run(["sh", "-ec", '. "$1"; printf "%s" "${GRUB_ENABLE_CRYPTODISK-}"',
                             "check", str(root / "etc/default/grub")],
                            env=environment, check=True, capture_output=True, text=True)
    assert result.stdout == expected
    assert not (root / "etc/default/grub.d/00calamares").exists()
