import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from deb_dev_builder.core.chroot_manager import ChrootManager

logger = logging.getLogger("customizer")

class SystemCustomizer:
    def __init__(self, chroot: ChrootManager, config: Dict[str, Any]):
        self.chroot = chroot
        self.config = config
        self.target_root = chroot.target_root

    def setup_live_users(self):
        if self.chroot.mode == "mock":
            return
        live_user_cfg = self.config.get("live_user", "liveuser")
        if isinstance(live_user_cfg, dict):
            live_user = live_user_cfg.get("name", "liveuser")
            live_password = live_user_cfg.get("password", "live")
            cfg_groups = live_user_cfg.get("groups", [])
        else:
            live_user = str(live_user_cfg)
            live_password = "live"
            cfg_groups = []

        groups = self.config.get("live_groups") or cfg_groups or ["sudo", "audio", "video", "plugdev", "netdev", "users"]
        groups_str = ",".join(groups)

        try:
            for group in groups:
                lookup = self.chroot.run_in_chroot(["getent", "group", str(group)], check=False)
                if lookup.returncode != 0:
                    self.chroot.run_in_chroot(["groupadd", "-f", str(group)], check=False)

            self.chroot.run_in_chroot(["groupadd", "-f", "nopasswdlogin"], check=False)
            create_user = self.chroot.run_in_chroot(["useradd", "-m", "-s", "/bin/bash", "-G", f"{groups_str},nopasswdlogin", str(live_user)], check=False)
            if create_user.returncode != 0:
                self.chroot.run_in_chroot(["usermod", "-aG", "nopasswdlogin", str(live_user)], check=False)

            self.chroot.run_in_chroot(f"echo '{live_user}:{live_password}' | chpasswd", check=False)
            self.chroot.run_in_chroot(f"echo 'root:{live_password}' | chpasswd", check=False)
            self.chroot.run_in_chroot(["passwd", "-u", str(live_user)], check=False)
            self.chroot.run_in_chroot(["passwd", "-u", "root"], check=False)
        except Exception:
            logger.exception("Could not fully configure live user %s", live_user)

        sudoers_file = self.target_root / "etc" / "sudoers.d" / "live_user_nopasswd"
        sudoers_file.parent.mkdir(parents=True, exist_ok=True)
        with open(sudoers_file, "w") as f:
            f.write(f"{live_user} ALL=(ALL) NOPASSWD: ALL\n")

    def configure_system_defaults(self):
        if self.chroot.mode == "mock":
            return
        base = self.config.get("base_distro", "debian").lower()
        hostname = self.config.get("hostname", f"{base}-modern")
        etc_dir = self.target_root / "etc"
        etc_dir.mkdir(parents=True, exist_ok=True)

        with open(etc_dir / "hostname", "w") as f:
            f.write(f"{hostname}\n")

        hosts_file = etc_dir / "hosts"
        hosts_content = (
            "127.0.0.1   localhost\n"
            f"127.0.1.1   {hostname}.localdomain {hostname}\n"
            "::1         localhost ip6-localhost ip6-loopback\n"
        )
        hosts_file.write_text(hosts_content)

        locale = self.config.get("locale", "en_US.UTF-8")
        loc_conf = etc_dir / "default" / "locale"
        loc_conf.parent.mkdir(parents=True, exist_ok=True)
        with open(loc_conf, "w") as f:
            f.write(f"LANG={locale}\n")

    def setup_services(self):
        if self.chroot.mode == "mock":
            return
        services = self.config.get("services", [])
        if isinstance(services, dict):
            services = services.get("enable", [])
        services_to_enable = list(services)

        for auto_svc in ["NetworkManager"]:
            if auto_svc not in services_to_enable:
                unit = self.target_root / "usr" / "lib" / "systemd" / "system" / f"{auto_svc}.service"
                if unit.exists():
                    services_to_enable.append(auto_svc)

        for svc in services_to_enable:
            try:
                self.chroot.run_in_chroot(["systemctl", "enable", str(svc)], check=False)
            except Exception:
                pass

    def _detect_desktop_session(self) -> str:
        session = self.config.get("desktop_session") or self.config.get("desktop")
        if session:
            session_lower = session.lower()
            if session_lower in {"kde", "plasma"}:
                return "plasma"
            return session_lower
        for session_dir in ["usr/share/xsessions", "usr/share/wayland-sessions"]:
            full_dir = self.target_root / session_dir
            if full_dir.exists():
                for f in sorted(full_dir.glob("*.desktop")):
                    return f.stem
        return "xfce"

    def configure_autologin(self):
        if self.chroot.mode == "mock":
            return
        dm = self.config.get("display_manager")
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        if not dm:
            if (self.target_root / "usr" / "bin" / "sddm").exists():
                dm = "sddm"
            elif (self.target_root / "usr" / "sbin" / "gdm3").exists() or (self.target_root / "usr" / "bin" / "gdm3").exists():
                dm = "gdm3"
            elif (self.target_root / "usr" / "sbin" / "lightdm").exists() or (self.target_root / "usr" / "bin" / "lightdm").exists():
                dm = "lightdm"

        session_name = self._detect_desktop_session()
        # Ensure PAM autologin configuration files permit passwordless login across all display managers
        pam_autologin_content = (
            "#%PAM-1.0\n"
            "auth        sufficient  pam_permit.so\n"
            "account     sufficient  pam_permit.so\n"
            "password    sufficient  pam_permit.so\n"
            "session     required    pam_limits.so\n"
            "session     sufficient  pam_permit.so\n"
        )
        for pam_service in [
            "lightdm", "lightdm-autologin",
            "sddm", "sddm-autologin",
            "gdm", "gdm-autologin", "gdm-password", "gdm3",
            "lxdm", "lxdm-autologin",
            "slim"
        ]:
            pam_file = self.target_root / "etc" / "pam.d" / pam_service
            pam_file.parent.mkdir(parents=True, exist_ok=True)
            pam_file.write_text(pam_autologin_content)

        # SDDM configuration
        for sddm_rel in ["etc/sddm.conf.d/autologin.conf", "etc/sddm.conf"]:
            sddm_conf = self.target_root / sddm_rel
            sddm_conf.parent.mkdir(parents=True, exist_ok=True)
            sddm_conf.write_text(
                f"[Autologin]\nUser={live_user}\nSession={session_name}\nRelogin=false\n\n"
                f"[General]\nDisplayServer=x11\n"
            )

        # Set default-display-manager if lightdm or sddm is active
        if dm:
            dm_path = self.target_root / "etc" / "X11" / "default-display-manager"
            dm_path.parent.mkdir(parents=True, exist_ok=True)
            if dm == "sddm":
                dm_path.write_text("/usr/bin/sddm\n")
            elif dm == "lightdm":
                dm_path.write_text("/usr/sbin/lightdm\n")
            elif dm in {"gdm", "gdm3"}:
                dm_path.write_text("/usr/sbin/gdm3\n")

        # GDM / GDM3 configuration
        gdm_content = (
            "[daemon]\n"
            "AutomaticLoginEnable=true\n"
            f"AutomaticLogin={live_user}\n"
            "TimedLoginEnable=true\n"
            f"TimedLogin={live_user}\n"
            "TimedLoginDelay=0\n"
        )
        for gdm_path in [
            "etc/gdm/custom.conf", "etc/gdm3/custom.conf",
            "etc/gdm/daemon.conf", "etc/gdm3/daemon.conf"
        ]:
            gdm_conf = self.target_root / gdm_path
            gdm_conf.parent.mkdir(parents=True, exist_ok=True)
            gdm_conf.write_text(gdm_content)

        # LightDM configuration
        lightdm_content = (
            "[Seat:*]\n"
            "autologin-guest=false\n"
            f"autologin-user={live_user}\n"
            "autologin-user-timeout=0\n"
            "autologin-in-background=false\n"
            f"autologin-session={session_name}\n"
            "pam-service=lightdm-autologin\n"
            "pam-autologin-service=lightdm-autologin\n"
            "\n"
            "[SeatDefaults]\n"
            "autologin-guest=false\n"
            f"autologin-user={live_user}\n"
            "autologin-user-timeout=0\n"
            "autologin-in-background=false\n"
            f"autologin-session={session_name}\n"
            "pam-service=lightdm-autologin\n"
            "pam-autologin-service=lightdm-autologin\n"
        )
        for conf_rel in ["etc/lightdm/lightdm.conf", "etc/lightdm/lightdm.conf.d/50-autologin.conf"]:
            conf_file = self.target_root / conf_rel
            conf_file.parent.mkdir(parents=True, exist_ok=True)
            conf_file.write_text(lightdm_content)

        # LXDM configuration
        lxdm_conf = self.target_root / "etc" / "lxdm" / "lxdm.conf"
        if lxdm_conf.parent.exists() or (self.target_root / "usr" / "sbin" / "lxdm").exists():
            lxdm_conf.parent.mkdir(parents=True, exist_ok=True)
            lxdm_conf.write_text(
                f"[base]\nautologin={live_user}\nsession={session_name}\n\n"
                f"[server]\n[display]\n[input]\n"
            )

        # SLiM configuration
        slim_conf = self.target_root / "etc" / "slim.conf"
        if slim_conf.parent.exists() or (self.target_root / "usr" / "bin" / "slim").exists():
            slim_content = (
                f"default_user        {live_user}\n"
                "auto_login          yes\n"
                f"login_cmd           exec /bin/sh - ~/.xinitrc {session_name}\n"
            )
            slim_conf.write_text(slim_content)

        # Greetd (Wayland) configuration
        greetd_conf = self.target_root / "etc" / "greetd" / "config.toml"
        if greetd_conf.parent.exists() or (self.target_root / "usr" / "bin" / "greetd").exists():
            greetd_conf.parent.mkdir(parents=True, exist_ok=True)
            greetd_conf.write_text(
                f"[terminal]\nvt = 1\n\n"
                f"[default_session]\ncommand = \"{session_name}\"\nuser = \"{live_user}\"\n\n"
                f"[initial_session]\ncommand = \"{session_name}\"\nuser = \"{live_user}\"\n"
            )

        # TTY1 Console Autologin (Getty fallback for live session without DM)
        getty_dropin = self.target_root / "etc" / "systemd" / "system" / "getty@tty1.service.d" / "autologin.conf"
        getty_dropin.parent.mkdir(parents=True, exist_ok=True)
        getty_dropin.write_text(
            f"[Service]\nExecStart=\nExecStart=-/sbin/agetty -o '-p -f -- \\\\u' --noclear --autologin {live_user} %I $TERM\n"
        )

    def configure_zram(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_zram", True):
            return
        zram_conf = self.target_root / "etc" / "systemd" / "zram-generator.conf"
        zram_conf.parent.mkdir(parents=True, exist_ok=True)
        with open(zram_conf, "w") as f:
            f.write("[zram0]\nzram-size = ram / 2\ncompression-algorithm = zstd\n")

    def configure_flathub(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_flathub", False):
            return
        flatpak_dir = self.target_root / "etc" / "flatpak" / "remotes.d"
        flatpak_dir.mkdir(parents=True, exist_ok=True)
        with open(flatpak_dir / "flathub.flatpakrepo", "w") as f:
            f.write(
                "[Flatpak Remote]\n"
                "Title=Flathub\n"
                "Url=https://dl.flathub.org/repo/\n"
                "GPGKey=https://dl.flathub.org/repo/flathub.gpg\n"
                "Homepage=https://flathub.org/\n"
            )

    def configure_polkit_power(self):
        if self.chroot.mode == "mock":
            return
        polkit_dir = self.target_root / "etc" / "polkit-1" / "rules.d"
        polkit_dir.mkdir(parents=True, exist_ok=True)
        rule_file = polkit_dir / "10-enable-power-actions.rules"
        rule_content = (
            "polkit.addRule(function(action, subject) {\n"
            "    if (action.id.indexOf('org.freedesktop.login1.') === 0 ||\n"
            "        action.id.indexOf('org.freedesktop.upower.') === 0 ||\n"
            "        action.id.indexOf('org.gnome.SessionManager.') === 0 ||\n"
            "        action.id.indexOf('org.freedesktop.consolekit.') === 0) {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n"
        )
        with open(rule_file, "w") as f:
            f.write(rule_content)

    def configure_calamares(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_calamares", False):
            return

        # 1. Polkit rule to allow launching Calamares via pkexec without password prompt
        polkit_dir = self.target_root / "etc" / "polkit-1" / "rules.d"
        polkit_dir.mkdir(parents=True, exist_ok=True)
        (polkit_dir / "49-calamares.rules").write_text(
            "/* Allow live user to launch calamares installer via pkexec without password prompt */\n"
            "polkit.addRule(function(action, subject) {\n"
            "    if ((action.id === 'org.freedesktop.policykit.exec' && action.lookup('program') === '/usr/bin/calamares') ||\n"
            "        action.id.indexOf('com.github.calamares.') === 0 ||\n"
            "        action.id.indexOf('io.calamares.') === 0) {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n"
        )

        desktop_entry = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Name=Install System\n"
            "Name[pt_PT]=Instalar o Sistema\n"
            "Comment=Install system to disk\n"
            "Comment[pt_PT]=Instalar o sistema no disco rígido\n"
            "Exec=pkexec /usr/bin/calamares\n"
            "Icon=system-software-install\n"
            "Terminal=false\n"
            "Categories=System;Qt;\n"
            "StartupNotify=true\n"
        )

        # 2. Add launcher to /usr/share/applications/
        apps_dir = self.target_root / "usr" / "share" / "applications"
        apps_dir.mkdir(parents=True, exist_ok=True)
        app_desktop = apps_dir / "install-system.desktop"
        app_desktop.write_text(desktop_entry)
        app_desktop.chmod(0o755)

        # 3. Add desktop launcher for new users (/etc/skel/Desktop)
        skel_desktop = self.target_root / "etc" / "skel" / "Desktop" / "install-deb-dev.desktop"
        skel_desktop.parent.mkdir(parents=True, exist_ok=True)
        skel_desktop.write_text(desktop_entry)
        skel_desktop.chmod(0o755)

        # 4. Helper script to create and trust desktop icon on live session login
        script_path = self.target_root / "usr" / "local" / "bin" / "add-installer-desktop-icon.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_content = (
            "#!/bin/sh\n"
            "desktop_dir=\"$HOME/Desktop\"\n"
            "mkdir -p \"$desktop_dir\"\n"
            "icon_path=\"$desktop_dir/install-deb-dev.desktop\"\n"
            "cat << 'EOF' > \"$icon_path\"\n"
            f"{desktop_entry}"
            "EOF\n"
            "chmod +x \"$icon_path\"\n"
            "if command -v gio >/dev/null 2>&1; then\n"
            "    gio set --type=string \"$icon_path\" metadata::trusted true 2>/dev/null\n"
            "    if command -v sha256sum >/dev/null 2>&1; then\n"
            "        checksum=$(sha256sum \"$icon_path\" | cut -d' ' -f1)\n"
            "        gio set --type=string \"$icon_path\" metadata::xfce-exe-checksum \"$checksum\" 2>/dev/null\n"
            "    fi\n"
            "fi\n"
            "touch \"$icon_path\"\n"
        )
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        # 5. Autostart desktop entry (/etc/xdg/autostart/)
        autostart_dir = self.target_root / "etc" / "xdg" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)
        (autostart_dir / "create-install-icon.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Create Install Icon\n"
            "Exec=/usr/local/bin/add-installer-desktop-icon.sh\n"
            "Icon=system-software-install\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
        )

        # 6. Add desktop launcher for existing home directories
        home_dir = self.target_root / "home"
        if home_dir.exists():
            for user_dir in home_dir.iterdir():
                if user_dir.is_dir():
                    user_desktop = user_dir / "Desktop" / "install-deb-dev.desktop"
                    user_desktop.parent.mkdir(parents=True, exist_ok=True)
                    user_desktop.write_text(desktop_entry)
                    user_desktop.chmod(0o755)

    def configure_branding(self):
        if self.chroot.mode == "mock":
            return
        etc_dir = self.target_root / "etc"
        etc_dir.mkdir(parents=True, exist_ok=True)

        base_distro = self.config.get("base_distro", "debian").capitalize()
        pretty_name = f"{base_distro} Modern GNU/Linux"
        os_release = etc_dir / "os-release"
        if not os_release.exists():
            os_release.write_text(
                f'NAME="{base_distro} Modern"\n'
                f'ID="{base_distro.lower()}_modern"\n'
                f'ID_LIKE="{base_distro.lower()}"\n'
                f'PRETTY_NAME="{pretty_name}"\n'
                'VERSION="2026.08"\n'
                'VERSION_ID="2026.08"\n'
                'HOME_URL="https://github.com/acoroslinux/deb-dev-builder"\n'
                'SUPPORT_URL="https://github.com/acoroslinux/deb-dev-builder"\n'
                'BUG_REPORT_URL="https://github.com/acoroslinux/deb-dev-builder"\n'
            )

        issue_file = etc_dir / "issue"
        issue_file.write_text(f"{pretty_name} \\r (\\l)\n\n")

    def fix_home_permissions(self):
        if self.chroot.mode == "mock":
            return
        live_user_cfg = self.config.get("live_user", "liveuser")
        if isinstance(live_user_cfg, dict):
            live_user = live_user_cfg.get("name", "liveuser")
        else:
            live_user = str(live_user_cfg)

        user_home = self.target_root / "home" / live_user
        if user_home.exists():
            self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:sudo", f"/home/{live_user}"], check=False)
            self.chroot.run_in_chroot(["chmod", "0755", f"/home/{live_user}"], check=False)

    def configure_machine_id(self):
        if self.chroot.mode == "mock":
            return

        # For a Live system the machine-id MUST be empty (not a real UUID).
        # systemd reads an empty /etc/machine-id and creates a transient one in
        # /run/machine-id without trying to commit it to disk — committing to a
        # read-only rootfs would crash systemd (exit code 1 / Kernel Panic).
        m_id = self.target_root / "etc" / "machine-id"
        m_id.parent.mkdir(parents=True, exist_ok=True)
        m_id.write_text("")  # empty → uninitialized; systemd generates transient ID

        # /var/lib/dbus/machine-id should symlink to /etc/machine-id
        dbus_id = self.target_root / "var" / "lib" / "dbus" / "machine-id"
        dbus_id.parent.mkdir(parents=True, exist_ok=True)
        if dbus_id.exists() or dbus_id.is_symlink():
            try:
                dbus_id.unlink()
            except Exception:
                pass
        try:
            dbus_id.symlink_to(Path("/etc/machine-id"))
        except Exception:
            pass

        # Disable systemd-machine-id-commit so it never tries to write to the
        # read-only squashfs-backed /etc
        masked_dir = self.target_root / "etc" / "systemd" / "system"
        masked_dir.mkdir(parents=True, exist_ok=True)
        commit_mask = masked_dir / "systemd-machine-id-commit.service"
        if not commit_mask.exists():
            try:
                commit_mask.symlink_to("/dev/null")
            except Exception:
                pass

        fstab = self.target_root / "etc" / "fstab"
        if not fstab.exists():
            fstab.parent.mkdir(parents=True, exist_ok=True)
            fstab.write_text("# UNCONFIGURED FSTAB FOR BASE SYSTEM\n")

        init_sym = self.target_root / "sbin" / "init"
        if not init_sym.exists() and (self.target_root / "lib" / "systemd" / "systemd").exists():
            init_sym.parent.mkdir(parents=True, exist_ok=True)
            try:
                init_sym.symlink_to(Path("/lib/systemd/systemd"))
            except Exception:
                pass

        # Purge OpenSSH host keys so live-config generates fresh ones on boot (live-build standard)
        for key_file in (self.target_root / "etc" / "ssh").glob("ssh_host_*"):
            try:
                key_file.unlink()
            except Exception:
                pass

        # Clear udev persistent network rules (live-build standard)
        udev_rules = self.target_root / "etc" / "udev" / "rules.d"
        if udev_rules.exists():
            for rule_file in udev_rules.glob("*persistent-net.rules"):
                try:
                    rule_file.write_text("")
                except Exception:
                    pass

        conf_dir = self.target_root / "etc" / "initramfs-tools" / "conf.d"
        conf_dir.mkdir(parents=True, exist_ok=True)
        (conf_dir / "live.conf").write_text("BOOT=live\n")

        if (self.target_root / "usr" / "sbin" / "update-initramfs").exists():
            try:
                self.chroot.run_in_chroot(["update-initramfs", "-c", "-k", "all"], check=False)
                self.chroot.run_in_chroot(["update-initramfs", "-u", "-k", "all"], check=False)
            except Exception as e:
                logger.warning("Could not update initramfs: %s", e)

    def configure_dbus_launch(self):
        if self.chroot.mode == "mock":
            return
        dbus_launch = self.target_root / "usr" / "bin" / "dbus-launch"
        if not dbus_launch.exists() or dbus_launch.stat().st_size == 0:
            dbus_launch.parent.mkdir(parents=True, exist_ok=True)
            script_content = (
                "#!/bin/sh\n"
                "# Compatibility wrapper for dbus-launch\n"
                'if [ -n "$DBUS_SESSION_BUS_ADDRESS" ]; then\n'
                '    while [ $# -gt 0 ]; do\n'
                '        case "$1" in\n'
                '            --exit-with-session|--sh-syntax|--csh-syntax|--close-stderr) shift ;;\n'
                '            *) break ;;\n'
                '        esac\n'
                '    done\n'
                '    if [ $# -gt 0 ]; then\n'
                '        exec "$@"\n'
                '    else\n'
                '        echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"\n'
                '        exit 0\n'
                '    fi\n'
                'fi\n'
                'exec dbus-run-session "$@"\n'
            )
            dbus_launch.write_text(script_content)
            dbus_launch.chmod(0o755)

    def configure_live_environment(self):
        self.configure_locales()
        self.setup_live_users()
        self.configure_system_defaults()
        self.configure_dbus_launch()
        self.configure_branding()
        self.setup_services()
        self.configure_autologin()
        self.configure_zram()
        self.configure_flathub()
        self.configure_polkit_power()
        self.configure_calamares()
        self.configure_artwork()
        self.copy_custom_files()
        if self.config.get("with_offline_repo") or self.config.get("offline_repo_packages"):
            self.configure_offline_repository()
        self.configure_machine_id()
        self.fix_home_permissions()

    def configure_offline_repository(self):
        """Configure /etc/apt/sources.list.d/offline-iso.list pointing to the ISO offline repository."""
        if self.chroot.mode == "mock":
            return
        sources_d = self.target_root / "etc" / "apt" / "sources.list.d"
        sources_d.mkdir(parents=True, exist_ok=True)
        repo_content = (
            "# Offline Debian/Devuan Live ISO Repository\n"
            "deb [trusted=yes] file:/run/live/medium/repo/ ./\n"
            "deb [trusted=yes] file:/media/cdrom/repo/ ./\n"
        )
        (sources_d / "offline-iso.list").write_text(repo_content)
        logger.info("Configured Debian offline ISO repository in /etc/apt/sources.list.d/offline-iso.list")

    def configure_locales(self):
        if self.chroot.mode == "mock":
            return
        logger.info("🌐 Configuring locales (pt_PT.UTF-8 & en_US.UTF-8)...")
        try:
            self.chroot.run_in_chroot(["apt-get", "install", "-y", "locales"], check=False)
            locale_gen = self.target_root / "etc" / "locale.gen"
            if locale_gen.exists():
                content = locale_gen.read_text()
                new_lines = []
                for line in content.splitlines():
                    if "pt_PT.UTF-8" in line or "en_US.UTF-8" in line:
                        clean_line = line.lstrip("#").strip()
                        new_lines.append(clean_line)
                    else:
                        new_lines.append(line)
                locale_gen.write_text("\n".join(new_lines) + "\n")
            else:
                locale_gen.parent.mkdir(parents=True, exist_ok=True)
                locale_gen.write_text("pt_PT.UTF-8 UTF-8\nen_US.UTF-8 UTF-8\n")

            self.chroot.run_in_chroot(["locale-gen"], check=False)
            self.chroot.run_in_chroot(["update-locale", "LANG=pt_PT.UTF-8", "LC_ALL=pt_PT.UTF-8"], check=False)
        except Exception as e:
            logger.warning("Could not fully configure locales: %s", e)

    def copy_custom_files(self):
        """
        Copies custom files and overlays into the target rootfs chroot.
        """
        if self.chroot.mode == "mock":
            return

        from deb_dev_builder.core.path_utils import resolve_from_project
        project_root = resolve_from_project("")
        custom_files_dir = project_root / "configs" / "custom_files"

        if custom_files_dir.exists() and custom_files_dir.is_dir():
            for item in custom_files_dir.iterdir():
                if item.name == ".gitkeep":
                    continue
                dest_path = self.target_root / item.name
                if item.is_dir():
                    shutil.copytree(item, dest_path, dirs_exist_ok=True, symlinks=True, ignore_dangling_symlinks=True)
                else:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest_path)

        custom_files_list = list(self.config.get("custom_files", []))
        base_copy_files = self.config.get("base_copy_files", [])
        if isinstance(base_copy_files, list):
            for entry in base_copy_files:
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        if not custom_files_list:
            return

        for entry in custom_files_list:
            if not isinstance(entry, dict):
                continue
            src_rel = entry.get("source")
            dest_rel = entry.get("destination")
            if not src_rel or not dest_rel:
                continue

            src_path = custom_files_dir / src_rel
            if not src_path.exists():
                src_path = project_root / src_rel
            dest_path = self.target_root / dest_rel.lstrip("/")

            if not src_path.exists():
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.is_dir():
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True, symlinks=True, ignore_dangling_symlinks=True)
            else:
                shutil.copy2(src_path, dest_path)

            mode_str = entry.get("permissions")
            if mode_str:
                try:
                    mode = int(mode_str, 8)
                    dest_path.chmod(mode)
                except Exception:
                    pass

    def configure_artwork(self):
        """Install custom Debian & Devuan Modern artwork."""
        if self.chroot.mode == "mock":
            return
        bg_dir = self.target_root / "usr" / "share" / "backgrounds"
        bg_dir.mkdir(parents=True, exist_ok=True)
        from deb_dev_builder.core.path_utils import resolve_from_project
        artwork_src = resolve_from_project("artwork/wallpapers/deb-dev-modern.jpg")
        if artwork_src.exists():
            shutil.copy2(artwork_src, bg_dir / "deb-dev-modern.jpg")

            default_wp = bg_dir / "default-wallpaper.png"
            if default_wp.exists() or default_wp.is_symlink():
                default_wp.unlink()
            default_wp.symlink_to(Path("/usr/share/backgrounds/deb-dev-modern.jpg"))

    def fix_home_permissions(self):
        if self.chroot.mode == "mock":
            return
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        home_dir = self.target_root / "home" / live_user
        if home_dir.exists():
            try:
                self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:{live_user}", f"/home/{live_user}"], check=False)
                self.chroot.run_in_chroot(["chmod", "755", f"/home/{live_user}"], check=False)
            except Exception as e:
                logger.warning(f"Could not fix permissions on /home/{live_user}: {e}")

        # Ensure sticky bit on /tmp and /var/tmp
        for tmp_path in ["/tmp", "/var/tmp"]:
            try:
                self.chroot.run_in_chroot(["chmod", "1777", tmp_path], check=False)
            except Exception:
                pass
