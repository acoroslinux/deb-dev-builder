import struct

import pytest

from deb_dev_builder.core.initrd_editor import Entry, customize_initrd, pack_initrd, unpack_initrd
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.runtime_config import RuntimeConfigError, load_runtime
from deb_dev_builder.core.toolchain_manager import ToolchainManager


@pytest.mark.parametrize("frontend", ["text", "gtk"])
def test_theme_overlay_preserves_native_accessibility_and_generated_hooks(tmp_path, frontend):
    originals = {
        "etc/gtk-2.0/gtkrc": Entry("etc/gtk-2.0/gtkrc", b'gtk-theme-name = "Clearlooks"\n', 0o100644),
        "usr/share/themes/dark/gtk-2.0/gtkrc": Entry("usr/share/themes/dark/gtk-2.0/gtkrc", b"native high contrast", 0o100644),
        "usr/bin/gtk-set-font": Entry("usr/bin/gtk-set-font", b"native multilingual fonts and zoom", 0o100755),
        "usr/share/graphics/logo_installer.png": Entry("usr/share/graphics/logo_installer.png", b"logo_debian.png", 0o120777),
    }
    archive = tmp_path / "initrd.gz"
    archive.write_bytes(pack_initrd(originals))
    hook = Entry("usr/lib/finish-install.d/14-target-boot", b"mandatory boot checks", 0o100755)
    customize_initrd(archive, load_runtime()["installer"]["customization"], frontend, generated=[hook])
    result = unpack_initrd(archive.read_bytes())
    assert result[hook.name] == hook
    for name in ("usr/bin/gtk-set-font", "usr/share/themes/dark/gtk-2.0/gtkrc"):
        assert result[name] == originals[name]
    if frontend == "text":
        assert result["etc/gtk-2.0/gtkrc"] == originals["etc/gtk-2.0/gtkrc"]
        assert result["usr/share/graphics/logo_installer.png"] == originals["usr/share/graphics/logo_installer.png"]
    else:
        assert b'DebDevModern' in result["etc/gtk-2.0/gtkrc"].data
        for name in ("logo_installer.png", "logo_installer_dark.png"):
            entry = result["usr/share/graphics/" + name]
            assert entry.data.startswith(b"\x89PNG\r\n\x1a\n")
            assert struct.unpack(">II", entry.data[16:24]) == (640, 75)
            assert entry.mode & 0o170000 == 0o100000
            assert entry.uid == entry.gid == 0


@pytest.mark.parametrize("graphical", [False, True])
def test_additional_boot_modes_match_grub_and_isolinux(tmp_path, graphical):
    config = {"architecture": "amd64", "with_debian_installer": True, "di_mode": "netinstall"}
    engine = ISOEngine(tmp_path, tmp_path / "root", "preview", config, "mock", ToolchainManager(tmp_path, mode="mock"))
    engine.build_iso()
    # Also exercise systems whose official image has no GTK frontend.
    engine.config["di_graphical"] = graphical
    native = engine.target_root / "usr/lib/ISOLINUX/isolinux.bin"
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"test boot image")
    engine._copy_syslinux_binaries()
    isolinux = (engine.iso_staging / "isolinux/isolinux.cfg").read_text()
    grub = (engine.iso_staging / "boot/grub/grub.cfg").read_text()
    for entry, kernel, initrd, parameters in engine._installer_extra_boot_entries():
        assert f"KERNEL {kernel}\n" in isolinux
        assert f"APPEND initrd={initrd} {parameters} --- quiet" in isolinux
        assert f"linux {kernel} {parameters} --- quiet" in grub
        assert f"menuentry '{entry['label']}'" in grub
    assert ("LABEL install-accessible" in isolinux) == graphical
    assert "rescue/enable=true --- quiet" in isolinux
    assert "priority=low --- quiet" in isolinux


@pytest.mark.parametrize("entry", [
    None,
    {"id": "auto", "label": "Reserved", "frontend": "text", "parameters": []},
    {"id": "safe", "label": "Injected' { reboot; }", "frontend": "text", "parameters": []},
    {"id": "safe", "label": "Safe", "frontend": "gtk", "parameters": ["theme=dark\nreboot"]},
])
def test_boot_entries_reject_unsafe_configuration(entry):
    with pytest.raises(RuntimeConfigError):
        load_runtime({"runtime": {"installer": {"extra_boot_entries": [entry]}}})
