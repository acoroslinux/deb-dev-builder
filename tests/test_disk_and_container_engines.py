import pytest
import hashlib
import json
import tarfile
from pathlib import Path
from deb_dev_builder.core.disk_engine import DiskEngine
from deb_dev_builder.core.container_engine import ContainerEngine

def test_mock_disk_engine(tmp_path):
    workdir = tmp_path / "workdir"
    target_root = tmp_path / "chroot"
    workdir.mkdir()
    target_root.mkdir()

    engine = DiskEngine(
        workdir=workdir,
        target_root=target_root,
        output_name="test-disk",
        config={},
        mode="mock"
    )
    result = engine.build_disk_image("img")
    assert isinstance(result, Path)
    assert result.name == "test-disk.img"

def test_mock_container_engine(tmp_path):
    target_root = tmp_path / "chroot"
    target_root.mkdir()

    engine = ContainerEngine(
        target_root=target_root,
        output_name="test-container",
        config={},
        mode="mock"
    )
    result = engine.build_oci_archive()
    assert isinstance(result, Path)
    assert result.name == "test-container.oci.tar"

@pytest.mark.parametrize("image_format", ["img", "qcow2", "vdi", "vmdk"])
def test_mock_disk_output_formats_and_absolute_paths(tmp_path, image_format):
    engine = DiskEngine(tmp_path / "work", tmp_path / "root", str(tmp_path / "artifacts" / "machine.iso"), {}, "mock")
    result = engine.build_disk_image(image_format)
    assert result == tmp_path / "artifacts" / f"machine.{image_format}"

def test_real_oci_archive_has_valid_image_layout(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text("ID=debian\n")
    result = ContainerEngine(root, str(tmp_path / "debian-root"), {"dpkg_arch": "amd64"}, "real").build_oci_archive()

    with tarfile.open(result) as archive:
        names = set(archive.getnames())
        assert {"oci-layout", "index.json", "blobs"} <= names
        index = json.load(archive.extractfile("index.json"))
        manifest_digest = index["manifests"][0]["digest"].split(":", 1)[1]
        manifest_data = archive.extractfile(f"blobs/sha256/{manifest_digest}").read()
        assert hashlib.sha256(manifest_data).hexdigest() == manifest_digest
        manifest = json.loads(manifest_data)
        assert manifest["schemaVersion"] == 2
        assert len(manifest["layers"]) == 1

def test_platform_boot_artifacts_are_injected_at_declared_offsets(tmp_path):
    root = tmp_path / "root"
    source = root / "boot" / "u-boot" / "idbloader.img"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"U-BOOT")
    image = tmp_path / "disk.img"
    image.write_bytes(b"\0" * 65536)
    engine = DiskEngine(
        tmp_path / "work",
        root,
        "arm",
        {"hardware": {"boot_artifacts": [
            {"source": "/boot/u-boot/idbloader.img", "offset_kib": 32, "required": True}
        ]}},
        "real",
    )
    engine._inject_boot_artifacts(image)
    with image.open("rb") as stream:
        stream.seek(32 * 1024)
        assert stream.read(6) == b"U-BOOT"
