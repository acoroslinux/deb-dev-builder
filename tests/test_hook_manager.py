from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from deb_dev_builder.core.hook_manager import HookError, HookManager
from deb_dev_builder.core.path_utils import resolve_from_project, safe_remove_tree


def write_hook(path, body="exit 0\n", executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755 if executable else 0o644)
    return path


def manager(tmp_path, **kwargs):
    chroot = SimpleNamespace(target_root=tmp_path / "root", mode="real", is_mounted=True)
    return HookManager(chroot, {"architecture": "amd64", "desktop": "xfce"},
                       hooks_dir=str(tmp_path / "hooks"), **kwargs)


def test_host_hooks_use_canonical_phases_order_and_context(tmp_path):
    runner = manager(tmp_path)
    output = tmp_path / "events"
    write_hook(runner.hooks_base / "pre-chroot/20-second.sh",
               f'echo "$HOOK_PHASE:$BUILD_ARCH:$BUILD_DESKTOP:$PWD" >> "{output}"\n')
    write_hook(runner.hooks_base / "pre-chroot/10-first.sh", f'echo first >> "{output}"\n')
    write_hook(runner.hooks_base / "post-chroot/10-artifact.sh",
               f'echo "$DEB_DEV_ARTIFACT" >> "{output}"\n')
    runner.run_stage("pre-chroot")
    runner.run_stage("post-chroot", artifact=tmp_path / "result.iso")
    assert output.read_text().splitlines() == [
        "first", f"pre-chroot:amd64:xfce:{resolve_from_project('.')}", str(tmp_path / "result.iso")]


def test_discovery_preserves_sources_and_skips_nonexecutable_hidden_and_symlinks(tmp_path):
    runner = manager(tmp_path)
    stage = runner.hooks_base / "chroot"
    valid = write_hook(stage / "10-valid.sh")
    ignored = write_hook(stage / "20-disabled.sh", executable=False)
    write_hook(stage / "30-other.txt")
    write_hook(stage / ".hidden.sh")
    (stage / "40-linked.sh").symlink_to(valid)
    assert runner.scripts_for("chroot") == [valid]
    assert ignored.stat().st_mode & 0o777 == 0o644
    runner.run_stage("pre-chroot")
    assert not (runner.hooks_base / "pre-chroot").exists()
    with pytest.raises(HookError, match="Unknown hook phase"):
        runner.run_stage("../elsewhere")


@pytest.mark.parametrize("disabled_by", ["mock", "cli", "config"])
def test_mock_and_disabled_hooks_never_execute_or_stage(tmp_path, disabled_by):
    runner = manager(tmp_path, enabled=disabled_by != "cli")
    if disabled_by == "mock":
        runner.mode = "mock"
    if disabled_by == "config":
        runner = HookManager(runner.chroot, {"hooks": {"enabled": False}},
                             hooks_dir=str(runner.hooks_base))
    marker = tmp_path / "marker"
    for stage in runner.STAGES:
        write_hook(runner.hooks_base / stage / "10-write.sh", f'touch "{marker}"\n')
        runner.run_stage(stage)
    assert not marker.exists()
    assert not runner.target_root.exists()


def test_host_failure_stops_later_scripts(tmp_path):
    runner = manager(tmp_path)
    marker = tmp_path / "marker"
    write_hook(runner.hooks_base / "pre-chroot/10-fail.sh", "exit 7\n")
    write_hook(runner.hooks_base / "pre-chroot/20-later.sh", f'touch "{marker}"\n')
    with pytest.raises(HookError, match="pre-chroot"):
        runner.run_stage("pre-chroot")
    assert not marker.exists()


@pytest.mark.parametrize("fail", [False, True])
def test_chroot_staging_passes_context_preserves_existing_files_and_cleans_on_failure(tmp_path, fail):
    runner = manager(tmp_path)
    source = write_hook(runner.hooks_base / "chroot/10-inside.sh")
    tmp = runner.target_root / "tmp"
    tmp.mkdir(parents=True)
    previous = tmp / source.name
    previous.write_text("existing file")
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        staged = runner.target_root / command[1].lstrip("/")
        assert command[0] == "/bin/bash"
        assert staged != previous
        assert staged.read_bytes() == source.read_bytes()
        assert kwargs["check"] is True
        assert kwargs["env"]["HOOK_PHASE"] == "chroot"
        assert kwargs["env"]["BUILD_ARCH"] == "amd64"
        assert kwargs["env"]["TARGET_ROOT"] == str(runner.target_root)
        if fail:
            raise subprocess.CalledProcessError(7, command)

    runner.chroot.run_in_chroot = execute
    if fail:
        with pytest.raises(HookError, match="chroot"):
            runner.run_stage("chroot")
    else:
        runner.run_stage("chroot")
    assert len(commands) == 1
    assert list(tmp.iterdir()) == [previous]
    assert previous.read_text() == "existing file"
    assert source.stat().st_mode & 0o777 == 0o755


def test_chroot_requires_mounts_and_does_not_stage_through_host_symlink(tmp_path):
    runner = manager(tmp_path)
    write_hook(runner.hooks_base / "chroot/10-inside.sh")
    runner.target_root.mkdir()
    runner.chroot.is_mounted = False
    with pytest.raises(HookError, match="mounted"):
        runner.run_stage("chroot")
    runner.chroot.is_mounted = True
    (runner.target_root / "tmp").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(HookError, match="symlink"):
        runner.run_stage("chroot")
    assert not list(tmp_path.glob("deb-dev-builder-hook-*"))


def test_repository_uses_three_existing_config_hook_directories(tmp_path):
    runner = HookManager(SimpleNamespace(target_root=tmp_path, mode="mock"))
    assert runner.hooks_base == resolve_from_project("configs/hooks")
    assert {p.name for p in runner.hooks_base.iterdir() if p.is_dir()} == set(runner.STAGES)
    assert [p.name for p in runner.scripts_for("chroot")] == [
        "10-cleanup-rootfs.sh", "20-rebuild-desktop-caches.sh", "30-sanitize-live-system.sh"]


def test_configured_hook_root_and_cli_override(tmp_path):
    chroot = SimpleNamespace(target_root=tmp_path, mode="mock")
    configured = {"hooks": {"directory": "configs/hooks"}}
    assert HookManager(chroot, configured).hooks_base == resolve_from_project("configs/hooks")
    assert HookManager(chroot, configured, hooks_dir=str(tmp_path)).hooks_base == tmp_path


def test_safe_remove_tree_is_limited_to_an_explicit_root(tmp_path):
    allowed = tmp_path / "allowed"
    target = allowed / "build"
    target.mkdir(parents=True)
    (target / "temporary").write_text("x")
    safe_remove_tree(target, allowed_root=allowed)
    assert not target.exists()
    with pytest.raises(ValueError, match="outside"):
        safe_remove_tree(tmp_path / "other", allowed_root=allowed)
