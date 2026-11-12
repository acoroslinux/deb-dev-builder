import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from deb_dev_builder.core.installer_conffiles import ucf_preservation_hooks
from deb_dev_builder.core.runtime_config import load_runtime


def run(script, **kwargs):
    return subprocess.run(["sh", "-eu"], input=script, text=True, check=True, capture_output=True, **kwargs)


@pytest.mark.parametrize("existing", [False, True])
def test_ucf_policy_is_scoped_idempotent_and_restored_exactly(tmp_path, existing):
    policy = load_runtime()["installer"]["ucf_preservation"]
    target = tmp_path / "target with spaces"
    config = target / policy["config_file"]
    config.parent.mkdir(parents=True)
    original = "# Existing local policy\nconf_force_conffnew=YES\n"
    if existing:
        config.write_text(original)
        config.chmod(0o640)
    prepare, finish = ucf_preservation_hooks(str(target), policy)
    for _ in range(2):
        run(prepare)
        assert config.read_text().count("# Temporary live-installer policy") == 1
        for destination in ("/etc/default/grub", "/etc/unrelated"):
            script = "\n".join([
                "dest_file=" + shlex.quote(destination),
                "UCF_FORCE_CONFFNEW=1", "DPKG_FORCE=confdef",
                ". " + shlex.quote(str(config)),
                "printf '%s|%s|%s|%s' \"${conf_force_conffold-}\" \"${conf_force_conffnew-}\" \"${UCF_FORCE_CONFFNEW-}\" \"$DPKG_FORCE\"",
            ])
            output = run(script).stdout
            if destination == "/etc/default/grub":
                assert output == "YES|||confold"
            else:
                assert output == ("|YES|1|confdef" if existing else "||1|confdef")
    for _ in range(2):
        run(finish)
    assert not (target / policy["backup_directory"]).exists()
    assert (target / policy["restored_marker"]).exists()
    if existing:
        assert config.read_text() == original
        assert config.stat().st_mode & 0o777 == 0o640
    else:
        assert not config.exists()


def test_native_ucf_retains_modified_grub_without_prompting_and_updates_other_files(tmp_path):
    native = Path("/usr/bin/ucf")
    library = Path("/usr/share/ucf/ucf_library.sh")
    if not native.is_file() or not library.is_file():
        pytest.skip("Native ucf package required")
    target = tmp_path / "target"
    config = target / "etc/ucf.conf"
    config.parent.mkdir(parents=True)
    config.write_text("conf_force_conffnew=YES\n")
    protected = target / "etc/default/grub"
    protected.parent.mkdir()
    protected.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_ENABLE_CRYPTODISK=y\n')
    original = protected.read_bytes()
    new_file = tmp_path / "maintainer-grub"
    new_file.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n')
    policy = {**load_runtime()["installer"]["ucf_preservation"], "files": [str(protected)]}
    prepare, finish = ucf_preservation_hooks(str(target), policy)
    run(prepare)
    tools = tmp_path / "bin"
    tools.mkdir()
    # Keep the native file-selection/state logic. Redirect only configuration
    # and debconf transport into the fixture; never touch host ucf/debconf state.
    confmodule = tools / "confmodule"
    confmodule.write_text("db_x_loadtemplatefile() { :; }; db_settitle() { :; }; db_title() { :; }; db_stop() { :; }; db_input() { echo 'Unexpected configuration prompt' >&2; exit 99; }; db_get() { return 1; };\n")
    script = tools / "ucf"
    script.write_text(native.read_text().replace("/etc/ucf.conf", str(config)).replace("/usr/share/debconf/confmodule", str(confmodule)))
    script.chmod(0o755)
    shutil.copy2(library, tools / library.name)
    (tools / "id").write_text('#!/bin/sh\nif [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
    (tools / "id").chmod(0o755)
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"], "DEBIAN_FRONTEND": "noninteractive", "DPKG_FORCE": "confdef"}
    state = tmp_path / "ucf-state"
    state.mkdir()
    subprocess.run([str(script), "--three-way", "--debconf-ok", "--state-dir", str(state), str(new_file), str(protected)], env=env, capture_output=True, text=True, timeout=10, check=True)
    assert protected.read_bytes() == original
    assert Path(str(protected) + ".ucf-dist").read_bytes() == new_file.read_bytes()
    other = target / "etc/unrelated"
    other.write_text("local unrelated settings\n")
    subprocess.run([str(script), "--debconf-ok", "--state-dir", str(state), str(new_file), str(other)], env={**env, "DPKG_FORCE": ""}, capture_output=True, text=True, timeout=10, check=True)
    assert other.read_bytes() == new_file.read_bytes()
    run(finish)
    assert config.read_text() == "conf_force_conffnew=YES\n"


def test_ucf_policy_rejects_symlink_configuration_before_modifying_it(tmp_path):
    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    external = tmp_path / "outside"
    external.write_text("unchanged\n")
    (target / "etc/ucf.conf").symlink_to(external)
    prepare, _ = ucf_preservation_hooks(str(target), load_runtime()["installer"]["ucf_preservation"])
    with pytest.raises(subprocess.CalledProcessError):
        run(prepare)
    assert external.read_text() == "unchanged\n"
