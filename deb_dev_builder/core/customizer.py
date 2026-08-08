import subprocess
from pathlib import Path
from typing import Dict, Any
from deb_dev_builder.core.chroot_manager import ChrootManager

class SystemCustomizer:
    def __init__(self, chroot: ChrootManager, config: Dict[str, Any]):
        self.chroot = chroot
        self.config = config
        self.target_root = chroot.target_root

    def setup_live_users(self):
        if self.chroot.mode == "mock":
            return
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")
        groups = self.config.get("live_groups", ["sudo", "audio", "video", "plugdev", "netdev"])
        groups_str = ",".join(groups)
        try:
            self.chroot.run_in_chroot(["useradd", "-m", "-s", "/bin/bash", "-G", groups_str, str(live_user)], check=False)
            self.chroot.run_in_chroot(f"echo '{live_user}:live' | chpasswd", check=False)
        except Exception:
            pass

        sudoers_file = self.target_root / "etc" / "sudoers.d" / "live_user_nopasswd"
        sudoers_file.parent.mkdir(parents=True, exist_ok=True)
        with open(sudoers_file, "w") as f:
            f.write(f"{live_user} ALL=(ALL) NOPASSWD: ALL\n")

    def configure_system_defaults(self):
        if self.chroot.mode == "mock":
            return
        hostname = self.config.get("hostname", "deb-dev-live")
        with open(self.target_root / "etc" / "hostname", "w") as f:
            f.write(f"{hostname}\n")

        locale = self.config.get("locale", "en_US.UTF-8")
        loc_conf = self.target_root / "etc" / "default" / "locale"
        loc_conf.parent.mkdir(parents=True, exist_ok=True)
        with open(loc_conf, "w") as f:
            f.write(f"LANG={locale}\n")

    def setup_services(self):
        if self.chroot.mode == "mock":
            return
        services = self.config.get("services", [])
        if isinstance(services, dict):
            services = services.get("enable", [])
        for svc in services:
            try:
                self.chroot.run_in_chroot(["systemctl", "enable", str(svc)], check=False)
            except Exception:
                pass

    def configure_autologin(self):
        if self.chroot.mode == "mock":
            return
        dm = self.config.get("display_manager")
        if not dm:
            return
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")
        if dm == "gdm":
            gdm_conf = self.target_root / "etc" / "gdm3" / "daemon.conf"
            gdm_conf.parent.mkdir(parents=True, exist_ok=True)
            with open(gdm_conf, "w") as f:
                f.write(f"[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin={live_user}\n")

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
                "GPGKey=mQENBFk71/ABCADb7...\n"
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
        script_path = self.target_root / "usr" / "local" / "bin" / "create-install-icon.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_content = (
            "#!/bin/bash\n"
            "for user_home in /home/*; do\n"
            "    if [ -d \"$user_home\" ]; then\n"
            "        desktop_dir=\"$user_home/Desktop\"\n"
            "        mkdir -p \"$desktop_dir\"\n"
            "        cat << 'EOF' > \"$desktop_dir/install-deb-dev.desktop\"\n"
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Install System\n"
            "Comment=Install system to disk\n"
            "Exec=sudo calamares\n"
            "Icon=system-software-install\n"
            "Terminal=false\n"
            "Categories=System;\n"
            "EOF\n"
            "        chmod +x \"$desktop_dir/install-deb-dev.desktop\"\n"
            "        chown -R $(basename \"$user_home\"): \"$desktop_dir\"\n"
            "    fi\n"
            "done\n"
        )
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        autostart_dir = self.target_root / "etc" / "xdg" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)
        (autostart_dir / "create-install-icon.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Create Install Icon\n"
            "Exec=/usr/local/bin/create-install-icon.sh\n"
            "Hidden=false\n"
            "NoDisplay=false\n"
        )

    def configure_live_environment(self):
        self.setup_live_users()
        self.configure_system_defaults()
        self.setup_services()
        self.configure_autologin()
        self.configure_zram()
        self.configure_flathub()
        self.configure_polkit_power()
        self.configure_calamares()
        self.configure_artwork()
        self.copy_custom_files()

    def copy_custom_files(self):
        """
        Copies custom files and overlays into the target rootfs chroot.
        Supports both:
        1. Direct rootfs overlay from configs/custom_files/ -> /
        2. Structured JSON custom_files / copy_files entries mapping source -> destination.
        """
        if self.chroot.mode == "mock":
            return

        import shutil
        from deb_dev_builder.core.path_utils import resolve_from_project
        project_root = resolve_from_project("")
        custom_files_dir = project_root / "configs" / "custom_files"

        # 1. Direct overlay from configs/custom_files/ -> target_root/
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

        # 2. Structured list from JSON config
        custom_files_list = list(self.config.get("custom_files", []))
        copy_files = self.config.get("copy_files", [])
        if isinstance(copy_files, list):
            for entry in copy_files:
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        desktop_env = self.config.get("desktop_environment", {})
        if isinstance(desktop_env, dict):
            for entry in desktop_env.get("copy_files", []):
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        if not custom_files_list:
            return

        py_ver = "3.12"
        python_dirs = list(self.target_root.glob("usr/lib/python3.*"))
        if python_dirs:
            py_ver = python_dirs[0].name.replace("python", "")

        for entry in custom_files_list:
            if not isinstance(entry, dict):
                continue
            src_rel = entry.get("source")
            dest_rel = entry.get("destination")
            if not src_rel or not dest_rel:
                continue

            dest_rel = dest_rel.format(python_version=py_ver)
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
        bg_dir = self.target_root / "usr" / "share" / "backgrounds" / "deb-dev-modern"
        bg_dir.mkdir(parents=True, exist_ok=True)
        from deb_dev_builder.core.path_utils import resolve_from_project
        artwork_src = resolve_from_project("artwork/wallpapers/deb-dev-modern.jpg")
        if artwork_src.exists():
            import shutil
            shutil.copy2(artwork_src, bg_dir / "deb-dev-modern.jpg")
