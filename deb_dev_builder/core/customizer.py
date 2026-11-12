import os
import json
import re
import shutil
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from deb_dev_builder.core.runtime_config import load_runtime, live_user_settings
from deb_dev_builder.core.chroot_manager import ChrootManager
from .installer_lifecycle import prepare_target_script
from .desktop_sessions import detect_session
from .installer_crypto import keyfile_unlock_script
from .installer_media import target_boot_hook
from .runtime_config import installer_storage_packages
from .installer_grub import grub_lines
from .installer_lifecycle import target_header
from .plymouth_theme import render_theme_script
from .live_media import configure_live_media
from .session_audio import configure_session_audio
from .desktop_integration import configure_desktop_integration
from .path_utils import rootfs_destination
from .calamares_compat import configure_legacy_modules

logger = logging.getLogger("customizer")

_VALID_LOGIN_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}\$?$")

class SystemCustomizer:
    def __init__(self, chroot: ChrootManager, config: Dict[str, Any]):
        self.chroot = chroot
        self.config = config
        self.target_root = chroot.target_root
        self.runtime = load_runtime(config)

    def setup_live_users(self):
        if self.chroot.mode == "mock":
            return
        live_user_cfg = live_user_settings(self.config, self.runtime)
        live_user = live_user_cfg["name"]
        live_password = live_user_cfg.get("password")
        groups = self.config.get("live_groups", live_user_cfg["groups"])
        user_policy = self.runtime["users"]

        if not isinstance(live_user, str) or live_user == "root" or not _VALID_LOGIN_NAME.fullmatch(live_user):
            raise ValueError(f"Invalid live user name: {live_user!r}")
        if live_password is not None and (not isinstance(live_password, str) or not live_password or any(character in live_password for character in ("\n", "\r", "\0"))):
            raise ValueError("The live user password must be a non-empty single-line string")

        try:
            for group in groups:
                lookup = self.chroot.run_in_chroot(["getent", "group", str(group)], check=False)
                if lookup.returncode != 0:
                    self.chroot.run_in_chroot(["groupadd", "-f", str(group)], check=False)

            self.chroot.run_in_chroot(["groupadd", "-f", user_policy["autologin_group"]], check=False)
            login_groups = ",".join([*groups, user_policy["autologin_group"]])
            create_user = self.chroot.run_in_chroot(["useradd", "-m", "-s", user_policy["shell"], "-G", login_groups, str(live_user)], check=False)
            if create_user.returncode != 0:
                self.chroot.run_in_chroot(["usermod", "-G", login_groups, str(live_user)], check=True)

            # Accounts are locked by default. Autologin is configured through PAM,
            # while an interactive password is only set when a profile explicitly
            # supplies one. Never set or unlock the root account in a live image.
            if live_password:
                password_result = self.chroot.run_in_chroot(
                    ["chpasswd"],
                    check=False,
                    input_data=f"{live_user}:{live_password}\n",
                    text=True,
                )
                if password_result.returncode != 0:
                    raise RuntimeError(f"Could not set password for live user {live_user}")
        except Exception:
            logger.exception("Could not fully configure live user %s", live_user)
            raise

        sudoers_file = self.target_root / user_policy["sudoers_path"]
        if user_policy["admin_group"] in groups:
            sudoers_file.parent.mkdir(parents=True, exist_ok=True)
            sudoers_file.write_text(user_policy["sudoers_template"].format(user=live_user))
            sudoers_file.chmod(int(user_policy["sudoers_mode"], 8))
        else:
            sudoers_file.unlink(missing_ok=True)

        # live-config must reuse the account prepared here rather than create
        # its default user or grant fresh sudo permissions during first boot.
        live_config = self.target_root / "etc/live/config.conf.d/10-deb-dev-user.conf"
        live_config.parent.mkdir(parents=True, exist_ok=True)
        live_config.write_text(
            f"LIVE_USERNAME={shlex.quote(live_user)}\n"
            f"LIVE_USER_DEFAULT_GROUPS={shlex.quote(','.join(groups))}\n"
            "LIVE_CONFIG_NOCOMPONENTS=user-setup,sudo\n"
        )

    def configure_system_defaults(self):
        if self.chroot.mode == "mock":
            return
        system_config = self.config.get("system", {})
        hostname = self.config.get("hostname", system_config.get("hostname", self.runtime["system"]["hostname"]))
        etc_dir = self.target_root / "etc"
        etc_dir.mkdir(parents=True, exist_ok=True)

        with open(etc_dir / "hostname", "w") as f:
            f.write(f"{hostname}\n")

        hosts_file = etc_dir / "hosts"
        hosts_content = self.runtime["system"]["hosts_template"].format(hostname=hostname)
        hosts_file.write_text(hosts_content)

        locale = self.config.get("locale", system_config.get("locale", self.runtime["system"]["locale"]))
        loc_conf = etc_dir / "default" / "locale"
        loc_conf.parent.mkdir(parents=True, exist_ok=True)
        with open(loc_conf, "w") as f:
            f.write(f"LANG={locale}\n")

        timezone = self.config.get("timezone", system_config.get("timezone", self.runtime["system"]["timezone"]))
        (etc_dir / "timezone").write_text(f"{timezone}\n")
        zoneinfo = self.target_root / "usr" / "share" / "zoneinfo" / timezone
        localtime = etc_dir / "localtime"
        if zoneinfo.exists():
            if localtime.exists() or localtime.is_symlink():
                localtime.unlink()
            localtime.symlink_to(Path("/usr/share/zoneinfo") / timezone)

    def setup_services(self):
        if self.chroot.mode == "mock":
            return
        service_config = self.config.get("services", [])
        services_to_enable = list(service_config.get("enable", [])) if isinstance(service_config, dict) else list(service_config)
        services_to_disable = list(service_config.get("disable", [])) if isinstance(service_config, dict) else []
        init_system = self.config.get("init_system", self.runtime["system"]["init_system"])

        policy = self.runtime["services"]
        masked = []
        if self.config.get("output_format") == "iso":
            masked.extend(policy["iso_mask"].get(init_system, []))
        if self.config.get("variant") == "live":
            masked.extend(policy["live_mask"].get(init_system, []))
        for service in masked:
            if service not in services_to_disable:
                services_to_disable.append(service)
        for auto_svc in policy["auto_enable"]:
            unit = self.target_root / "usr/lib/systemd/system" / f"{auto_svc}.service"
            name = self._service_name(auto_svc, init_system)
            native = (unit.exists() if init_system == "systemd" else
                      (self.target_root / policy["runit_source"] / name).is_dir() if init_system == "runit" else
                      (self.target_root / "etc/init.d" / name).is_file())
            if native and auto_svc not in services_to_enable:
                services_to_enable.append(auto_svc)

        display_manager = self.config.get("display_manager")
        if display_manager:
            manager = self.runtime["desktop"]["display_manager_aliases"].get(display_manager, display_manager)
            display_service = self.runtime["desktop"]["display_managers"].get(manager, {}).get("service", manager)
            if display_service not in services_to_enable:
                services_to_enable.append(display_service)

        for svc in services_to_enable:
            service_name = self._service_name(str(svc), init_system)
            if init_system == "systemd":
                command = ["systemctl", "enable", str(svc)]
            elif init_system == "openrc":
                command = ["rc-update", "add", service_name, policy["openrc_runlevel"]]
            elif init_system == "sysvinit":
                command = ["update-rc.d", service_name, "defaults"]
            elif init_system == "s6":
                command = ["s6-rc-bundle-update", "add", "default", str(svc)]
            elif init_system == "runit":
                source = self.target_root / policy["runit_source"] / service_name
                destination = self.target_root / policy["runit_target"] / service_name
                if source.is_dir() and not destination.is_symlink():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(os.path.relpath(source, destination.parent))
                continue
            else:
                logger.warning("Unknown init system %s; cannot enable %s", init_system, svc)
                continue
            result = self.chroot.run_in_chroot(command, check=False)
            if result.returncode != 0:
                logger.warning("Could not enable service %s with %s", svc, init_system)

        for svc in services_to_disable:
            service_name = self._service_name(str(svc), init_system)
            if init_system == "systemd":
                command = ["systemctl", "disable", str(svc)]
            elif init_system == "openrc":
                command = ["rc-update", "del", service_name, policy["openrc_runlevel"]]
            elif init_system == "sysvinit":
                command = ["update-rc.d", service_name, "disable"]
            elif init_system == "runit":
                destination = self.target_root / policy["runit_target"] / service_name
                if destination.is_symlink():
                    destination.unlink()
                continue
            else:
                continue
            self.chroot.run_in_chroot(command, check=False)
            if init_system == "systemd" and (self.config.get("variant") == "live" or svc in masked):
                unit_path = self.target_root / "etc" / "systemd" / "system" / str(svc)
                if unit_path.exists() or unit_path.is_symlink():
                    unit_path.unlink()
                unit_path.parent.mkdir(parents=True, exist_ok=True)
                unit_path.symlink_to(Path("/dev/null"))

    def _service_name(self, service: str, init_system: str) -> str:
        if init_system == "systemd":
            return service
        name = service.removesuffix(".service")
        return self.runtime["services"]["aliases"].get(name, name)

    def _detect_desktop_session(self) -> str:
        return detect_session(self.target_root, self.config, self.runtime)

    def configure_autologin(self):
        if self.chroot.mode == "mock":
            return
        policy = self.runtime["desktop"]
        managers = policy["display_managers"]
        dm = self.config.get("display_manager")
        if not dm:
            dm = next((name for name, spec in managers.items()
                       if any((self.target_root / binary).exists() for binary in spec["binaries"])), None)
        dm = policy["display_manager_aliases"].get(dm, dm)
        context = {
            "live_user": live_user_settings(self.config, self.runtime)["name"],
            "session": self._detect_desktop_session(),
            "display_server": policy["display_servers"].get(self.config.get("session_type"), policy["display_servers"]["default"]),
        }
        mode = int(policy["file_mode"], 8)
        if dm in managers:
            manager = managers[dm]
            base_distro = self.config.get("base_distro", self.runtime["system"]["base_distro"])
            force_live_xorg = (
                self.config.get("with_calamares", False)
                and self.config.get("suite") in manager.get("live_xorg_suites", {}).get(base_distro, [])
            )
            # Keep the vendor's account/session policy; only fill missing PAM files.
            if manager.get("pam_service"):
                pam_file = self.target_root / "etc/pam.d" / manager["pam_service"]
                if not pam_file.exists():
                    pam_file.parent.mkdir(parents=True, exist_ok=True)
                    pam_file.write_text(policy["pam_autologin_template"])
                    pam_file.chmod(mode)
            for relative, template in manager["files"].items():
                # Daedalus live GNOME can leave XWayland clients blocked during
                # installer startup. These live-only files are removed on install.
                if force_live_xorg:
                    template = template.replace("[daemon]\n", "[daemon]\nWaylandEnable=false\n", 1)
                destination = self.target_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(template.format_map(context))
                destination.chmod(mode)
            default_manager = self.target_root / policy["default_manager_file"]
            default_manager.parent.mkdir(parents=True, exist_ok=True)
            default_manager.write_text(manager["executable"] + "\n")
            default_manager.chmod(mode)
        if self.config.get("init_system", self.runtime["system"]["init_system"]) == "systemd":
            console = policy["console_autologin"]
            destination = self.target_root / console["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(console["content"].format_map(context))
            destination.chmod(mode)

    def configure_zram(self):
        if self.chroot.mode == "mock":
            return
        policy = self.runtime["customization"]["zram"]
        if not self.config.get("with_zram", policy["enabled_by_default"]):
            return
        init_system = self.config.get("init_system", self.runtime["system"]["init_system"])
        template = policy.get(init_system, policy["default"])
        destination = self.target_root / template["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(template["content"])

    def configure_flathub(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_flathub", False):
            return
        # Import the official descriptor, including its embedded signing key.
        # GPGKey in a .flatpakrepo is base64 data, not a key download URL.
        policy = self.runtime["customization"]["flathub"]
        stale = self.target_root / policy["staged_descriptor"]
        stale.unlink(missing_ok=True)
        self.chroot.run_in_chroot([
            "flatpak", "remote-add", "--system", "--if-not-exists",
            policy["name"], policy["url"],
        ], check=True)

    def configure_network_sharing(self):
        """Install conservative SMB defaults for Linux/Windows interoperability."""
        if self.chroot.mode == "mock":
            return
        policy = self.runtime["customization"]["samba"]
        samba_conf = self.target_root / policy["path"]
        if not samba_conf.parent.exists() and not (self.target_root / policy["binary"]).exists():
            return
        samba_conf.parent.mkdir(parents=True, exist_ok=True)
        if samba_conf.exists() and samba_conf.stat().st_size > 0:
            return
        samba_conf.write_text(policy["content"])

    def configure_plymouth_theme(self):
        """Select the bundled Plymouth theme and include it in initramfs."""
        if self.chroot.mode == "mock":
            return
        policy = self.runtime["customization"]["plymouth"]
        theme_dir = self.target_root / policy["directory"]
        if not theme_dir.is_dir():
            return
        script = self.target_root / policy["script_file"]
        locale = self.config.get('locale', self.config.get('system', {}).get('locale', self.runtime['system']['locale']))
        script.write_text(render_theme_script(script.read_text(), policy, locale=locale))
        configure_live_media(self.target_root, self.runtime, locale=locale)
        selector = self.target_root / policy["selector"]
        if selector.exists():
            result = self.chroot.run_in_chroot(
                [*policy["select_command"], "-R", policy["name"]], check=True
            )
            if result.returncode != 0:
                logger.warning("Could not activate Plymouth theme %s", policy["name"])

    def configure_grub_theme(self):
        """Persist the bundled GRUB theme for the installed system."""
        if self.chroot.mode == "mock":
            return
        if self.config.get("base_distro", self.runtime["system"]["base_distro"]) not in {"debian", "devuan"}:
            return
        bootloader = self.config.get("bootloader_type") or self.config.get("bootloader", {}).get("type", "")
        if bootloader and "grub" not in str(bootloader).lower():
            return
        policy = self.runtime["customization"]["grub"]
        self.target_root.joinpath("etc").mkdir(parents=True, exist_ok=True)
        subprocess.run(["/bin/sh"], input="\n".join([*target_header(str(self.target_root.resolve())), *grub_lines(self.runtime)]) + "\n", text=True, check=True)

    def prepare_lightdm_runtime(self):
        """Create runtime paths expected by LightDM in a live rootfs."""
        if self.chroot.mode == "mock" or not self.config.get("display_manager"):
            return
        lightdm_data = self.target_root / "var" / "lib" / "lightdm" / "data"
        lightdm_data.mkdir(parents=True, exist_ok=True)
        # systemd-tmpfiles normally creates utmp, but live boots can reach
        # LightDM before that hook has run.
        utmp = self.target_root / "var" / "run" / "utmp"
        utmp.parent.mkdir(parents=True, exist_ok=True)
        if not utmp.exists():
            utmp.touch()
            utmp.chmod(0o664)

    def configure_polkit_power(self):
        if self.chroot.mode == "mock":
            return
        for relative, template in self.runtime["customization"]["polkit_files"].items():
            destination = self.target_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(template.replace("@@ADMIN_GROUP@@", self.runtime["users"]["admin_group"]))

    def configure_calamares(self):
        if self.chroot.mode == "mock" or not self.config.get("with_calamares", False):
            return
        policy = self.runtime["customization"]["calamares"]

        if "live_remove_packages" in self.config:
            packages_path = self.target_root / policy["packages_config"]
            if Path(policy["packages_config"]).is_absolute() or ".." in Path(policy["packages_config"]).parts:
                raise ValueError("Invalid Calamares package configuration path")
            packages_path.parent.mkdir(parents=True, exist_ok=True)
            packages_path.write_text(json.dumps({
                "backend": policy["packages_backend"], "skip_if_no_internet": False,
                "update_db": False, "update_system": False,
                "operations": [{"remove": self.config["live_remove_packages"]}] if self.config["live_remove_packages"] else [],
            }, indent=2) + "\n")

        def relative_path(value):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Installer artifact path must be relative: {value!r}")
            return path

        def target_path(key):
            return self.target_root / relative_path(policy[key])

        def write_artifact(key, content, mode=0o644):
            destination = target_path(key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            destination.chmod(mode)

        display_manager = self.config.get("display_manager")
        if display_manager:
            manager = policy["display_manager_aliases"].get(display_manager, display_manager)
            write_artifact("display_manager_config", json.dumps({
                "displaymanagers": [manager], "basicSetup": False, "sysconfigSetup": False,
            }, indent=2) + "\n")

        grub_config = self.target_root / self.runtime["customization"]["grub"]["calamares_config"]
        grub_config.parent.mkdir(parents=True, exist_ok=True)
        grub_policy = self.runtime["customization"]["grub"]
        defaults = dict(grub_policy["settings"])
        # The native module owns installation-specific storage/kernel options.
        defaults.pop("GRUB_CMDLINE_LINUX", None)
        # grubcfg replaces the live defaults, so the installed theme must be
        # included explicitly alongside the native module's storage options.
        if (self.target_root / grub_policy["theme_file"]).is_file():
            defaults[grub_policy["theme_key"]] = "/" + grub_policy["theme_file"]
        grub_config.write_text(json.dumps({**grub_policy["calamares"], "defaults": defaults,
                                         "kernel_params": [*grub_policy["kernel_parameters"], *grub_policy["plymouth_parameters"]]}, indent=2) + "\n")

        configure_legacy_modules(self.target_root, self.runtime["build"]["installer_audit"]["module_directories"])

        write_artifact("prepare_script", prepare_target_script(self.runtime).decode(), 0o755)
        write_artifact("keyfile_script", keyfile_unlock_script(self.runtime["build"]["installer_storage"]).decode(), 0o755)
        write_artifact("validate_script", target_boot_hook(
            None, self.runtime["build"]["installer_storage"], installer_storage_packages(self.config, self.runtime),
            package_operations=False, regenerate=False, failure_code=None,
            identity=self.runtime["customization"]["identity"],
            plymouth=self.runtime["customization"]["plymouth"],
        ).decode(), 0o755)
        for name in ("prepare", "keyfile", "validate"):
            # Calamares 3.2-era builds use @@ROOT@@ while newer releases use
            # ${ROOT}. Pass both forms; the target script selects the first
            # non-empty expansion so the same ISO config works across versions.
            command = shlex.quote("/" + policy[name + "_script"]) + ' "${ROOT}" "@@ROOT@@"'
            write_artifact(name + "_config", json.dumps({
                "dontChroot": True, "timeout": policy["validation_timeout"], "script": [command],
            }, indent=2) + "\n")

        filename = policy["launcher_filename"]
        if not filename or Path(filename).name != filename:
            raise ValueError("The installer launcher filename must be a basename")
        executable = policy["executable"]
        entry = policy["desktop_entry"].format_map(policy)
        write_artifact("polkit_rule", (
            "/* Permit the active local administrator to launch the installer. */\n"
            "polkit.addRule(function(action, subject) {\n"
            "    if (!subject.local || !subject.active || !subject.isInGroup("
            + json.dumps(self.runtime["users"]["admin_group"]) + ")) return;\n"
            "    if ((action.id === 'org.freedesktop.policykit.exec' && action.lookup('program') === "
            + json.dumps(executable) + ") ||\n"
            "        action.id.indexOf('com.github.calamares.') === 0 ||\n"
            "        action.id.indexOf('io.calamares.') === 0) {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n"
        ))
        live_user = live_user_settings(self.config, self.runtime)["name"]
        write_artifact("removeuser_config", f"---\nusername: {live_user}\n")
        write_artifact("application_launcher", entry, 0o755)

        # Do not seed installer icons into accounts created by Calamares.
        # Also migrate workdirs built before shortcuts were live-session-only.
        skel = self.target_root / "etc/skel"
        if skel.is_dir():
            for old_icon in skel.rglob(filename):
                if old_icon.is_file() or old_icon.is_symlink():
                    old_icon.unlink()

        live_option = next((option for option in self.runtime["artifacts"]["iso"]["required_kernel_options"]
                            if option.startswith("boot=")), None)
        if not live_option:
            raise ValueError("Calamares shortcuts require a configured live boot kernel option")
        helper = "\n".join([
            "#!/bin/sh",
            "# Only create shortcuts in a running live session with its installer present.",
            f"[ -x {shlex.quote(executable)} ] || exit 0",
            f'case " $(cat {shlex.quote(policy["cmdline_file"])} 2>/dev/null) " in',
            f"    *{shlex.quote(' ' + live_option + ' ')}*) ;;",
            "    *) exit 0 ;;",
            "esac",
            'desktop_dir=""',
            "if command -v xdg-user-dir >/dev/null 2>&1; then",
            '    desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null || true)',
            "fi",
            'if [ -z "$desktop_dir" ] || [ "$desktop_dir" = "$HOME" ]; then',
            '    desktop_dir="$HOME"/' + shlex.quote(policy["desktop_directory_fallback"]),
            "fi",
            'mkdir -p "$desktop_dir"',
            'icon_path="$desktop_dir"/' + shlex.quote(filename),
            "printf '%s' " + shlex.quote(entry) + ' > "$icon_path"',
            'chmod +x "$icon_path"',
            "if command -v gio >/dev/null 2>&1; then",
            '    gio set --type=string "$icon_path" metadata::trusted true 2>/dev/null',
            "    if command -v sha256sum >/dev/null 2>&1; then",
            '        checksum=$(sha256sum "$icon_path" | cut -d\' \' -f1)',
            '        gio set --type=string "$icon_path" metadata::xfce-exe-checksum "$checksum" 2>/dev/null',
            "    fi",
            "fi",
            'touch "$icon_path"',
            "",
        ])
        write_artifact("helper_script", helper, 0o755)
        write_artifact("autostart_launcher", policy["autostart_entry"].format_map(policy))

        # The live account is removed by Calamares before creating target users.
        # Prepare only that account; the guarded helper handles localized Desktop paths.
        home = self.target_root / "home" / live_user
        if home.is_dir():
            icon = home / relative_path(policy["desktop_directory_fallback"]) / filename
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_text(entry, encoding="utf-8")
            icon.chmod(0o755)

        # shellprocess runs in the live environment with every path rooted in
        # Calamares' installed target. Never remove anything from the live root.
        def rooted(value):
            return '"$target_root"/' + shlex.quote(str(relative_path(value)))

        # Each shellprocess command runs in a fresh shell. Resolve either
        # Calamares root placeholder in every command, then guard the target.
        def with_root(command):
            return ('target_root="${ROOT}"; [ -n "$target_root" ] || target_root="@@ROOT@@"; '
                    'test -n "$target_root" && test "$target_root" != / && '
                    'test -d "$target_root/etc" && ' + command)

        commands = []
        artifacts = [policy[key] for key in ("autostart_launcher", "helper_script", "application_launcher", "polkit_rule",
                                             "prepare_script", "keyfile_script", "validate_script")]
        artifacts.append(self.runtime["customization"]["offline_repository"]["source_file"])
        commands.append(with_root("rm -f -- " + " ".join(rooted(path) for path in artifacts)))
        for search_root in policy["cleanup_search_roots"]:
            path = rooted(search_root)
            commands.append(with_root(f"if [ -d {path} ]; then find {path} -type f -name {shlex.quote(filename)} -delete; fi"))
        cleanup = {"dontChroot": True, "timeout": policy["cleanup_timeout"],
                   "i18n": policy["cleanup_i18n"], "script": commands}
        write_artifact("cleanup_config", json.dumps(cleanup, indent=2, ensure_ascii=False) + "\n")

    def configure_branding(self):
        if self.chroot.mode == "mock":
            return
        etc_dir = self.target_root / "etc"
        etc_dir.mkdir(parents=True, exist_ok=True)

        policy = self.runtime["customization"]["branding"]
        base_name = self.config.get("base_distro", self.runtime["system"]["base_distro"]).capitalize()
        context = {"base_name": base_name, "base_id": base_name.lower()}
        context["pretty_name"] = policy["pretty_name"].format_map(context)
        os_release = etc_dir / "os-release"
        if not os_release.exists():
            os_release.write_text(policy["os_release"].format_map(context))
        (etc_dir / "issue").write_text(policy["issue"].format_map(context))

    def configure_machine_id(self):
        if self.chroot.mode == "mock":
            return

        # For a Live system the machine-id MUST be empty (not a real UUID).
        # systemd reads an empty /etc/machine-id and creates a transient one in
        # /run/machine-id without trying to commit it to disk — committing to a
        # read-only rootfs would crash systemd (exit code 1 / Kernel Panic).
        identity = self.runtime["customization"]["identity"]
        m_id = self.target_root / identity["machine_id"]
        m_id.parent.mkdir(parents=True, exist_ok=True)
        m_id.write_text("")  # empty → uninitialized; systemd generates transient ID

        # /var/lib/dbus/machine-id should symlink to /etc/machine-id
        dbus_id = self.target_root / identity["dbus_id"]
        dbus_id.parent.mkdir(parents=True, exist_ok=True)
        if dbus_id.exists() or dbus_id.is_symlink():
            try:
                dbus_id.unlink()
            except Exception:
                pass
        try:
            dbus_id.symlink_to(identity["dbus_target"])
        except Exception:
            pass

        # Disable systemd-machine-id-commit so it never tries to write to the
        # read-only squashfs-backed /etc
        commit_mask = self.target_root / identity["commit_mask"]
        masked_dir = commit_mask.parent
        masked_dir.mkdir(parents=True, exist_ok=True)
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

        # The ISO selects live boot through its kernel command line. Persisting
        # BOOT=live here survives Calamares' removal of live-boot, leaving the
        # installed initramfs sourcing a missing /scripts/live and exiting PID 1.
        # Also remove the override when reusing an older builder rootfs.
        live_boot_override = self.target_root / "etc/initramfs-tools/conf.d/live.conf"
        live_boot_override.unlink(missing_ok=True)

        if (self.target_root / "usr" / "sbin" / "update-initramfs").exists():
            try:
                result = self.chroot.run_in_chroot(["update-initramfs", "-u", "-k", "all"], check=False)
                if result.returncode != 0:
                    raise RuntimeError(f"update-initramfs exited with code {result.returncode}")
                kernel_files = list((self.target_root / "boot").glob("vmlinuz-*"))
                initrd_files = list((self.target_root / "boot").glob("initrd.img-*"))
                if kernel_files and not initrd_files:
                    raise RuntimeError("no initrd.img-* was produced for the installed kernel")
            except Exception as e:
                logger.error("Could not update initramfs: %s", e)
                raise

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
        # Install skel files and theme assets before creating users and
        # selecting themes. Runtime settings must use the selected profiles.
        self.copy_custom_files()
        if self.chroot.mode != 'mock':
            configure_session_audio(self.target_root, self.runtime, self.config.get('init_system', self.runtime['system']['init_system']))
            configure_desktop_integration(self.target_root, self.config, self.runtime)
        self.setup_live_users()
        self.configure_system_defaults()
        self.configure_dbus_launch()
        self.configure_branding()
        self.prepare_lightdm_runtime()
        self.setup_services()
        self.configure_autologin()
        self.configure_zram()
        self.configure_flathub()
        self.configure_network_sharing()
        self.configure_plymouth_theme()
        self.configure_grub_theme()
        self.configure_polkit_power()
        self.configure_calamares()
        self.configure_artwork()
        if self.config.get("with_offline_repo"):
            self.configure_offline_repository()
        self.configure_machine_id()
        self.fix_home_permissions()

    def configure_offline_repository(self):
        """Configure the live session's local APT source from its JSON policy."""
        if self.chroot.mode == "mock":
            return
        policy = self.runtime["customization"]["offline_repository"]
        relative = Path(policy["source_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid offline repository source path")
        source = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(policy["entry_template"].format(uri=policy["live_uri"]))
        logger.info("Configured live offline repository at %s", source)

    def configure_locales(self):
        if self.chroot.mode == "mock":
            return
        system_config = self.config.get("system", {})
        locale = self.config.get("locale", system_config.get("locale", self.runtime["system"]["locale"]))
        locales = list(dict.fromkeys([locale, *self.runtime["system"]["fallback_locales"]]))
        logger.info("🌐 Configuring locales: %s", ", ".join(locales))
        try:
            self.chroot.run_in_chroot(["apt-get", "install", "-y", "locales"], check=False)
            locale_gen = self.target_root / "etc" / "locale.gen"
            if locale_gen.exists():
                content = locale_gen.read_text()
                new_lines = []
                for line in content.splitlines():
                    if any(selected in line for selected in locales):
                        clean_line = line.lstrip("#").strip()
                        new_lines.append(clean_line)
                    else:
                        new_lines.append(line)
                locale_gen.write_text("\n".join(new_lines) + "\n")
            else:
                locale_gen.parent.mkdir(parents=True, exist_ok=True)
                locale_gen.write_text("".join(f"{selected} UTF-8\n" for selected in locales))

            self.chroot.run_in_chroot(["locale-gen"], check=False)
            self.chroot.run_in_chroot(["update-locale", f"LANG={locale}"], check=False)
        except Exception as e:
            logger.warning("Could not fully configure locales: %s", e)


    def apply_theme_assets(self, chroot):
        import json
        import shutil
        from pathlib import Path
        try:
            from core.path_utils import resolve_from_project
        except ImportError:
            try:
                # deb_dev_builder/core or fedora_builder/core
                from ..core.path_utils import resolve_from_project
            except ImportError:
                def resolve_from_project(p): return Path(p)
            
        mapping_file = resolve_from_project(self.runtime["customization"]["overlay"]["asset_mapping"])
        assets_dir = resolve_from_project(self.runtime["customization"]["overlay"]["assets_directory"])
        
        if not mapping_file.exists():
            return
            
        try:
            with open(mapping_file, 'r') as f:
                mapping = json.load(f)
                
            for asset_name, paths in mapping.items():
                src = assets_dir / asset_name
                if src.exists():
                    for target in paths:
                        relative = Path(target.lstrip('/'))
                        if '..' in relative.parts:
                            raise ValueError(f"Unsafe theme asset destination: {target}")
                        signature = src.read_bytes()[:8]
                        suffix = relative.suffix.lower()
                        if suffix == '.png' and signature != b'\x89PNG\r\n\x1a\n':
                            raise ValueError(f"Theme asset {src} is not PNG but destination {target} requires PNG")
                        if suffix in ('.jpg', '.jpeg') and not signature.startswith(b'\xff\xd8\xff'):
                            raise ValueError(f"Theme asset {src} is not JPEG but destination {target} requires JPEG")
                        target_path = chroot.target_root / relative
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, target_path)
        except Exception as e:
            raise ValueError(f"Failed to apply theme assets: {e}") from e

    def copy_custom_files(self):
        """
        Copies custom files and overlays into the target rootfs chroot.
        """
        if self.chroot.mode == "mock":
            return

        from deb_dev_builder.core.path_utils import resolve_from_project
        project_root = resolve_from_project("")
        overlay = self.runtime["customization"]["overlay"]
        custom_files_dir = project_root / overlay["source"]

        if custom_files_dir.exists() and custom_files_dir.is_dir():
            # ``custom_files`` is a semantic overlay, not a raw copy into the
            # chroot root. Keep conventional Unix trees (etc/, usr/, boot/)
            # rooted, and map project-specific asset groups to their package
            # locations. Unknown groups are kept under /usr/local/share so a
            # typo can never create an unexpected /<name> directory.
            destination_prefixes = {name: Path(destination) for name, destination in overlay["destinations"].items()}

            for source in custom_files_dir.rglob("*"):
                if not source.is_file() or source.name == ".gitkeep":
                    continue
                relative = source.relative_to(custom_files_dir)
                group = relative.parts[0]
                if group == "calamares" and len(relative.parts) > 2 and relative.parts[1] in {"systemd", "non-systemd"}:
                    selector = relative.parts[1]
                    init_system = str(self.config.get("init_system", self.runtime["system"]["init_system"])).lower()
                    if (selector == "systemd") != (init_system == "systemd"):
                        continue
                    relative = Path("calamares") / Path(*relative.parts[2:])
                if group == "plymouth":
                    if relative == Path("plymouth/plymouthd.conf"):
                        destination = Path("etc/plymouth/plymouthd.conf")
                    elif len(relative.parts) > 2 and relative.parts[1] == "themes":
                        destination = Path("usr/share/plymouth") / Path(*relative.parts[1:])
                    else:
                        destination = Path("usr/share/plymouth") / Path(*relative.parts[1:])
                elif group in overlay["rooted_groups"]:
                    destination = relative
                elif group in destination_prefixes:
                    destination = destination_prefixes[group] / Path(*relative.parts[1:])
                else:
                    destination = Path(overlay["fallback"]) / relative
                dest_path = rootfs_destination(self.target_root, destination)
                if dest_path.is_symlink():
                    dest_path.unlink()
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest_path)

        custom_files_list = list(self.config.get("custom_files", []))
        base_copy_files = self.config.get("base_copy_files", [])
        if isinstance(base_copy_files, list):
            for entry in base_copy_files:
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        if not custom_files_list:
            self.apply_theme_assets(self.chroot)
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
            dest_path = rootfs_destination(self.target_root, dest_rel.lstrip('/'))
            if dest_path.is_symlink():
                dest_path.unlink()

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

        self.apply_theme_assets(self.chroot)

    def configure_artwork(self):
        """Install custom Debian & Devuan Modern artwork."""
        if self.chroot.mode == "mock":
            return
        policy = self.runtime["customization"]["artwork"]
        bg_dir = self.target_root / policy["directory"]
        bg_dir.mkdir(parents=True, exist_ok=True)
        from deb_dev_builder.core.path_utils import resolve_from_project
        artwork_src = resolve_from_project(policy["source"])
        if artwork_src.exists():
            shutil.copy2(artwork_src, bg_dir / policy["filename"])

            default_wp = bg_dir / policy["default_link"]
            if default_wp.exists() or default_wp.is_symlink():
                default_wp.unlink()
            default_wp.symlink_to(Path("/") / policy["directory"] / policy["filename"])

        compat = policy["xfce_default_backdrop"]
        if self.config.get("desktop") == "xfce" and self.config.get("distro") in compat["distributions"]:
            # Ceres carries xfdesktop's Debian fallback path without the
            # matching desktop-base file. Retain the distribution's image.
            destination = rootfs_destination(self.target_root, compat["path"])
            if rootfs_destination(self.target_root, compat["path"], follow_leaf=True).is_file():
                return
            image = rootfs_destination(self.target_root, compat["image"], follow_leaf=True)
            if not image.is_file():
                raise ValueError('The XFCE default backdrop requires an installed distribution wallpaper')
            if destination.is_symlink():
                destination.unlink()
            elif destination.exists():
                raise ValueError('The XFCE default backdrop path must be a file or symlink')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to('/' + compat["image"])

    def fix_home_permissions(self):
        if self.chroot.mode == "mock":
            return
        live_user = live_user_settings(self.config, self.runtime)["name"]

        home_dir = self.target_root / "home" / live_user
        if home_dir.exists():
            try:
                self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:{live_user}", f"/home/{live_user}"], check=False)
                self.chroot.run_in_chroot(["chmod", self.runtime["users"]["home_mode"], f"/home/{live_user}"], check=False)
            except Exception as e:
                logger.warning(f"Could not fix permissions on /home/{live_user}: {e}")

        # Ensure sticky bit on /tmp and /var/tmp
        for tmp_path in self.runtime["users"]["temporary_paths"]:
            try:
                self.chroot.run_in_chroot(["chmod", self.runtime["users"]["temporary_mode"], tmp_path], check=False)
            except Exception:
                pass
