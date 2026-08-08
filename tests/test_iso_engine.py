import pytest
from pathlib import Path
from deb_dev_builder.core.toolchain_manager import ToolchainManager
from deb_dev_builder.core.iso_engine import ISOEngine

class TestISOEngine:
    def test_mock_iso_build(self, tmp_path):
        workdir = tmp_path / "amd64"
        target_root = workdir / "chroot"
        toolchain = ToolchainManager(workdir, mode="mock")
        engine = ISOEngine(workdir, target_root, "test-deb-dev", {"architecture": "amd64"}, mode="mock", toolchain=toolchain)
        iso_path = engine.build_iso()
        assert isinstance(iso_path, Path)
        assert iso_path.name.endswith(".iso")
