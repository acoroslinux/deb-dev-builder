"""Live-only icon creation and target-only Calamares cleanup."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.path_utils import resolve_from_project


@pytest.fixture
def installer(tmp_path):
    root = tmp_path / "live-root"
    (root / "home/session-account").mkdir(parents=True)
    (root / "home/other-account").mkdir()
    legacy = root / "etc/skel/Desktop/install-deb-dev.desktop"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old installer shortcut")
    (legacy.parent / "keep.txt").write_text("keep")
    executable = tmp_path / "calamares"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("boot=live components\n")
    config = {"with_calamares": True, "live_user": {"name": "session-account"}, "runtime": {
        "customization": {"calamares": {"executable": str(executable), "cmdline_file": str(cmdline)}},
    }}
    customizer = SystemCustomizer(ChrootManager(root, mode="real"), config)
    customizer.configure_calamares()
    policy = customizer.runtime["customization"]["calamares"]
    return root, policy, cmdline, executable


def run_helper(installer, tmp_path):
    root, policy, _, _ = installer
    home = tmp_path / "session-home"
    home.mkdir(exist_ok=True)
    desktop = home / "Desktop \u2603"
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    for name, body in {"gio": "exit 0", "xdg-user-dir": 'printf "%s\\n" "$TEST_DESKTOP"'}.items():
        path = binaries / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "TEST_DESKTOP": str(desktop),
           "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
    subprocess.run(["/bin/sh", str(root / policy["helper_script"])], env=env, check=True, capture_output=True)
    return home, desktop


def test_installer_is_not_seeded_into_skel_or_other_accounts(installer):
    root, policy, _, _ = installer
    name = policy["launcher_filename"]
    assert not (root / "etc/skel/Desktop" / name).exists()
    assert (root / "etc/skel/Desktop/keep.txt").read_text() == "keep"
    assert (root / "home/session-account/Desktop" / name).is_file()
    assert not (root / "home/other-account/Desktop" / name).exists()
    assert "TryExec=" in (root / policy["application_launcher"]).read_text()
    assert "TryExec=" in (root / policy["autostart_launcher"]).read_text()


def test_target_hooks_pass_both_calamares_root_placeholder_generations(installer):
    root, policy, _, _ = installer
    for name in ("prepare_config", "keyfile_config", "validate_config"):
        config = json.loads((root / policy[name]).read_text())
        command = config["script"][0]
        assert '"${ROOT}"' in command
        assert '"@@ROOT@@"' in command


@pytest.mark.parametrize("cmdline", ["root=UUID=123 ro", "boot=local", "boot=live-other", "notboot=live"])
def test_helper_never_creates_installer_on_nonlive_system(installer, tmp_path, cmdline):
    installer[2].write_text(cmdline + "\n")
    home, desktop = run_helper(installer, tmp_path)
    assert not desktop.exists()
    assert list(home.iterdir()) == []


def test_helper_requires_installer_binary(installer, tmp_path):
    installer[3].unlink()
    _, desktop = run_helper(installer, tmp_path)
    assert not desktop.exists()


def test_live_helper_uses_current_users_localized_desktop(installer, tmp_path):
    _, policy, _, _ = installer
    home, desktop = run_helper(installer, tmp_path)
    icon = desktop / policy["launcher_filename"]
    assert icon.is_file()
    assert icon.stat().st_mode & 0o111
    assert "Exec=pkexec " + str(installer[3]) in icon.read_text()
    assert not (home / "Desktop").exists()


def cleanup_commands(installer):
    root, policy, _, _ = installer
    config = json.loads((root / policy["cleanup_config"]).read_text())
    assert config["dontChroot"] is True
    return config["script"]


def expand_cleanup_command(command, target, calamares_generation):
    if calamares_generation == "legacy":
        return command.replace("${ROOT}", "").replace("@@ROOT@@", str(target))
    return command.replace("${ROOT}", str(target))


def test_installed_target_drops_live_offline_repository_but_live_keeps_it(installer, tmp_path):
    root, _, _, _ = installer
    relative = Path("etc/apt/sources.list.d/offline-iso.list")
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).write_text("deb [trusted=yes] file:/run/live/medium/repo/ ./\n")
    target = tmp_path / "installed"
    shutil.copytree(root, target)
    for command in cleanup_commands(installer):
        subprocess.run(["sh", "-ec", command.replace("${ROOT}", str(target))], check=True)
    assert not (target / relative).exists()
    assert (root / relative).exists()


def test_cleanup_removes_all_target_artifacts_and_does_not_touch_live_or_external_home(installer, tmp_path):
    root, policy, _, _ = installer
    target = tmp_path / "installed system"
    shutil.copytree(root, target)
    name = policy["launcher_filename"]
    localized = target / "home/real-user/Desktop \u2603" / name
    localized.parent.mkdir(parents=True)
    localized.write_text("old installer")
    old_skel = target / "etc/skel/Desktop" / name
    old_skel.write_text("old installer")
    keep = localized.with_name("keep.desktop")
    keep.write_text("user launcher")
    external = tmp_path / "external-home"
    external.mkdir()
    (external / name).write_text("external user file")
    (target / "home/linked-home").symlink_to(external, target_is_directory=True)
    commands = cleanup_commands(installer)
    for _ in range(2):  # Cleanup remains safe if the job is retried.
        for command in commands:
            subprocess.run(["/bin/sh", "-c", command.replace("${ROOT}", str(target))], check=True, capture_output=True)
    for key in ("application_launcher", "autostart_launcher", "helper_script", "polkit_rule"):
        assert not (target / policy[key]).exists()
        assert (root / policy[key]).is_file()
    assert not localized.exists()
    assert not old_skel.exists()
    assert not list((target / "home/session-account").rglob(name))
    assert keep.read_text() == "user launcher"
    assert (external / name).read_text() == "external user file"
    assert (root / "home/session-account/Desktop" / name).is_file()


def test_cleanup_works_with_legacy_calamares_root_placeholder(installer, tmp_path):
    root, policy, _, _ = installer
    target = tmp_path / "installed"
    shutil.copytree(root, target)
    app = target / policy["application_launcher"]
    assert app.is_file()

    for command in cleanup_commands(installer):
        subprocess.run(
            ["/bin/sh", "-c", expand_cleanup_command(command, target, "legacy")],
            check=True, capture_output=True, text=True,
            env={key: value for key, value in os.environ.items() if key != "ROOT"},
        )

    assert not app.exists()
    assert (root / policy["application_launcher"]).is_file()


@pytest.mark.parametrize("invalid_root", ["", "/"])
def test_cleanup_refuses_missing_target_or_live_root(installer, invalid_root):
    guard = cleanup_commands(installer)[0].replace("${ROOT}", invalid_root)
    result = subprocess.run(["/bin/sh", "-c", guard], capture_output=True)
    assert result.returncode != 0


def test_cleanup_tolerates_absent_home_and_skel(installer, tmp_path):
    target = tmp_path / "empty-installed"
    (target / "etc").mkdir(parents=True)
    for command in cleanup_commands(installer):
        subprocess.run(["/bin/sh", "-c", command.replace("${ROOT}", str(target))], check=True, capture_output=True)


@pytest.mark.parametrize("variant", ["systemd", "non-systemd"])
def test_cleanup_is_scheduled_after_target_users_and_packages_before_unmount(variant):
    settings = resolve_from_project(f"configs/custom_files/calamares/{variant}/settings.conf").read_text()
    lines = settings.splitlines()
    cleanup = lines.index("    - shellprocess@cleanup-live-installer")
    assert lines.index("    - users") < cleanup
    assert lines.index("    - packages") < cleanup < lines.index("    - umount")
    assert "    module: shellprocess" in settings
    assert "    config: cleanup-live-installer.conf" in settings
