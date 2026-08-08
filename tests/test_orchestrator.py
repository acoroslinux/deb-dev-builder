import pytest
from pathlib import Path
from deb_dev_builder.core.orchestrator import BuildOrchestrator

def make_orchestrator(tmp_path=None, **kwargs) -> BuildOrchestrator:
    defaults = dict(
        arch="amd64",
        mode="mock",
        distro="debian-12",
        init_system="systemd",
        desktop=None,
        output_format="iso",
    )
    defaults.update(kwargs)
    orch = BuildOrchestrator(**defaults)
    if tmp_path:
        orch.workdir = tmp_path / orch.arch
        orch.target_root = orch.workdir / "chroot"
    return orch

class TestOrchestrator:
    def test_construction(self):
        orch = make_orchestrator()
        assert orch.arch == "amd64"
        assert orch.distro == "debian-12"

    def test_validate(self):
        orch = make_orchestrator()
        report = orch.validate()
        assert report.get("valid") is True

    def test_mock_build_debian(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="debian-12", desktop="gnome")
        result = orch.build()
        assert isinstance(result, Path)

    def test_mock_build_devuan(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="devuan-5", init_system="openrc", desktop="xfce")
        result = orch.build()
        assert isinstance(result, Path)

    def test_mock_build_tarball(self, tmp_path):
        orch = make_orchestrator(tmp_path=tmp_path, distro="debian-12", output_format="tarball")
        result = orch.build()
        assert isinstance(result, Path)
        assert result.name.endswith(".tar.xz")
