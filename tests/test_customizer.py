import pytest
import subprocess
from pathlib import Path
from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.customizer import SystemCustomizer

def test_system_customizer_mock(tmp_path):
    chroot = ChrootManager(target_root=tmp_path / "chroot", mode="mock")
    config = {
        "base_distro": "debian",
        "hostname": "deb-test",
        "live_user": "testuser",
        "display_manager": "lightdm",
        "with_zram": True,
        "with_flathub": True,
        "with_calamares": True,
    }
    customizer = SystemCustomizer(chroot=chroot, config=config)
    customizer.configure_live_environment()
    assert customizer.config["hostname"] == "deb-test"

def test_theme_mapping_targets_the_real_chroot_attribute(tmp_path):
    root = tmp_path / "chroot"
    chroot = ChrootManager(target_root=root, mode="real")
    SystemCustomizer(chroot=chroot, config={}).apply_theme_assets(chroot)
    assert (root / "usr" / "share" / "backgrounds" / "default-wallpaper.png").is_file()

def test_live_systemd_masks_network_wait_units(tmp_path):
    root = tmp_path / "chroot"
    chroot = ChrootManager(target_root=root, mode="real")
    customizer = SystemCustomizer(chroot, {"variant": "live", "init_system": "systemd"})
    customizer.setup_services()
    systemd_dir = root / "etc/systemd/system"
    for unit in ("NetworkManager-wait-online.service", "systemd-networkd-wait-online.service"):
        assert (systemd_dir / unit).is_symlink()
        assert (systemd_dir / unit).readlink() == Path("/dev/null")

def test_network_sharing_writes_windows_compatible_samba_defaults(tmp_path):
    root = tmp_path / "chroot"
    (root / "usr/sbin").mkdir(parents=True)
    (root / "usr/sbin/smbd").write_text("")
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), {})
    customizer.configure_network_sharing()
    config = (root / "etc/samba/smb.conf").read_text()
    assert "workgroup = WORKGROUP" in config
    assert "min protocol = SMB2" in config
    assert "[homes]" in config

def test_lightdm_runtime_paths_are_prepared_for_live_boot(tmp_path):
    root = tmp_path / "chroot"
    customizer = SystemCustomizer(
        ChrootManager(root, mode="real"),
        {"display_manager": "lightdm"},
    )
    customizer.prepare_lightdm_runtime()
    assert (root / "var/lib/lightdm/data").is_dir()
    assert (root / "var/run/utmp").is_file()

def test_offline_repository_uses_live_medium_path_only(tmp_path):
    root = tmp_path / "chroot"
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), {})
    customizer.configure_offline_repository()
    sources = (root / "etc/apt/sources.list.d/offline-iso.list").read_text()
    assert "file:/run/live/medium/repo/" in sources
    assert "file:/media/cdrom/repo/" not in sources

def test_offline_repository_is_opt_in(tmp_path):
    root = tmp_path / "chroot"
    customizer = SystemCustomizer(
        ChrootManager(root, mode="real"),
        {"offline_repo_packages": ["nala"], "with_offline_repo": False},
    )
    customizer.configure_live_environment()
    assert not (root / "etc/apt/sources.list.d/offline-iso.list").exists()

def test_grub_theme_is_persisted_for_installed_system(tmp_path):
    root = tmp_path / "chroot"
    theme = root / "boot/grub/themes/deb-dev-modern/theme.txt"
    theme.parent.mkdir(parents=True)
    theme.write_text("theme")
    defaults = root / "etc/default/grub"
    defaults.parent.mkdir(parents=True)
    defaults.write_text('GRUB_TIMEOUT=5\nGRUB_THEME="/old/theme.txt"\n')
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), {})
    customizer.configure_grub_theme()
    content = defaults.read_text()
    assert 'GRUB_THEME="/boot/grub/themes/deb-dev-modern/theme.txt"' in content
    assert 'GRUB_GFXMODE="auto"' in content
    assert 'GRUB_TERMINAL_OUTPUT="gfxterm"' in content

def test_calamares_creates_launcher_and_polkit_rule(tmp_path):
    root = tmp_path / "chroot"
    chroot = ChrootManager(target_root=root, mode="real")
    customizer = SystemCustomizer(chroot=chroot, config={"with_calamares": True})
    customizer.configure_calamares()
    assert (root / "usr" / "share" / "applications" / "install-system.desktop").is_file()
    assert (root / "etc" / "polkit-1" / "rules.d" / "49-calamares.rules").is_file()

def test_synaptic_polkit_rule_is_limited_to_active_sudo_users(tmp_path):
    root = tmp_path / "chroot"
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), {})
    customizer.configure_polkit_power()
    rule = (root / "etc/polkit-1/rules.d/20-synaptic-live-user.rules").read_text()
    assert "com.ubuntu.pkexec.synaptic" in rule
    assert "subject.isInGroup('sudo')" in rule


