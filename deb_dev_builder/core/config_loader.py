import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.runtime_config import load_runtime, merge_settings, architecture_settings, RuntimeConfigError, catalog_document_paths

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
        catalog = self.config_root / "catalog.json"
        if catalog.exists() and path.resolve() in catalog_document_paths(catalog):
            raise ConfigLoaderError(f"Configuration document is not a selectable {category} profile: {profile_name}")
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
            if key == "runtime" and isinstance(value, dict):
                result[key] = merge_settings(result.get(key, {}), value)
            elif key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_dicts(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                existing = list(result[key])
                extras = [item for item in value if item not in existing]
                result[key] = existing + extras
            else:
                result[key] = value
        return result

    def _desktop_distribution_profile(self, profile, family, distro):
        """Apply package differences declared by a desktop for a distribution."""
        profile = dict(profile)
        overrides = profile.pop('distribution_overrides', {})
        if not isinstance(overrides, dict):
            raise ConfigLoaderError('Desktop distribution_overrides must be an object')
        for selector, values in overrides.items():
            if not isinstance(selector, str) or not selector or not isinstance(values, dict):
                raise ConfigLoaderError('Desktop distribution overrides require named objects')
            if set(values) - {'packages', 'exclude_packages', 'backports_mirror', 'backports_suite', 'apt_target_release', 'display_manager', 'session_type'}:
                raise ConfigLoaderError('Desktop distribution overrides only accept package, session and backports settings')
            for key, packages in values.items():
                if key == 'session_type':
                    if not isinstance(packages, str) or packages not in {'x11', 'wayland', 'both'}:
                        raise ConfigLoaderError('Desktop session_type must be x11, wayland or both')
                elif key in {'backports_mirror', 'backports_suite', 'apt_target_release', 'display_manager'}:
                    if not isinstance(packages, str) or not packages or any(char.isspace() for char in packages):
                        raise ConfigLoaderError('Desktop repository and manager settings must be nonempty strings without whitespace')
                elif not isinstance(packages, list) or not all(isinstance(p, str) and p for p in packages):
                    raise ConfigLoaderError('Desktop distribution override packages must be string lists')
        for selector in dict.fromkeys((family, distro)):
            if selector in overrides:
                profile = self._merge_dicts(profile, overrides[selector])
        return profile

    @staticmethod
    def _architecture_kernel_packages(kernel: Optional[str], dpkg_arch: str, runtime=None) -> List[str]:
        runtime = runtime or load_runtime()
        suffix = architecture_settings({"dpkg_arch": dpkg_arch}, runtime)["kernel_suffix"]
        policy = runtime["build"]
        flavor = (kernel or runtime["cli"]["kernel"]).lower()
        flavor = policy["kernel_aliases"].get(flavor, flavor)
        templates = policy["kernel_packages"].get(flavor, policy["kernel_packages"][runtime["cli"]["kernel"]])
        return [template.format(suffix=suffix) for template in templates]

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

        runtime_path = self.config_root / "catalog.json"
        runtime_input = self.load_json(global_config_path)
        if distro:
            family = self.load_profile("system", distro).get("base_distro")
            if family:
                runtime_input = {**runtime_input, "base_distro": family}
        runtime = load_runtime(runtime_input, path=runtime_path if runtime_path.is_file() else None)
        architecture = runtime["architecture_aliases"].get(architecture, architecture)

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
        config["runtime"] = runtime

        # The default live account has its own profile, just like admin and
        # guest. Do not inherit its credentials into another named account.
        if not live_profile:
            config["live_user"] = self.load_profile("live-users", runtime["build"]["default_live_profile"]).get("live_user", {})

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
        config = self._merge_dicts(config, self._normalize_profile({key: value for key, value in arch_profile.items() if key not in {"aliases", "image", "kernel_suffix"}}))
        config["architecture"] = arch_profile["arch"]
        config["dpkg_arch"] = arch_profile.get("dpkg_arch", architecture)

        if hardware_profile:
            hardware = self.load_profile("hardware", hardware_profile)
            hardware_arch = hardware.get("architecture")
            aliases = runtime["architecture_aliases"]
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
            desktop_profile = self._desktop_distribution_profile(desktop_profile, config.get('base_distro'), distro)
            config = self._merge_dicts(config, self._normalize_profile(desktop_profile))
            session_type = desktop_profile.get("session_type", "x11")
            if session_type in {"x11", "both"}:
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("software", "xorg")))
            if session_type == "wayland":
                config = self._merge_dicts(config, self._normalize_profile(self.load_profile("software", "wayland")))

        # 7. Kernel
        if kernel:
            kernel = runtime["build"]["kernel_aliases"].get(kernel.lower(), kernel.lower())
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
            # from global customization settings.
            if "groups" in live_user_profile:
                config["live_user"]["groups"] = list(live_user_profile["groups"])

        # 14. Deduplicate lists
        # NetworkManager split its GTK tools in testing/unstable. Keep older
        # release profiles on their native package, and use the new names where
        # the transitional network-manager-gnome package has been removed.
        package_aliases = runtime['build']['package_aliases']
        config['software'] = [replacement for package in config['software']
                              for replacement in package_aliases.get(package, {}).get(config.get('suite'), [package])]
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
        for package in self._architecture_kernel_packages(kernel, config["dpkg_arch"], config["runtime"]):
            if package not in software:
                software.append(package)
        excluded = set(config.get("exclude_packages", []))
        config["software"] = [package for package in software if package not in excluded]

        return config

    def audit_catalog(self) -> List[str]:
        """Return semantic errors found across every JSON configuration file."""
        errors: List[str] = []
        catalog_path = self.config_root / "catalog.json"
        try:
            module_documents = catalog_document_paths(catalog_path) if catalog_path.exists() else set()
        except RuntimeConfigError as exc:
            return [str(exc)]
        allowed = {
            "architectures": {"arch", "dpkg_arch", "aliases", "image", "kernel_suffix"},
            "boot": {"packages", "bootloader"},
            "desktops": {"desktop", "display_manager", "session_type", "supported_suites", "packages", "exclude_packages", "distribution_overrides"},
            "hardware": {
                "name", "architecture", "bootloader", "output_format", "status", "notes",
                "packages", "partition_start_mib", "firmware_tree", "boot_artifacts",
                "required_files",
            },
            "live-users": {"live_user"},
            "services": {"services", "packages"},
            "repos": {"repos", "extra_repos", "packages"},
            "vm": {"name", "output_format", "firmware", "supported_architectures", "packages"},
            "software": {"packages", "exclude_packages", "offline_repo_packages", "supported_suites"},
            "system": {
                "distro", "distro_name", "base_distro", "system", "suite", "mirror",
                "security_mirror", "security_suite", "updates_mirror", "updates_suite",
                "backports_mirror", "backports_suite", "components", "init_system",
                "kernel", "variant", "packages", "exclude_packages", "supported_suites",
                "package_templates", "live_config_package", "zram_package",
            },
        }
        identity_keys = {
            "architectures": "arch", "desktops": "desktop", "system": None,
        }
        for category, keys in allowed.items():
            category_dir = self.config_root / category
            for path in sorted(category_dir.glob("*.json")):
                if path.resolve() in module_documents:
                    continue
                try:
                    data = self.load_json(path)
                except ConfigLoaderError as exc:
                    errors.append(str(exc))
                    continue
                unknown = sorted(set(data) - keys)
                if unknown:
                    errors.append(f"{path}: unknown keys: {', '.join(unknown)}")
                if category == 'desktops':
                    try:
                        self._desktop_distribution_profile(data, None, None)
                    except ConfigLoaderError as exc:
                        errors.append(f'{path}: {exc}')
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
            runtime_path = self.config_root / "catalog.json"
            load_runtime(global_data, path=runtime_path if runtime_path.is_file() else None)
            if set(global_data) - {"system", "boot", "hooks", "cleanup", "toolchain", "live_user", "runtime"}:
                errors.append(f"{global_path}: unknown top-level keys")
            if "live_user" in global_data and not isinstance(global_data["live_user"], dict):
                errors.append(f"{global_path}: live_user must be an object")
            system = global_data.get("system")
            if not isinstance(system, dict) or set(system) - {"hostname", "timezone", "locale", "iso_label", "apt_cache"}:
                errors.append(f"{global_path}: invalid system settings")
            boot = global_data.get("boot")
            if not isinstance(boot, dict) or set(boot) - {"kernel_params"}:
                errors.append(f"{global_path}: invalid boot settings")
            hooks = global_data.get("hooks", {})
            if not isinstance(hooks, dict) or set(hooks) - {"enabled", "directory"}:
                errors.append(f"{global_path}: invalid hooks settings")
            elif not isinstance(hooks.get("enabled", True), bool) or not isinstance(hooks.get("directory", "configs/hooks"), str) or not hooks.get("directory", "configs/hooks").strip():
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
        except (ConfigLoaderError, RuntimeConfigError) as exc:
            errors.append(str(exc))

        customizations_path = self.config_root / "base_customizations.json"
        try:
            customizations = self.load_json(customizations_path)
            if set(customizations) - {"live_user", "custom_files", "base_copy_files"}:
                errors.append(f"{customizations_path}: unknown top-level keys")
            if "live_user" in customizations:
                live_user = customizations["live_user"]
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
