from pathlib import Path

import pytest

from deb_dev_builder.core.hook_runner import HookError, HookRunner
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.path_utils import safe_remove_tree


def _write_hook(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_hooks_run_in_lexical_order_with_build_environment(tmp_path):
    hooks = tmp_path / "hooks"
    output = tmp_path / "events"
    _write_hook(hooks / "post-packages.d" / "20-second", f'echo "$DEB_DEV_HOOK_PHASE:$DEB_DEV_ARCH" >> "{output}"\n')
    _write_hook(hooks / "post-packages.d" / "10-first", f'echo first >> "{output}"\n')
    runner = HookRunner(str(hooks), {"architecture": "amd64"}, tmp_path / "work", tmp_path / "root", "real")
    (tmp_path / "work").mkdir()
    runner.run("post-packages")
    assert output.read_text().splitlines() == ["first", "post-packages:amd64"]


def test_failed_hook_stops_the_phase(tmp_path):
    hook = tmp_path / "hooks" / "pre-artifact.d" / "10-fail"
    _write_hook(hook, "exit 7\n")
    runner = HookRunner(str(tmp_path / "hooks"), {}, tmp_path / "work", tmp_path / "root", "real")
    (tmp_path / "work").mkdir()
    with pytest.raises(HookError, match="Hook failed"):
        runner.run("pre-artifact")


def test_mock_mode_discovers_but_does_not_execute_hooks(tmp_path):
    hook = tmp_path / "hooks" / "cleanup.d" / "10-write"
    marker = tmp_path / "marker"
    _write_hook(hook, f'touch "{marker}"\n')
    runner = HookRunner(str(tmp_path / "hooks"), {}, tmp_path / "work", tmp_path / "root", "mock")
    runner.run("cleanup")
    assert not marker.exists()


def test_all_lifecycle_phases_are_available(tmp_path):
    runner = HookRunner(str(tmp_path / "hooks"), {}, tmp_path / "work", tmp_path / "root", "mock")
    for phase in runner.PHASES:
        runner.run(phase)


def test_chroot_hooks_are_staged_and_cleaned_up(tmp_path):
    hook = tmp_path / "hooks" / "chroot" / "post-packages.d" / "10-inside"
    _write_hook(hook, "exit 0\n")
    root = tmp_path / "root"
    root.mkdir()

    class FakeChroot:
        def __init__(self):
            self.commands = []

        def run_in_chroot(self, command, **kwargs):
            self.commands.append((command, kwargs))

    fake = FakeChroot()
    runner = HookRunner(str(tmp_path / "hooks"), {"architecture": "amd64"}, tmp_path / "work", root, "real")
    runner.run_chroot("post-packages", fake)
    assert fake.commands[0][0] == ["/run/deb-dev-builder-hooks/post-packages/10-inside"]
    assert not (root / "run" / "deb-dev-builder-hooks").exists()


def test_repository_contains_host_and_chroot_directories_for_every_phase():
    root = resolve_from_project("hooks")
    for phase in HookRunner.PHASES:
        assert (root / f"{phase}.d").is_dir()
        assert (root / "chroot" / f"{phase}.d").is_dir()


def test_safe_remove_tree_is_limited_to_an_explicit_root(tmp_path):
    allowed = tmp_path / "allowed"
    target = allowed / "build"
    target.mkdir(parents=True)
    (target / "temporary").write_text("x")
    safe_remove_tree(target, allowed_root=allowed)
    assert not target.exists()
    with pytest.raises(ValueError, match="outside"):
        safe_remove_tree(tmp_path / "other", allowed_root=allowed)
