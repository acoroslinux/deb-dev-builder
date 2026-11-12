"""Adapt shared installer policy to native Calamares module capabilities."""
import json
from pathlib import Path

import yaml


def module_properties(root, module_directories, module):
    """Read capabilities from the installed module's own schema."""
    schema_path = next((path for pattern in module_directories
                        for directory in sorted(Path(root).glob(pattern))
                        for path in [directory / module / (module + ".schema.yaml")]
                        if path.is_file()), None)
    return yaml.safe_load(schema_path.read_text()).get("properties", {}) if schema_path else {}


def configure_legacy_modules(root, module_directories):
    """Translate known policy differences without suppressing schema errors."""
    root = Path(root)
    configure_legacy_mount(root, module_directories)
    config_dir = root / "etc/calamares/modules"
    path = config_dir / "bootloader.conf"
    properties = module_properties(root, module_directories, "bootloader")
    if path.is_file() and "kernel" in properties and "kernelSearchPath" not in properties:
        bootloader = yaml.safe_load(path.read_text())
        if bootloader.get("efiBootLoader") != "grub":
            raise ValueError("Legacy bootloader compatibility requires GRUB")
        # GRUB discovers kernels through grub-mkconfig. The old schema also
        # requires Debian's kernel/initrd paths used by its systemd-boot branch.
        for key in ("kernelSearchPath", "kernelPattern", "kernelParams"):
            bootloader.pop(key, None)
        bootloader.setdefault("kernel", "/vmlinuz")
        bootloader.setdefault("img", "/initrd.img")
        path.write_text(json.dumps(bootloader, indent=2) + "\n")
    path = config_dir / "grubcfg.conf"
    properties = module_properties(root, module_directories, "grubcfg")
    if path.is_file() and "keepDistributor" in properties and "keep_distributor" not in properties:
        grub = yaml.safe_load(path.read_text())
        if "keep_distributor" in grub:
            grub["keepDistributor"] = grub.pop("keep_distributor")
        if grub.pop("always_use_defaults", False) and not grub.get("overwrite", False):
            raise ValueError("Legacy Calamares requires overwrite to apply all GRUB defaults")
        path.write_text(json.dumps(grub, indent=2) + "\n")
    path = config_dir / "services-systemd.conf"
    properties = module_properties(root, module_directories, "services-systemd")
    if path.is_file() and "timers" in properties and "units" not in properties:
        services = yaml.safe_load(path.read_text())
        for unit in services.pop("units", []):
            unit = dict(unit)
            action = unit.pop("action")
            suffix = unit["name"].rsplit(".", 1)[-1]
            category = {"timer": "timers", "target": "targets", "service": "services"}.get(suffix)
            if action != "enable" or category is None:
                raise ValueError("Unsupported legacy Calamares unit action: " + action)
            services.setdefault(category, []).append(unit)
        path.write_text(json.dumps(services, indent=2) + "\n")


def configure_legacy_mount(root, module_directories):
    """Use the 3.2 mount/fstab contract when its native schema requires it."""
    root = Path(root)
    properties = module_properties(root, module_directories, "mount")
    if "extraMountsEfi" not in properties or "mountOptions" in properties:
        return
    config_dir = root / "etc/calamares/modules"
    mount_path = config_dir / "mount.conf"
    if not mount_path.is_file():
        return
    mount = yaml.safe_load(mount_path.read_text())
    if "mountOptions" not in mount:
        return
    options = mount.pop("mountOptions")
    common, efi = [], list(mount.get("extraMountsEfi", []))
    for entry in mount.get("extraMounts", []):
        destination = efi if entry.pop("efi", False) else common
        destination.append(entry)
    # Older libcalamares expects the mount command's option string.
    for entry in common + efi:
        if isinstance(entry.get("options"), list):
            entry["options"] = ",".join(entry["options"])
    mount["extraMounts"] = common
    mount["extraMountsEfi"] = efi
    fstab_path = config_dir / "fstab.conf"
    fstab = yaml.safe_load(fstab_path.read_text())
    fstab["mountOptions"] = {
        entry["filesystem"]: ",".join(entry["options"])
        for entry in options if entry["filesystem"] != "efi"
    }
    for entry in options:
        if entry["filesystem"] == "efi":
            fstab["efiMountOptions"] = ",".join(entry["options"])
    mount_path.write_text(json.dumps(mount, indent=2) + "\n")
    fstab_path.write_text(json.dumps(fstab, indent=2) + "\n")
