import pytest
from pathlib import Path
from deb_dev_builder.core.config_loader import ConfigLoader
from deb_dev_builder.core.path_utils import resolve_from_project

@pytest.fixture
def config_root():
    return resolve_from_project("configs")

class TestConfigLoader:
    def test_load_global_config(self, config_root):
        loader = ConfigLoader(config_root)
        config = loader.assemble_build_config(
            global_config_path=config_root / "global_build.json",
            architecture="amd64",
            distro="debian-12",
        )
        assert config.get("architecture") == "amd64" or config.get("suite") == "bookworm"

    def test_package_profiles_exist(self, config_root):
        required = [
            "base", "audio", "bluetooth", "browsers", "chat", "cloud-tools",
            "desktop-apps", "dev-tools", "development", "filesystems", "gaming",
            "graphics", "ide", "multimedia", "multimedia-editing", "network-shares",
            "network-tools", "networking", "office", "printing", "productivity",
            "security", "system-utils", "virtualization", "wayland", "xorg"
        ]
        for name in required:
            path = config_root / "packages" / f"{name}.json"
            assert path.exists(), f"Missing package profile: {name}.json"