def test_live_user_password_is_passed_to_chpasswd_without_a_shell(tmp_path):
    class RecordingChroot:
        mode = "real"

        def __init__(self, root):
            self.target_root = root
            self.calls = []

        def run_in_chroot(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

    chroot = RecordingChroot(tmp_path / "chroot")
    SystemCustomizer(chroot, {"live_user": {"name": "builder", "password": "safe-password"}}).setup_live_users()

    password_calls = [(command, kwargs) for command, kwargs in chroot.calls if command == ["chpasswd"]]
    assert password_calls == [(["chpasswd"], {"check": False, "input_data": "builder:safe-password\n", "text": True})]
    assert not any(command == ["passwd", "-u", "root"] for command, _ in chroot.calls)


def test_live_user_name_rejects_shell_metacharacters(tmp_path):
    chroot = ChrootManager(target_root=tmp_path / "chroot", mode="real")
    customizer = SystemCustomizer(chroot, {"live_user": {"name": "bad'; touch /owned #"}})
    with pytest.raises(ValueError, match="Invalid live user name"):
        customizer.setup_live_users()


def test_custom_files_use_semantic_system_destinations(tmp_path, monkeypatch):
    root = tmp_path / "chroot"
    source = tmp_path / "configs" / "custom_files"
    (source / "backgrounds").mkdir(parents=True)
    (source / "backgrounds" / "wallpaper.png").write_bytes(b"png")
    (source / "plymouth" / "themes" / "demo").mkdir(parents=True)
    (source / "plymouth" / "plymouthd.conf").write_text("[Daemon]\n")
    (source / "plymouth" / "themes" / "demo" / "demo.plymouth").write_text("[Plymouth Theme]\n")
    (source / "etc").mkdir()
    (source / "etc" / "example.conf").write_text("ok\n")
    monkeypatch.setattr("deb_dev_builder.core.path_utils.resolve_from_project", lambda _: tmp_path)
    customizer = SystemCustomizer(ChrootManager(target_root=root, mode="real"), {})
    customizer.copy_custom_files()
    assert (root / "usr/share/backgrounds/wallpaper.png").is_file()
    assert (root / "etc/plymouth/plymouthd.conf").is_file()
    assert (root / "usr/share/plymouth/themes/demo/demo.plymouth").is_file()
    assert (root / "etc/example.conf").is_file()
    assert not (root / "backgrounds").exists()
    assert not (root / "plymouth").exists()

def test_calamares_files_target_etc_and_follow_init_system(tmp_path, monkeypatch):
    root = tmp_path / "chroot"
    source = tmp_path / "configs" / "custom_files"
    (source / "calamares" / "branding" / "deb-dev-modern").mkdir(parents=True)
    (source / "calamares" / "branding" / "deb-dev-modern" / "branding.desc").write_text("common")
    (source / "calamares" / "systemd").mkdir(parents=True)
    (source / "calamares" / "systemd" / "module.conf").write_text("systemd")
    (source / "calamares" / "non-systemd").mkdir(parents=True)
    (source / "calamares" / "non-systemd" / "module.conf").write_text("sysv")
    monkeypatch.setattr("deb_dev_builder.core.path_utils.resolve_from_project", lambda _: tmp_path)
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), {"init_system": "sysvinit"})
    customizer.copy_custom_files()
    assert (root / "etc/calamares/branding/deb-dev-modern/branding.desc").is_file()
    assert (root / "etc/calamares/module.conf").read_text() == "sysv"


def test_project_calamares_configuration_is_distro_neutral(tmp_path):
    """The bundled Calamares overlay must not carry Arch-only commands."""
    from deb_dev_builder.core.path_utils import resolve_from_project

    calamares = resolve_from_project("") / "configs" / "custom_files" / "calamares"
    assert (calamares / "modules" / "packages.conf").read_text().find("backend: apt") >= 0
    assert "pacman" not in "\n".join(p.read_text() for p in calamares.rglob("*.conf"))
    assert "mkinitcpio" not in "\n".join(p.read_text() for p in calamares.rglob("*.conf"))
    assert "run/live/medium/live/filesystem.squashfs" in (
        calamares / "modules" / "unpackfs.conf"
    ).read_text()
    assert "services-systemd" in (calamares / "systemd" / "settings.conf").read_text()
    assert "services-systemd" not in (calamares / "non-systemd" / "settings.conf").read_text()
    assert (calamares / "branding" / "deb-dev-modern" / "branding.desc").is_file()
    assert "file" in (calamares / "modules" / "partition.conf").read_text()
    packages = (calamares / "modules" / "packages.conf").read_text()
    assert "calamares-settings-debian" not in packages
