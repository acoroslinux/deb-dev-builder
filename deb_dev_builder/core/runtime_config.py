"""Operational defaults loaded from JSON, with per-build overrides."""
import copy
import json
import re
import math
from pathlib import Path

from deb_dev_builder.core.path_utils import resolve_from_project


class RuntimeConfigError(RuntimeError):
    pass


def _validate_override_types(defaults: dict, overrides: dict, prefix="runtime"):
    for key, value in overrides.items():
        if key not in defaults:
            continue  # Maps can introduce new architectures, services and packages.
        expected = defaults[key]
        location = f"{prefix}.{key}"
        numeric = isinstance(expected, (int, float)) and not isinstance(expected, bool)
        if expected is None:
            valid = True  # Nullable policies validate their concrete type below.
        elif numeric:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            valid = isinstance(value, type(expected))
        if not valid:
            raise RuntimeConfigError(f"{location} must be {type(expected).__name__}")
        if isinstance(expected, dict):
            _validate_override_types(expected, value, location)
        elif isinstance(expected, list) and expected and all(isinstance(item, str) for item in expected):
            if not all(isinstance(item, str) for item in value):
                raise RuntimeConfigError(f"{location} must contain only strings")


def merge_settings(base: dict, override: dict) -> dict:
    """Merge objects; explicit lists replace defaults, including empty lists."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_settings(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_object(source: Path) -> dict:
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeConfigError(f"Cannot load runtime configuration {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeConfigError(f"Configuration must be an object: {source}")
    return data


def _catalog_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RuntimeConfigError("Catalog paths must be non-empty strings")
    source = (root / relative).resolve()
    if not source.is_relative_to(root.resolve()):
        raise RuntimeConfigError(f"Catalog path escapes configuration directory: {relative}")
    return source


def catalog_document_paths(source: Path) -> set[Path]:
    """Module documents are configuration, not selectable build profiles."""
    catalog = _read_object(source)
    try:
        return {_catalog_path(source.parent, entry["source"]) for entry in catalog["documents"]}
    except (KeyError, TypeError) as exc:
        raise RuntimeConfigError(f"Invalid documents index in {source}") from exc


def _load_catalog(source: Path) -> dict:
    catalog = _read_object(source)
    root = source.parent
    settings = {}
    try:
        for entry in catalog["documents"]:
            value = _read_object(_catalog_path(root, entry["source"]))
            if "key" in entry:
                value = value[entry["key"]]
            if not isinstance(value, dict):
                raise RuntimeConfigError(f"Catalog document must select an object: {entry['source']}")
            target = entry["target"]
            if not isinstance(target, str) or not target or any(not key for key in target.split(".")):
                raise RuntimeConfigError(f"Invalid catalog target: {target!r}")
            fragment = value
            for key in reversed(target.split(".")):
                fragment = {key: fragment}
            settings = merge_settings(settings, fragment)

        # Architecture and kernel metadata belongs to the selectable profile.
        # Derive the internal lookup maps instead of maintaining a second copy.
        architectures = {}
        aliases = {}
        for path in sorted(_catalog_path(root, catalog["architecture_profiles"]).glob("*.json")):
            profile = _read_object(path)
            name = profile["arch"]
            if name != path.stem or name in architectures:
                raise RuntimeConfigError(f"Architecture identity must match its unique filename: {path}")
            if not isinstance(profile["aliases"], list) or not all(isinstance(alias, str) and alias for alias in profile["aliases"]):
                raise RuntimeConfigError(f"Architecture aliases must be a list of names: {path}")
            if not isinstance(profile["image"], dict) or not isinstance(profile["kernel_suffix"], str):
                raise RuntimeConfigError(f"Invalid architecture image/kernel metadata: {path}")
            architectures[name] = {**profile["image"], "kernel_suffix": profile["kernel_suffix"]}
            for alias in profile["aliases"]:
                if alias in architectures or alias in aliases:
                    raise RuntimeConfigError(f"Duplicate architecture alias {alias!r}: {path}")
                aliases[alias] = name
        if not architectures or set(architectures).intersection(aliases):
            raise RuntimeConfigError("Architecture profiles must have unique names and aliases")
        settings["architectures"] = architectures
        settings["architecture_aliases"] = aliases
        settings["cli"]["architectures"] = sorted([*architectures, *aliases])

        kernel_packages = {}
        init_packages = {}
        zram_packages = {}
        module_sources = {_catalog_path(root, entry["source"]) for entry in catalog["documents"]}
        for path in sorted(_catalog_path(root, catalog["system_profiles"]).glob("*.json")):
            if path.resolve() in module_sources:
                continue
            profile = _read_object(path)
            if "kernel" in profile:
                kernel_packages[profile["kernel"]] = profile["package_templates"]
            if "init_system" in profile:
                init_packages[profile["init_system"]] = profile["live_config_package"]
                zram_packages[profile["init_system"]] = profile["zram_package"]
        settings["build"]["kernel_packages"] = kernel_packages
        settings["build"]["live_config_packages"] = init_packages
        settings["build"]["zram_packages"] = zram_packages
        repository = _read_object(_catalog_path(root, catalog["repository_profile"].format(distro=settings["cli"]["distro"])))
        settings["apt"] = {key: repository[key] for key in ("mirror", "suite", "components", "distro")}
    except (KeyError, TypeError) as exc:
        raise RuntimeConfigError(f"Invalid configuration catalog {source}: {exc}") from exc
    return settings


def _validate_pipeline(settings):
    """Validate new operational policies before any build side effects."""
    import re
    from .initrd_editor import archive_path
    epoch = settings["build"]["reproducibility"]["source_date_epoch"]
    if epoch is not None and (type(epoch) is not int or not 0 <= epoch <= 0xffffffff):
        raise RuntimeConfigError("source_date_epoch must be null or an unsigned 32-bit integer")
    customization = settings["installer"]["customization"]
    installer = settings["installer"]
    for architecture in settings["architectures"].values():
        flavors = architecture.get("kernel_flavors")
        if flavors is not None and (not isinstance(flavors, list) or not flavors
                or not all(isinstance(value, str) and value in settings["build"]["kernel_packages"] for value in flavors)):
            raise RuntimeConfigError("Architecture kernel_flavors must list configured kernel flavors")
        suites = architecture.get("bootable_suites", {})
        if not isinstance(suites, dict) or not all(
            family in installer["distributions"] and isinstance(values, list) and values
            and all(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) for value in values)
            for family, values in suites.items()
        ):
            raise RuntimeConfigError("Architecture bootable_suites must map distributions to nonempty suite lists")
    bundle = installer["bundle"]
    if type(bundle["enabled"]) is not bool:
        raise RuntimeConfigError("Installer bundle enabled must be boolean")
    for key in ("download_attempts", "generation_attempts"):
        if type(bundle[key]) is not int or not 1 <= bundle[key] <= 10:
            raise RuntimeConfigError("Installer bundle attempts must be between 1 and 10")
    try:
        archive_path(bundle["directory"].format(family="debian", suite="sid", architecture="amd64", source="cdrom"))
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeConfigError("Invalid installer bundle cache directory") from exc
    if not isinstance(bundle["snapshot_offsets_hours"], list) or not all(type(value) is int for value in bundle["snapshot_offsets_hours"]):
        raise RuntimeConfigError("Installer snapshot offsets must be integer hours")
    for family, mirrors in bundle["snapshot_mirrors"].items():
        if family not in installer["distributions"] or not isinstance(mirrors, list):
            raise RuntimeConfigError("Installer snapshot mirrors require a known distribution")
        for mirror in mirrors:
            if not isinstance(mirror, str) or not mirror.startswith("https://") or mirror.count("{timestamp}") != 1:
                raise RuntimeConfigError("Installer snapshot mirrors require HTTPS and a timestamp placeholder")
            try:
                mirror.format(timestamp="20260101T000000Z")
            except (KeyError, ValueError) as exc:
                raise RuntimeConfigError("Invalid installer snapshot mirror template") from exc
    for family, daily in installer["daily_images"].items():
        if family not in installer["distributions"] or not isinstance(daily["suites"], list) or not daily["suites"]:
            raise RuntimeConfigError("Daily installer sources require a known distribution and suite list")
        for suite in [*daily["suites"], daily["udeb_suite"]]:
            if not isinstance(suite, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite):
                raise RuntimeConfigError("Invalid daily installer suite")
        if not isinstance(daily["url"], str) or not daily["url"].startswith("https://") or daily["url"].count("{architecture}") != 1:
            raise RuntimeConfigError("Daily installer URLs must use HTTPS and an architecture placeholder")
    for key in ("image_mirror", "archive_mirror"):
        value = installer[key]
        if not isinstance(value, str) or (value and (not re.match(r"^https?://[^/\s]+", value) or any(char.isspace() for char in value))):
            raise RuntimeConfigError("Installer mirrors must be HTTP(S) URLs or empty")
    if not isinstance(installer["default_preseed"], list) or not all(
        isinstance(line, str) and re.fullmatch(r"d-i [A-Za-z0-9_./-]+ (?:string|select|boolean) [^\r\n]+", line)
        for line in installer["default_preseed"]
    ):
        raise RuntimeConfigError("Installer default preseed answers must be individual d-i lines")
    if not isinstance(installer["init_question"], str) or (installer["init_question"] and not re.fullmatch(r"[a-z0-9_./-]+", installer["init_question"])):
        raise RuntimeConfigError("Installer init question must be a debconf question name")
    if type(installer["init_ask"]) is not bool:
        raise RuntimeConfigError("Installer init_ask must be boolean")
    if not isinstance(installer["init_packages"], dict) or not all(
        isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]*", name)
        and isinstance(packages, list) and packages
        and all(isinstance(package, str) and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package) for package in packages)
        for name, packages in installer["init_packages"].items()
    ):
        raise RuntimeConfigError("Installer init packages must map init names to package lists")
    seen_boot_ids = set()
    for entry in settings["installer"]["extra_boot_entries"]:
        if not isinstance(entry, dict):
            raise RuntimeConfigError("Installer boot entries must be objects")
        identifier = entry.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", identifier) or identifier in seen_boot_ids or identifier in ("gtk", "auto"):
            raise RuntimeConfigError("Installer boot entries require unique lowercase identifiers")
        seen_boot_ids.add(identifier)
        label = entry.get("label", "")
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 (),./+-]*", label):
            raise RuntimeConfigError("Installer boot entry labels must be safe, nonempty English text")
        if entry.get("frontend") not in ("text", "gtk"):
            raise RuntimeConfigError("Installer boot entries must select text or gtk")
        parameters = entry.get("parameters")
        if not isinstance(parameters, list) or not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:=,+-]+", value) for value in parameters):
            raise RuntimeConfigError("Installer boot parameters must be individual safe command-line tokens")
    if type(customization["compression_level"]) is not int or not 0 <= customization["compression_level"] <= 9:
        raise RuntimeConfigError("Installer compression_level must be between 0 and 9")
    try:
        from .installer_conffiles import ucf_preservation_hooks
        ucf_preservation_hooks(installer["target_root"], installer["ucf_preservation"])
        from .installer_cdrom import target_cdrom_hooks
        target_cdrom_hooks(installer["target_root"], installer["target_cdrom"])
        layout_hook = Path(archive_path(installer['layout_hook']))
        if layout_hook.parent.as_posix() != 'usr/lib/live-installer.d' or layout_hook.name >= 'openssh-server':
            raise ValueError('Live target layout must be prepared before native configuration hooks')
        if archive_path(installer['layout_marker']) == '.':
            raise ValueError('Live target layout marker must name a file')
        for alias in installer['merged_usr_aliases']:
            if '/' in alias or archive_path(alias) == '.':
                raise ValueError('Merged usr aliases must be root-level names')
        if installer["target_cdrom"]["enabled"]:
            release = Path(installer["target_cdrom"]["release_hook"])
            validation = Path(installer["target_boot_hook"])
            if release.parent != validation.parent or not validation.name < release.name < "15cdrom-detect":
                raise ValueError("Target CD-ROM must be released after boot validation and before native ejection")
        for desktop, candidates in settings["desktop"]["session_candidates"].items():
            if not isinstance(desktop, str) or not desktop or not isinstance(candidates, list) or not candidates:
                raise ValueError("Desktop session candidates require named, nonempty lists")
            if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) and not name.endswith(".desktop") for name in candidates):
                raise ValueError("Desktop session candidates must be session names without paths or extensions")
        archive_path(customization["early_script_path"])
        archive_path(customization["late_script_path"])
        for overlay in customization["overlays"]:
            if not isinstance(overlay["source"], str) or not overlay["source"]:
                raise ValueError("Overlay source is required")
            if not overlay["frontends"] or set(overlay["frontends"]) - {"text", "gtk"}:
                raise ValueError("Overlay frontends must select text/gtk")
            for field in ("uid", "gid"):
                if type(overlay[field]) is not int or not 0 <= overlay[field] <= 0xffffffff:
                    raise ValueError("Overlay uid/gid must be unsigned integers")
        for pattern in customization["remove"]:
            archive_path(pattern)
        for patch in customization["patches"]:
            archive_path(patch["path"])
            if not patch["frontends"] or set(patch["frontends"]) - {"text", "gtk"}:
                raise ValueError("Patch frontends must select text/gtk")
            if not isinstance(patch["search"], str) or not patch["search"] or not isinstance(patch["replace"], str) or type(patch["count"]) is not int or patch["count"] < 1:
                raise ValueError("Patch requires search, replace and a positive match count")
        packages = settings["build"]["packages"]
        for field in ("local_repository", "local_source", "build_sources_path"):
            if archive_path(packages[field]) == ".":
                raise ValueError("Local APT paths must not select the root")
        for arch in packages["foreign_architectures"]:
            if not isinstance(arch, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", arch):
                raise ValueError("Invalid foreign architecture")
        for field in ("local_debs", "live_only", "keep_installed", "build_only_sources"):
            if not isinstance(packages[field], list) or not all(isinstance(value, str) and value for value in packages[field]):
                raise ValueError(f"packages.{field} must contain non-empty strings")
        iso = settings["artifacts"]["iso"]
        if iso["secure_boot"] not in {"auto", "require", "disable"}:
            raise ValueError("secure_boot must be auto, require or disable")
        for overlay in iso["binary_overlays"]:
            archive_path(overlay["destination"])
            if not isinstance(overlay["source"], str) or not overlay["source"]:
                raise ValueError("Binary overlay source is required")
        if not set(iso["checksum_algorithms"]) <= {"md5", "sha256", "sha512"}:
            raise ValueError("Unsupported ISO checksum algorithm")
        for spec in iso["signed_loaders"].values():
            for value in spec.values():
                archive_path(value)
        squashfs = settings["artifacts"]["squashfs"]
        if type(squashfs["processors"]) is not int or squashfs["processors"] < 0:
            raise ValueError("SquashFS processors must be a non-negative integer")
        if squashfs["compression_level"] is not None and type(squashfs["compression_level"]) is not int:
            raise ValueError("SquashFS compression_level must be null or an integer")
        if squashfs["sort_file"] is not None and not isinstance(squashfs["sort_file"], str):
            raise ValueError("SquashFS sort_file must be null or a path")
        report = settings["build"]["report"]
        if not isinstance(report["suffix"], str) or not report["suffix"].startswith(".") or "/" in report["suffix"] or "\\" in report["suffix"]:
            raise ValueError("Report suffix must be a file suffix")
        for command in report["tool_commands"]:
            if not isinstance(command, list) or not command or not all(isinstance(argument, str) and argument for argument in command):
                raise ValueError("Report tool_commands must be non-empty argument lists")
        for rule in settings["build"]["capability_rules"]:
            if not isinstance(rule["when"], dict) or not isinstance(rule["require"], dict) or not isinstance(rule["message"], str):
                raise ValueError("Capability rules require when/require objects and a message")
            if not all(isinstance(allowed, list) and allowed for allowed in rule["require"].values()):
                raise ValueError("Capability requirements must be non-empty lists")
        storage = settings["build"]["installer_storage"]
        if not isinstance(storage["initramfs_excludes"], list) or not all(isinstance(value, str) and value and '/' not in value for value in storage["initramfs_excludes"]):
            raise ValueError("Initramfs exclusions must be filename patterns")
        unlock = storage["keyfile_unlock"]
        if not isinstance(unlock["key_file"], str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", unlock["key_file"]):
            raise ValueError("Unlock key must be one absolute file path")
        for path in (unlock["key_file"].lstrip('/'), unlock["embedded_directory"], unlock["initramfs_config"]):
            if archive_path(path) == '.':
                raise ValueError("Unlock key paths must not select the root")
        for mode in (unlock["mode"], unlock["umask"]):
            if not isinstance(mode, str) or not re.fullmatch(r"0?[0-7]{3}", mode):
                raise ValueError("Unlock key modes must be octal permissions")
        if int(unlock["mode"], 8) not in {0o400, 0o600} or int(unlock["umask"], 8) != 0o077:
            raise ValueError("Unlock keys and initramfs must not permit group or other access")
        for command in (storage["initramfs_update"], storage["initramfs_list"], storage["initramfs_extract"],
                        storage["early_unlock_ancestor_command"], storage["early_unlock_resolve_command"],
                        unlock["boot_source_command"], unlock["ancestor_types_command"],
                        unlock["resolve_source_command"], unlock["verify_key_command"]):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("Storage initramfs commands must be argument lists")
        options = [storage["early_unlock_option"], *storage["early_unlock_options"]]
        if not storage["early_unlock_options"] or not all(isinstance(option, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", option) for option in options):
            raise ValueError("Storage early unlock options must be crypttab option names")
        for paths in [*storage["required_initramfs_paths"].values(), *storage["initramfs_path_aliases"].values()]:
            if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path for path in paths):
                raise ValueError("Storage initramfs checks must be non-empty lists of paths")
            for path in paths:
                archive_path(path)
        for value in (storage["initramfs_glob"], storage["cryptsetup_config"]["path"],
                      storage["crypttab"], storage["crypttab_initramfs_path"], storage["grub_config"], storage["fstab"],
                      storage["validation_log"], *storage["required_boot_files"],
                      settings["installer"]["target_boot_hook"], settings["installer"]["cleanup_marker"]):
            if archive_path(value) == ".":
                raise ValueError("Storage paths must not select the root")
        identity = settings["customization"]["identity"]
        for key in ("machine_id", "dbus_id", "setup_binary", "ssh_server_binary", "commit_mask"):
            if archive_path(identity[key]) == ".":
                raise ValueError("Identity paths must not select the root")
        if identity["dbus_target"] != "/" + identity["machine_id"]:
            raise ValueError("D-Bus machine-id must reference the configured system machine-id")
        for command in (identity["setup_command"], identity["ssh_key_setup"]):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("Identity setup commands must be argument lists")
        audit = settings["build"]["installer_audit"]
        for key in ("report_path", "settings_path", "config_directory"):
            if archive_path(audit[key]) == ".":
                raise ValueError("Installer audit paths must not select the root")
        for directory in audit["module_directories"]:
            archive_path(directory)
        for pair in audit["before"]:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(job, str) and job for job in pair):
                raise ValueError("Installer audit ordering rules must be job pairs")
        locale = settings["installer"]["locale"]
        grub = settings["customization"]["grub"]
        plymouth = settings["customization"]["plymouth"]
        audio = settings['customization']['session_audio']
        for path in (audio['launcher'], audio['autostart_path'], *audio['required_binaries']):
            if archive_path(path) == '.':
                raise ValueError('Session audio paths must name files')
        for command in (*audio['processes'], audio['lock_command'], audio['readiness_command']):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError('Session audio commands must be argument lists')
        if not audio['processes'] or type(audio['readiness_attempts']) is not int or audio['readiness_attempts'] < 1:
            raise ValueError('Session audio requires processes and a positive startup retry limit')
        if not isinstance(audio['readiness_interval'], str) or not re.fullmatch(r'[0-9]+(?:[.][0-9]+)?', audio['readiness_interval']):
            raise ValueError('Session audio startup interval must be numeric')
        if not isinstance(audio['init_systems'], list) or not all(isinstance(name, str) and name for name in audio['init_systems']):
            raise ValueError('Session audio init systems must be a string list')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', audio['lock_file']):
            raise ValueError('Session audio lock must be a filename')
        for key, value in audio['environment'].items():
            if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key) or not isinstance(value, str) or '\0' in value:
                raise ValueError('Invalid session audio environment entry')
        for key, value in audio['autostart'].items():
            if not isinstance(value, str) or any(char in key + value for char in '\r\n\0'):
                raise ValueError('Audio autostart entries must be single-line strings')
        media = settings['customization']['live_media']
        for key in ('native_script', 'helper', 'service_path'):
            if archive_path(media[key]) == '.':
                raise ValueError('Live media paths must name files')
        for command in (media['client'], media['start_command'], media['reactivate_command'], media['active_vt_command'], media['switch_vt_command'], media['timeout_command'], media['release_renderer_command'], media['retained_switch_command'], media['keyboard_command'], media['active_console_command'], media['terminal_settings_command']):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError('Live media commands must be argument lists')
        if media['presentation'] not in ('plymouth', 'retained'):
            raise ValueError('Live media presentation must be plymouth or retained')
        if not isinstance(media['state_file'], str) or not media['state_file'].startswith('/') or archive_path(media['state_file'].lstrip('/')) == '.':
            raise ValueError('Live media state must name an absolute file path')
        if not isinstance(media['terminal_path'], str) or media['terminal_path'].count('{vt}') != 1 or not media['terminal_path'].startswith('/'):
            raise ValueError('Live media terminal path must be absolute and contain {vt}')
        if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', media['stage_environment']):
            raise ValueError('Invalid live media stage environment name')
        if not isinstance(media['service_content'], str) or not media['service_content']:
            raise ValueError('Live media service content is required')
        for target in media['service_targets']:
            if not re.fullmatch(r'[A-Za-z0-9-]+[.]target', target):
                raise ValueError('Invalid live media shutdown target')
        if not isinstance(media['shutdown_kernel_options'], list) or not all(isinstance(arg, str) and re.fullmatch(r'[A-Za-z0-9_.=-]+', arg) for arg in media['shutdown_kernel_options']):
            raise ValueError('Invalid shutdown-only Plymouth kernel options')
        if not media['confirm_keys'] or not media['disabled_options']:
            raise ValueError('Live media confirmation keys and disabled options are required')
        for translations in plymouth['media_translations'].values():
            if not set(translations) <= {'media_title', 'media_hint'} or not all(isinstance(value, str) and value and not any(c in value for c in '\r\n\0') for value in translations.values()):
                raise ValueError('Live media translations must be single-line title/hint strings')
        archive_path(plymouth["script_file"])
        for key, value in plymouth["visual"].items():
            if not re.fullmatch(r"[a-z_]+", key) or type(value) not in (str, int, float):
                raise ValueError("Invalid Plymouth visual setting")
            if isinstance(value, str) and (not value or any(c in value for c in "\r\n\0")):
                raise ValueError("Plymouth visual text must be a nonempty line")
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
                raise ValueError("Plymouth visual numbers must be finite and nonnegative")
            if (key.endswith(("_red", "_green", "_blue", "_y")) or key in {"background_opacity", "bar_fraction", "entry_fraction"}) and not (isinstance(value, (int, float)) and 0 <= value <= 1):
                raise ValueError("Plymouth colors and relative positions must be within 0..1")
        for key in ("dot_count", "dot_frames", "dot_spacing", "bar_height", "entry_height", "entry_border"):
            if type(plymouth["visual"][key]) is not int or plymouth["visual"][key] < 1:
                raise ValueError("Plymouth animation dimensions must be positive integers")
        if not plymouth["visual"]["entry_fraction"] or 2 * plymouth["visual"]["entry_border"] >= plymouth["visual"]["entry_height"]:
            raise ValueError("Plymouth password field must have a positive inner area")
        for value in (grub["defaults_file"], grub["theme_file"], grub["installer_hook"], grub["calamares_config"], plymouth["binary"], plymouth["selector"], plymouth["directory"], *plymouth["required_initramfs_paths"], *(p for paths in plymouth["initramfs_path_aliases"].values() for p in paths)):
            if archive_path(value) == ".":
                raise ValueError("GRUB/Plymouth paths must not select the root")
        for key in [*grub["settings"], grub["kernel_parameters_key"], grub["theme_key"]]:
            if not re.fullmatch(r"GRUB_[A-Z0-9_]+", key):
                raise ValueError("Invalid GRUB setting name")
        for value in grub["settings"].values():
            if type(value) not in (str, int, bool) or (isinstance(value, str) and any(c in value for c in "\r\n\0")):
                raise ValueError("GRUB setting values must be single-line strings, integers or booleans")
        for values in (grub["kernel_parameters"], grub["plymouth_parameters"], settings["artifacts"]["iso"]["plymouth_kernel_options"]):
            if not isinstance(values, list) or not all(isinstance(value, str) and value and not any(c.isspace() for c in value) for value in values):
                raise ValueError("Boot parameters must be lists of individual arguments")
        if not grub["header"] or not all(isinstance(value, str) and not any(c in value for c in "\r\n\0") for value in grub["header"]):
            raise ValueError("GRUB header must contain comment lines")
        for pattern in plymouth["required_initramfs_patterns"]:
            re.compile(pattern)
        for command in (grub["update_command"], plymouth["select_command"]):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("GRUB/Plymouth commands must be argument lists")
        for value in (locale["finish_hook"], locale["generation_file"], locale["supported_file"], locale["log_path"], *locale["files"]):
            if archive_path(value) == ".":
                raise ValueError("Installer locale paths must name files")
        if not locale["files"] or not locale["builtin_locales"]:
            raise ValueError("Installer locale files and builtin locales must not be empty")
        for value in locale["builtin_locales"]:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_@.+-]+", value):
                raise ValueError("Invalid builtin locale")
        for command in (locale["generate_command"], locale["update_command"]):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("Installer locale commands must be argument lists")
        for key in ("locale_question", "language_question"):
            if not isinstance(locale[key], str) or not re.fullmatch(r"[a-zA-Z0-9+_.:/-]+", locale[key]):
                raise ValueError("Invalid installer locale debconf identifier")
        autologin = settings["installer"]["autologin"]
        if autologin["priority"] not in {"low", "medium", "high", "critical"}:
            raise ValueError("Invalid d-i autologin question priority")
        if type(autologin["minimum_uid"]) is not int or autologin["minimum_uid"] < 1:
            raise ValueError("Autologin minimum_uid must be a positive integer")
        for key in ("template_path", "startup_hook", "finish_hook"):
            if archive_path(autologin[key]) == ".":
                raise ValueError("Autologin paths must name files")
        for key in ("owner", "question", "user_question", "make_user_question"):
            if not isinstance(autologin[key], str) or not re.fullmatch(r"[a-zA-Z0-9+_.:/-]+", autologin[key]):
                raise ValueError("Invalid autologin debconf identifier")
        for key in ("description", "details"):
            if not isinstance(autologin[key], str) or any(char in autologin[key] for char in "\r\n\0"):
                raise ValueError("Autologin template text must be a single line")
        for manager, spec in autologin["managers"].items():
            if manager not in settings["desktop"]["display_managers"]:
                raise ValueError("Autologin manager is missing desktop runtime settings")
            archive_path(spec["path"])
            for section, values in [*spec["sections"].items(), *spec["disable_sections"].items()]:
                if not section or any(char in section for char in "[]\r\n\0"):
                    raise ValueError("Invalid autologin INI section")
                for key, value in values.items():
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or not isinstance(value, str) or any(char in value for char in "\r\n\0"):
                        raise ValueError("Invalid autologin INI setting")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeConfigError(f"Invalid pipeline policy: {exc}") from exc


def load_runtime(config=None, *, path: Path | None = None) -> dict:
    """Compose module documents and profile metadata; apply build overrides last."""
    source = path or resolve_from_project("configs/catalog.json")
    defaults = _load_catalog(source)
    overrides = (config or {}).get("runtime", {})
    if not isinstance(overrides, dict):
        raise RuntimeConfigError("The runtime override must be an object")
    family = (config or {}).get("base_distro", defaults["system"]["base_distro"])
    distributions = defaults["installer"]["distributions"]
    # Family defaults precede explicit build overrides. Never infer Devuan
    # installer images from the merged APT mirror or from a Debian codename.
    if family in distributions:
        defaults["installer"] = merge_settings(defaults["installer"], distributions[family])
    _validate_override_types(defaults, overrides)
    settings = merge_settings(defaults, overrides)
    if settings["cli"]["distro"] != defaults["cli"]["distro"]:
        catalog = _read_object(source)
        repository = _read_object(_catalog_path(source.parent, catalog["repository_profile"].format(distro=settings["cli"]["distro"])))
        settings["apt"] = merge_settings(
            {key: repository[key] for key in ("mirror", "suite", "components", "distro")},
            overrides.get("apt", {}),
        )
    _validate_pipeline(settings)
    return settings


def architecture_settings(config: dict, runtime: dict | None = None) -> dict:
    settings = runtime or load_runtime(config)
    arch = config.get("architecture") or config.get("arch") or config.get("dpkg_arch") or settings["cli"]["architecture"]
    arch = settings["architecture_aliases"].get(arch, arch)
    try:
        return settings["architectures"][arch]
    except KeyError as exc:
        raise RuntimeConfigError(f"Architecture is missing runtime settings: {arch}") from exc


def installer_storage_packages(config: dict, runtime: dict | None = None) -> list[str]:
    settings = runtime or load_runtime(config)
    policy = settings["build"]["installer_storage"]
    init_system = config.get("init_system", settings["build"]["default_init_system"])
    suite = config.get("suite", settings["apt"]["suite"])
    packages = [*policy["packages"], *policy["init_packages"].get(init_system, [])]
    return list(dict.fromkeys(package for package in packages
                              if package not in policy["package_suites"] or suite in policy["package_suites"][package]))


def live_user_settings(config: dict, runtime: dict | None = None) -> dict:
    settings = runtime or load_runtime(config)
    profile_name = settings["build"]["default_live_profile"]
    source = resolve_from_project(f"configs/live-users/{profile_name}.json")
    try:
        defaults = json.loads(source.read_text(encoding="utf-8"))["live_user"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeConfigError(f"Cannot load default live-user profile {source}: {exc}") from exc
    user = config.get("live_user")
    if user is None:
        return copy.deepcopy(defaults)
    if isinstance(user, str):
        return {**defaults, "name": user, "password": None}
    if not isinstance(user, dict):
        raise RuntimeConfigError("live_user must be an object or a login name")
    # An explicitly selected account must never inherit another user's secret.
    return {**defaults, "password": None, **user}
