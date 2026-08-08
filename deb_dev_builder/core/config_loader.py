import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from deb_dev_builder.core.path_utils import resolve_from_project

class ConfigLoaderError(Exception):
    """Exception raised for configuration loading errors."""
    pass

class ConfigLoader:
    def __init__(self, config_root: Optional[Path] = None):
        self.config_root = config_root or resolve_from_project("configs")

    def load_json(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise ConfigLoaderError(f"Configuration file not found: {path}")
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigLoaderError(f"Invalid JSON in {path}: {e}")
        except Exception as e:
            raise ConfigLoaderError(f"Error loading {path}: {e}")

    def load_profile(self, category: str, profile_name: str) -> Dict[str, Any]:
        path = self.config_root / category / f"{profile_name}.json"
        try:
            return self.load_json(path)
        except ConfigLoaderError:
            return {}

    def _merge_dicts(self, base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
        result = base.copy()
        for key, value in update.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_dicts(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                existing = list(result[key])
                extras = [item for item in value if item not in existing]
                result[key] = existing + extras
            else:
                result[key] = value
        return result

    def assemble_build_config(
        self,
        global_config_path: Path,
        architecture: str,
        distro: str,
        init_system: Optional[str] = None,
        desktop: Optional[str] = None,
        kernel: Optional[str] = None,
        bootloader: Optional[str] = None,
        variant: Optional[str] = None,
        package_profiles: Optional[List[str]] = None,
        service_profiles: Optional[List[str]] = None,
        repo_profiles: Optional[List[str]] = None,
        live_profile: Optional[str] = None,
    ) -> Dict[str, Any]:

        config = {
            "packages": [],
            "groups": [],
            "services": {"enable": [], "disable": []},
            "repos": [],
            "kernel_packages": [],
            "bootloader": {},
            "live_user": {},
            "distro_info": {},
            "arch_info": {},
            "system": {},
            "boot": {},
            "variant_info": {}
        }

        # 1. Global config
        if global_config_path.exists():
            config = self._merge_dicts(config, self.load_json(global_config_path))

        # 2. Distro
        if distro:
            config = self._merge_dicts(config, self.load_profile("distros", distro))

        # 3. Init system
        if init_system:
            config = self._merge_dicts(config, self.load_profile("init-systems", init_system))

        # 4. Architecture
        config = self._merge_dicts(config, self.load_profile("architectures", architecture))

        # 5. Variant
        if variant:
            config = self._merge_dicts(config, self.load_profile("variants", variant))

        # 6. Desktop
        if desktop:
            config = self._merge_dicts(config, self.load_profile("desktops", desktop))

        # 7. Kernel
        if kernel:
            config = self._merge_dicts(config, self.load_profile("kernels", kernel))

        # 8. Bootloader
        if bootloader:
            config = self._merge_dicts(config, self.load_profile("bootloaders", bootloader))

        # 9. Base packages
        config = self._merge_dicts(config, self.load_profile("packages", "base"))

        # 10. Package profiles
        if package_profiles:
            for profile in package_profiles:
                config = self._merge_dicts(config, self.load_profile("packages", profile))

        # 11. Service profiles
        if service_profiles:
            for profile in service_profiles:
                config = self._merge_dicts(config, self.load_profile("services", profile))

        # 12. Repo profiles
        if repo_profiles:
            for profile in repo_profiles:
                config = self._merge_dicts(config, self.load_profile("repos", profile))

        # 13. Live User profile
        if live_profile:
            config["live_user"] = self._merge_dicts(config.get("live_user", {}), self.load_profile("live-users", live_profile))

        # 14. Deduplicate lists
        for key in ["packages", "groups", "repos", "kernel_packages"]:
            if key in config and isinstance(config[key], list):
                config[key] = list(dict.fromkeys(config[key]))

        if "services" in config:
            for state in ["enable", "disable"]:
                if state in config["services"] and isinstance(config["services"][state], list):
                    config["services"][state] = list(dict.fromkeys(config["services"][state]))

        return config
