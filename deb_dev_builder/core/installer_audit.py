"""Validate the deployed installer against the modules actually in its rootfs."""
import json
import re
from pathlib import Path

from .initrd_editor import archive_path
from .package_policy import installed_packages
from .runtime_config import installer_storage_packages
from .path_utils import resolve_from_project


class InstallerAuditError(RuntimeError):
    pass


def _path(root, relative):
    archive_path(relative)
    return Path(root) / relative


def audit_calamares(root, policy):
    try:
        import yaml
        from jsonschema import Draft7Validator
    except ImportError as exc:
        raise InstallerAuditError("Installer audit requires PyYAML and jsonschema; install requirements.txt") from exc
    root = Path(root)
    settings = yaml.safe_load(_path(root, policy["settings_path"]).read_text())
    jobs = [job for stage in settings["sequence"] for job in stage.get("exec", [])]
    for job in policy["required_jobs"]:
        if jobs.count(job) != 1:
            raise InstallerAuditError(f"Calamares requires exactly one execution of {job}")
    for first, second in policy["before"]:
        if first not in jobs or second not in jobs or jobs.index(first) >= jobs.index(second):
            raise InstallerAuditError(f"Calamares must run {first} before {second}")
    instances = {}
    for entry in settings.get("instances", []):
        if entry["id"] in instances:
            raise InstallerAuditError(f"Duplicate Calamares instance: {entry['id']}")
        instances[entry["id"]] = entry
    directories = [path for pattern in policy["module_directories"] for path in sorted(root.glob(pattern)) if path.is_dir()]
    references = jobs + [job for stage in settings["sequence"] for job in stage.get("show", [])]
    modules, validated = set(), []
    partition = yaml.safe_load(_path(root / policy["config_directory"], "partition.conf").read_text())
    for filesystem in partition.get("availableFileSystemTypes", []):
        tool = policy["filesystem_tools"].get(filesystem)
        if not tool or not _path(root, tool).is_file():
            raise InstallerAuditError(f"Calamares offers {filesystem} without its configured formatting tool")
    for reference in dict.fromkeys(references):
        module, _, instance_id = reference.partition("@")
        entry = instances.get(instance_id) if instance_id else None
        if instance_id and (entry is None or entry["module"] != module):
            raise InstallerAuditError(f"Undefined Calamares instance: {reference}")
        descriptor = next((directory / module / "module.desc" for directory in directories
                           if (directory / module / "module.desc").is_file()), None)
        if descriptor is None:
            raise InstallerAuditError(f"Calamares module is missing: {module}")
        metadata = yaml.safe_load(descriptor.read_text())
        for key in ("script", "load"):
            if key in metadata and not _path(descriptor.parent, metadata[key]).is_file():
                raise InstallerAuditError(f"Calamares module payload is missing: {module}/{metadata[key]}")
        modules.add(module)
        filename = entry.get("config", module + ".conf") if entry else module + ".conf"
        configuration = _path(root / policy["config_directory"], filename)
        if configuration.exists():
            data = yaml.safe_load(configuration.read_text())
            for schema_path in descriptor.parent.glob("*.schema.yaml"):
                schema = yaml.safe_load(schema_path.read_text())
                # Some shipped schemas use relative IDs for definitions. Make
                # those references local, supporting older jsonschema packages
                # in build hosts and preventing network lookups during a build.
                def local_refs(node):
                    if isinstance(node, dict):
                        reference = node.get("$ref")
                        if reference and not reference.startswith("#"):
                            if reference.startswith("definitions/") and reference.split("/", 1)[1] in schema.get("definitions", {}):
                                node["$ref"] = "#/" + reference
                            else:
                                raise InstallerAuditError(f"Nonlocal schema reference in {schema_path.name}: {reference}")
                        for value in node.values():
                            local_refs(value)
                    elif isinstance(node, list):
                        for value in node:
                            local_refs(value)
                local_refs(schema)
                errors = sorted(Draft7Validator(schema).iter_errors(data), key=lambda error: str(error.path))
                if errors:
                    raise InstallerAuditError(f"Calamares configuration {filename}: {errors[0].message}")
                validated.append(reference)
        elif not metadata.get("noconfig", False):
            # Some native modules have no distribution-specific config at all.
            # Referenced instances, however, must always supply their file.
            if entry:
                raise InstallerAuditError(f"Calamares instance config is missing: {filename}")
    component = settings.get("branding", "")
    if not component or Path(component).name != component:
        raise InstallerAuditError("Calamares branding must name a local component")
    theme = _path(root, policy["branding_directory"]) / component
    descriptor = theme / "branding.desc"
    if not descriptor.is_file():
        raise InstallerAuditError("Calamares branding descriptor is missing")
    branding = yaml.safe_load(descriptor.read_text())
    if branding.get("componentName") != component:
        raise InstallerAuditError("Calamares branding component name does not match settings")
    images = branding.get("images", {})
    if "welcomeImage" in images:
        raise InstallerAuditError("Calamares uses productWelcome, not welcomeImage")
    resources = [*policy["branding_required_files"]]
    for key in policy["branding_required_images"]:
        if not images.get(key):
            raise InstallerAuditError(f"Calamares branding image is not configured: {key}")
        resources.append(images[key])
    slideshow = branding.get("slideshow")
    if not isinstance(slideshow, str) or not slideshow:
        raise InstallerAuditError("Calamares branding must configure a QML slideshow")
    resources.append(slideshow)
    checked = set()
    while resources:
        relative = resources.pop()
        path = _path(theme, relative)
        if not path.resolve().is_relative_to(theme.resolve()) or not path.is_file():
            raise InstallerAuditError(f"Calamares branding resource is missing or escapes its theme: {relative}")
        if relative in checked:
            continue
        checked.add(relative)
        signature = path.read_bytes()[:8]
        if path.suffix.lower() == ".png" and signature != b"\x89PNG\r\n\x1a\n":
            raise InstallerAuditError(f"Calamares branding image is not PNG: {relative}")
        if path.suffix.lower() in {".jpg", ".jpeg"} and not signature.startswith(b"\xff\xd8\xff"):
            raise InstallerAuditError(f"Calamares branding image is not JPEG: {relative}")
        if path.suffix == ".qml":
            source = path.read_text()
            resources.extend(re.findall(r'(?:source|artwork)\s*:\s*["\']([^"\']+)["\']', source))
            for name in re.findall(r'\b([A-Z][A-Za-z0-9_]*)\s*\{', source):
                if (theme / (name + ".qml")).is_file():
                    resources.append(name + ".qml")
    return {"jobs": jobs, "modules": sorted(modules), "schemas_validated": validated,
            "branding": {"component": component, "resources_checked": sorted(checked)}}


