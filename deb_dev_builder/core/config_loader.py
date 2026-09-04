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
        path = Path(path)
        if not path.exists():
            raise ConfigLoaderError(f"Configuration file not found: {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ConfigLoaderError(f"Configuration root must be an object: {path}")
            return data
        except json.JSONDecodeError as e:
            raise ConfigLoaderError(f"Invalid JSON in {path}: {e}")
        except ConfigLoaderError:
            raise
        except OSError as e:
            raise ConfigLoaderError(f"Error loading {path}: {e}")

    def load_profile(self, category: str, profile_name: str, *, required: bool = True) -> Dict[str, Any]:
        if not profile_name or Path(str(profile_name)).name != str(profile_name):
            raise ConfigLoaderError(f"Invalid {category} profile name: {profile_name!r}")
        path = self.config_root / category / f"{profile_name}.json"
        if not path.exists() and not required:
            return {}
        return self.load_json(path)

    @staticmethod
    def _normalize_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
        """Translate the JSON vocabulary to the runtime configuration keys."""
        normalized = dict(profile)
        packages = normalized.pop("packages", None)
        if packages is not None:
            if not isinstance(packages, list) or not all(isinstance(p, str) for p in packages):
                raise ConfigLoaderError("The 'packages' field must be a list of strings")
            normalized["software"] = packages
        return normalized

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

    @staticmethod
    def _architecture_kernel_packages(kernel: Optional[str], dpkg_arch: str) -> List[str]:
        suffixes = {
            "amd64": "amd64",
            "i386": "686-pae",
            "arm64": "arm64",
            "armhf": "armmp",
            "riscv64": "riscv64",
        }
        suffix = suffixes.get(dpkg_arch, dpkg_arch)
        flavor = (kernel or "generic").lower()
        if flavor in {"rt", "kernel-rt"}:
            return [f"linux-image-rt-{suffix}", f"linux-headers-rt-{suffix}"]
        if flavor == "cloud":
            return [f"linux-image-cloud-{suffix}", f"linux-headers-cloud-{suffix}"]
        return [f"linux-image-{suffix}", f"linux-headers-{suffix}"]

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
        hardware_profile: Optional[str] = None,
        vm_profile: Optional[str] = None,
    ) -> Dict[str, Any]:

        architecture = {"x86_64": "amd64", "i686": "i386"}.get(architecture, architecture)

        config = {
            "software": [],
            "exclude_packages": [],
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

        # Project-wide customization defaults are part of the catalog, not a
        # dead standalone file. More specific profiles and CLI overrides win.
        base_customizations = self.config_root / "base_customizations.json"
        if base_customizations.exists():
            config = self._merge_dicts(config, self._normalize_profile(self.load_json(base_customizations)))

        # 1. Global config
        if global_config_path.exists():
            config = self._merge_dicts(config, self._normalize_profile(self.load_json(global_config_path)))
        else:
            raise ConfigLoaderError(f"Configuration file not found: {global_config_path}")

        # 2. Distro
        if distro:
            config = self._merge_dicts(config, self._normalize_profile(self.load_profile("system", distro)))

        # 3. Init system
        if init_system:
            config = self._merge_dicts(config, self._normalize_profile(self.load_profile("system", init_system)))

        # 4. Architecture
        arch_profile = self.load_profile("architectures", architecture)
        if arch_profile.get("arch") != architecture:
            raise ConfigLoaderError(f"Architecture profile {architecture!r} has mismatched 'arch' value")
        config = self._merge_dicts(config, self._normalize_profile(arch_profile))
        config["architecture"] = arch_profile["arch"]
        config["dpkg_arch"] = arch_profile.get("dpkg_arch", architecture)

        if hardware_profile:
            hardware = self.load_profile("hardware", hardware_profile)
            hardware_arch = hardware.get("architecture")
            aliases = {"x86_64": "amd64", "i686": "i386"}
            if aliases.get(hardware_arch, hardware_arch) != aliases.get(architecture, architecture):
                raise ConfigLoaderError(
                    f"Hardware profile {hardware_profile!r} requires architecture {hardware_arch!r}"
                )
            config["hardware_profile"] = hardware_profile
            config["hardware"] = hardware
            if hardware.get("packages"):
                config = self._merge_dicts(config, self._normalize_profile({"packages": hardware["packages"]}))

        if vm_profile:
            vm = self.load_profile("vm", vm_profile)
            supported_arches = vm.get("supported_architectures", [])
            if architecture not in supported_arches:
                raise ConfigLoaderError(
                    f"VM profile {vm_profile!r} does not support architecture {architecture!r}"
                )
            config["vm_profile"] = vm_profile
            config["vm"] = vm
            config = self._merge_dicts(config, self._normalize_profile({"packages": vm.get("packages", [])}))

        # 5. Variant
        if variant:
            config = self._merge_dicts(config, self._normalize_profile(self.load_profile("system", variant)))

        # 6. Desktop
        if desktop:
            desktop_profile = self.load_profile("desktops", desktop)
            config = self._merge_dicts(config, self._normalize_profile(desktop_profile))
            session_type = desktop_profile.get("session_type", "x11")
            if session_type in {"x11", "both"}:
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("software", "xorg")))
            if session_type == "wayland":
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("software", "wayland")))

        # 7. Kernel
        if kernel:
            config = self._merge_dicts(config, self._normalize_profile(self.load_profile("system", kernel)))

        # 8. Bootloader
        if bootloader:
            if isinstance(bootloader, dict):
                config = self._merge_dicts(config, {"bootloader": bootloader})
            else:
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("boot", bootloader)))

        # 9. Base packages
        config = self._merge_dicts(config, self._normalize_profile(self.load_profile("software", "base")))

        # 10. Package profiles
        if package_profiles:
            for profile in package_profiles:
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("software", profile)))

        # 11. Service profiles
        if service_profiles:
            for profile in service_profiles:
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("services", profile)))

        # 12. Repo profiles
        if repo_profiles:
            for profile in repo_profiles:
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("repos", profile)))

        # 13. Live User profile
        if live_profile:
            live_user_profile = self.load_profile("live-users", live_profile).get("live_user", {})
            config["live_user"] = self._merge_dicts(config.get("live_user", {}), live_user_profile)
            # A named live-user profile is authoritative for group membership.
            # Appending here would make the low-privilege guest inherit sudo
            # from base_customizations.json.
            if "groups" in live_user_profile:
                config["live_user"]["groups"] = list(live_user_profile["groups"])

        # 14. Deduplicate lists
        for key in ["software", "exclude_packages", "groups", "repos", "kernel_packages"]:
            if key in config and isinstance(config[key], list):
                config[key] = list(dict.fromkeys(config[key]))

        if "services" in config:
            for state in ["enable", "disable"]:
                if state in config["services"] and isinstance(config["services"][state], list):
                    config["services"][state] = list(dict.fromkeys(config["services"][state]))

        # Kernel profile files describe a flavor; Debian package names remain
        # architecture-specific. Replace any profile placeholder packages here.
        software = [
            package for package in config.get("software", [])
            if not package.startswith(("linux-image-", "linux-headers-"))
        ]
        for package in self._architecture_kernel_packages(kernel, config["dpkg_arch"]):
            if package not in software:
                software.append(package)
        excluded = set(config.get("exclude_packages", []))
        config["software"] = [package for package in software if package not in excluded]

        return config

    def audit_catalog(self) -> List[str]:
        """Return semantic errors found across every JSON configuration file."""
        errors: List[str] = []
        allowed = {
            "architectures": {"arch", "dpkg_arch"},
            "boot": {"packages", "bootloader"},
            "desktops": {"desktop", "display_manager", "session_type", "supported_suites", "packages"},
            "hardware": {
                "name", "architecture", "bootloader", "output_format", "status", "notes",
                "packages", "partition_start_mib", "firmware_tree", "boot_artifacts",
                "required_files",
            },
            "live-users": {"live_user"},
            "services": {"services", "packages"},
            "vm": {"name", "output_format", "firmware", "supported_architectures", "packages"},
            "software": {"packages", "offline_repo_packages", "supported_suites"},
            "system": {
                "distro", "distro_name", "base_distro", "system", "suite", "mirror",
                "security_mirror", "security_suite", "updates_mirror", "updates_suite",
                "backports_mirror", "backports_suite", "components", "init_system",
                "kernel", "variant", "packages", "exclude_packages", "supported_suites",
            },
        }
        identity_keys = {
            "architectures": "arch", "desktops": "desktop", "system": None,
        }
        for category, keys in allowed.items():
            category_dir = self.config_root / category
            for path in sorted(category_dir.glob("*.json")):
                try:
                    data = self.load_json(path)
                except ConfigLoaderError as exc:
                    errors.append(str(exc))
                    continue
                unknown = sorted(set(data) - keys)
                if unknown:
                    errors.append(f"{path}: unknown keys: {', '.join(unknown)}")
                identity = identity_keys.get(category)
                if identity and data.get(identity) != path.stem:
                    errors.append(f"{path}: '{identity}' must match filename")
                if category == "system":
                    role_keys = [key for key in ("distro", "init_system", "kernel", "variant") if key in data]
                    if len(role_keys) != 1:
                        errors.append(f"{path}: exactly one system profile role is required")
                    elif data[role_keys[0]] != path.stem:
                        errors.append(f"{path}: '{role_keys[0]}' must match filename")
                    elif role_keys[0] == "distro":
                        required = {"distro_name", "base_distro", "system", "suite", "mirror", "components"}
                        missing = sorted(required - set(data))
                        if missing:
                            errors.append(f"{path}: missing distro keys: {', '.join(missing)}")
                        if data.get("base_distro") not in {"debian", "devuan"}:
                            errors.append(f"{path}: base_distro must be 'debian' or 'devuan'")
                        distro_system = data.get("system")
                        if not isinstance(distro_system, dict) or set(distro_system) - {"hostname", "timezone", "locale", "iso_label", "apt_cache"}:
                            errors.append(f"{path}: invalid distro system settings")
                        elif any(not isinstance(distro_system.get(key), str) or not distro_system.get(key) for key in ("hostname", "iso_label")):
                            errors.append(f"{path}: system.hostname and system.iso_label are required")
                    elif role_keys[0] == "init_system":
                        if data["init_system"] not in {"systemd", "sysvinit", "openrc", "runit"}:
                            errors.append(f"{path}: unsupported init system")
                        if not data.get("packages"):
                            errors.append(f"{path}: init system must install packages")
                    elif role_keys[0] == "kernel" and data["kernel"] not in {"generic", "rt", "cloud"}:
                        errors.append(f"{path}: unsupported kernel flavor")
                    elif role_keys[0] == "variant" and not (data.get("packages") or data.get("exclude_packages")):
                        errors.append(f"{path}: variant has no package effect")
                for list_key in ("packages", "exclude_packages", "offline_repo_packages", "components", "supported_suites"):
                    values = data.get(list_key)
                    if values is not None:
                        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                            errors.append(f"{path}: '{list_key}' must be a list of non-empty strings")
                        elif len(values) != len(set(values)):
                            errors.append(f"{path}: duplicate values in '{list_key}'")

                if category == "architectures":
                    if not isinstance(data.get("dpkg_arch"), str) or not data.get("dpkg_arch"):
                        errors.append(f"{path}: 'dpkg_arch' must be a non-empty string")
                elif category == "boot":
                    bootloader_data = data.get("bootloader")
                    if not isinstance(bootloader_data, dict) or bootloader_data.get("type") != path.stem:
                        errors.append(f"{path}: bootloader.type must match filename")
                elif category == "desktops":
                    if data.get("session_type") not in {"x11", "wayland", "both"}:
                        errors.append(f"{path}: invalid or missing 'session_type'")
                    if not isinstance(data.get("display_manager"), str) or not data.get("display_manager"):
                        errors.append(f"{path}: 'display_manager' must be a non-empty string")
                elif category == "hardware":
                    if data.get("architecture") not in {"amd64", "x86_64", "i386", "i686", "aarch64", "armhf", "riscv64"}:
                        errors.append(f"{path}: unsupported hardware architecture")
                    if data.get("output_format") not in {"iso", "netboot", "img", "raw", "qcow2", "vdi", "vmdk", "vhd", "vhdx", "tarball", "container", "oci"}:
                        errors.append(f"{path}: unsupported hardware output_format")
                    if data.get("status", "stable") not in {"stable", "experimental"}:
                        errors.append(f"{path}: status must be 'stable' or 'experimental'")
                    if not isinstance(data.get("bootloader"), dict) or not isinstance(data["bootloader"].get("type"), str):
                        errors.append(f"{path}: bootloader.type must be a string")
                    if data.get("status") == "experimental" and not data.get("notes"):
                        errors.append(f"{path}: experimental hardware requires notes")
                    if "partition_start_mib" in data and (
                        not isinstance(data["partition_start_mib"], int) or data["partition_start_mib"] < 1
                    ):
                        errors.append(f"{path}: partition_start_mib must be a positive integer")
                    if "firmware_tree" in data and (
                        not isinstance(data["firmware_tree"], str) or not data["firmware_tree"].startswith("/")
                    ):
                        errors.append(f"{path}: firmware_tree must be an absolute target-root path")
                    required_files = data.get("required_files", [])
                    if not isinstance(required_files, list) or not all(
                        isinstance(required_file, str) and required_file.startswith("/")
                        for required_file in required_files
                    ):
                        errors.append(f"{path}: required_files must contain absolute target-root paths")
                    artifacts = data.get("boot_artifacts", [])
                    if not isinstance(artifacts, list):
                        errors.append(f"{path}: boot_artifacts must be a list")
                    else:
                        for artifact in artifacts:
                            if not isinstance(artifact, dict) or set(artifact) - {"source", "offset_kib", "required"}:
                                errors.append(f"{path}: invalid boot_artifacts entry")
                            elif not isinstance(artifact.get("source"), str) or not artifact["source"].startswith("/"):
                                errors.append(f"{path}: boot artifact source must be absolute")
                            elif not isinstance(artifact.get("offset_kib"), int) or artifact["offset_kib"] < 0:
                                errors.append(f"{path}: boot artifact offset_kib must be non-negative")
                elif category == "live-users":
                    live_user = data.get("live_user")
                    if not isinstance(live_user, dict) or not isinstance(live_user.get("name"), str) or not live_user.get("name"):
                        errors.append(f"{path}: live_user.name must be a non-empty string")
                    else:
                        groups = live_user.get("groups")
                        if not isinstance(groups, list) or not all(isinstance(group, str) and group for group in groups):
                            errors.append(f"{path}: live_user.groups must be a list of non-empty strings")
                        elif len(groups) != len(set(groups)):
                            errors.append(f"{path}: duplicate live_user groups")
                elif category == "services":
                    services = data.get("services")
                    if not isinstance(services, dict) or set(services) - {"enable", "disable"}:
                        errors.append(f"{path}: services must contain only enable/disable lists")
                    else:
                        for state in ("enable", "disable"):
                            values = services.get(state, [])
                            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                                errors.append(f"{path}: services.{state} must be a list of non-empty strings")
                            elif len(values) != len(set(values)):
                                errors.append(f"{path}: duplicate services.{state} values")
                elif category == "vm":
                    if data.get("output_format") not in {"qcow2", "vdi", "vmdk", "vhd", "vhdx", "raw", "img"}:
                        errors.append(f"{path}: invalid VM output_format")
                    if data.get("firmware") not in {"uefi", "bios"}:
                        errors.append(f"{path}: firmware must be 'uefi' or 'bios'")
                    arches = data.get("supported_architectures")
                    if not isinstance(arches, list) or not arches or not all(
                        arch in {"amd64", "i386", "aarch64", "armhf", "riscv64"} for arch in arches
                    ):
                        errors.append(f"{path}: invalid supported_architectures")

        global_path = self.config_root / "global_build.json"
        try:
            global_data = self.load_json(global_path)
            if set(global_data) - {"system", "boot", "hooks", "cleanup", "toolchain"}:
                errors.append(f"{global_path}: unknown top-level keys")
            system = global_data.get("system")
            if not isinstance(system, dict) or set(system) - {"hostname", "timezone", "locale", "iso_label", "apt_cache"}:
                errors.append(f"{global_path}: invalid system settings")
            boot = global_data.get("boot")
            if not isinstance(boot, dict) or set(boot) - {"kernel_params"}:
                errors.append(f"{global_path}: invalid boot settings")
            hooks = global_data.get("hooks", {})
            if not isinstance(hooks, dict) or set(hooks) - {"enabled", "directory", "chroot_directory"}:
                errors.append(f"{global_path}: invalid hooks settings")
            elif not isinstance(hooks.get("enabled", True), bool) or not isinstance(hooks.get("directory", "hooks"), str) or not isinstance(hooks.get("chroot_directory", "chroot"), str):
                errors.append(f"{global_path}: hooks.enabled/directory have invalid types")
            cleanup = global_data.get("cleanup", {})
            cleanup_keys = {"enabled", "apt_lists", "apt_archives", "logs", "temporary", "caches", "bytecode", "builder_metadata", "machine_identity"}
            if not isinstance(cleanup, dict) or set(cleanup) - cleanup_keys:
                errors.append(f"{global_path}: invalid cleanup settings")
            elif any(not isinstance(cleanup.get(key, True), bool) for key in cleanup_keys):
                errors.append(f"{global_path}: cleanup values must be booleans")
            toolchain = global_data.get("toolchain", {})
            if not isinstance(toolchain, dict) or set(toolchain) - {"backend", "tarball_url", "tarball_path", "tarball_sha256", "oci_registry", "oci_repository", "oci_reference"}:
                errors.append(f"{global_path}: invalid toolchain settings")
            elif toolchain.get("backend", "isolated-oci") not in {"auto", "isolated-tarball", "isolated-oci"}:
                errors.append(f"{global_path}: invalid toolchain.backend")
            for key in ("hostname", "timezone", "locale", "iso_label"):
                if not isinstance(system.get(key), str) or not system.get(key):
                    errors.append(f"{global_path}: system.{key} must be a non-empty string")
            if not isinstance(boot.get("kernel_params"), str) or not boot.get("kernel_params"):
                errors.append(f"{global_path}: boot.kernel_params must be a non-empty string")
        except ConfigLoaderError as exc:
            errors.append(str(exc))

        customizations_path = self.config_root / "base_customizations.json"
        try:
            customizations = self.load_json(customizations_path)
            if set(customizations) - {"live_user", "custom_files", "base_copy_files"}:
                errors.append(f"{customizations_path}: unknown top-level keys")
            live_user = customizations.get("live_user")
            if not isinstance(live_user, dict) or not isinstance(live_user.get("name"), str):
                errors.append(f"{customizations_path}: invalid live_user settings")
            elif not isinstance(live_user.get("groups"), list) or not all(
                isinstance(group, str) and group for group in live_user["groups"]
            ):
                errors.append(f"{customizations_path}: live_user.groups must be a list of non-empty strings")
        except ConfigLoaderError as exc:
            errors.append(str(exc))

        mapping_path = self.config_root / "assets" / "theme_mapping.json"
        try:
            mapping = self.load_json(mapping_path)
            all_destinations = []
            for source, destinations in mapping.items():
                if not (self.config_root / "assets" / source).is_file():
                    errors.append(f"{mapping_path}: missing source asset {source!r}")
                if not isinstance(destinations, list) or not destinations or not all(
                    isinstance(destination, str) and destination.startswith("/") for destination in destinations
                ):
                    errors.append(f"{mapping_path}: destinations for {source!r} must be absolute paths")
                else:
                    all_destinations.extend(destinations)
            if len(all_destinations) != len(set(all_destinations)):
                errors.append(f"{mapping_path}: duplicate destination paths")
        except ConfigLoaderError as exc:
            errors.append(str(exc))
        return errors