def audit_installer_root(root, config, runtime, toolchain=None):
    policy = runtime["build"]["installer_audit"]
    if not policy["enabled"]:
        return {}
    root = Path(root)
    identity = _path(root, runtime["customization"]["identity"]["machine_id"])
    if not identity.is_file() or identity.is_symlink():
        raise InstallerAuditError("Rootfs must contain a regular /etc/machine-id before image creation")
    if policy["require_empty_machine_id"] and identity.read_bytes():
        raise InstallerAuditError("Live rootfs machine-id must be empty")
    report = {"machine_id_placeholder": True}
    if config.get("with_calamares") or config.get("with_debian_installer"):
        installed = {entry["Package"] for entry in installed_packages(root).values()}
        missing = set(installer_storage_packages(config, runtime)) - installed
        if missing:
            raise InstallerAuditError("Installer target storage packages missing: " + ", ".join(sorted(missing)))
    if config.get("with_calamares"):
        if toolchain is None:
            report["calamares"] = audit_calamares(root, policy)
        else:
            result = toolchain.run_in_build_host([
                "python3", str(resolve_from_project("tools/audit-installers.py")), str(root),
                "--policy-json", json.dumps(policy),
            ], check=False, capture_output=True, text=True)
            if result.returncode != 0:
                raise InstallerAuditError("Calamares audit failed: " + (result.stderr or result.stdout or "no diagnostic"))
            report["calamares"] = json.loads(result.stdout)
    destination = _path(root, policy["report_path"])
    if not destination.resolve().is_relative_to(root.resolve()):
        raise InstallerAuditError("Installer audit report escapes rootfs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    return report
