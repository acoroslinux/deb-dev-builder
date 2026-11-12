import configparser
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from deb_dev_builder.core.installer_autologin import autologin_entries
from deb_dev_builder.core.installer_media import LiveInstallerMedia, preseed_archive
from deb_dev_builder.core.initrd_editor import unpack_initrd
from deb_dev_builder.core.runtime_config import load_runtime


@pytest.mark.parametrize("manager,desktop,session_override", [
    ("lightdm", "xfce", None), ("sddm", "kde", None),
    ("sddm", "kde", "plasmax11"), ("gdm3", "gnome", None),
])
@pytest.mark.parametrize("answer", ["true", "false"])
def test_d_i_autologin_uses_the_installed_account_and_preserves_other_settings(tmp_path, manager, desktop, session_override, answer):
    target = tmp_path / "installed system"
    (target / "etc").mkdir(parents=True)
    config = {"desktop": desktop, "display_manager": manager,
              "runtime": {"installer": {"target_root": str(target)}}}
    runtime = load_runtime(config)
    policy = runtime["installer"]["autologin"]
    binary = target / runtime["desktop"]["display_managers"][manager]["binaries"][0]
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("manager")
    binary.chmod(0o755)
    session = session_override or runtime["desktop"]["session_aliases"].get(desktop, desktop)
    session_file = target / runtime["desktop"]["session_directories"][0] / (session + ".desktop")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("[Desktop Entry]\n")
    result_path = target / policy["managers"][manager]["path"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    original = "[daemon]\nWaylandEnable=false\n\n[security]\nDisallowTCP=true\n"
    result_path.write_text(original)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, body in {
        "debconf-loadtemplate": 'printf "%s\\n" "$*" >> "$EVENTS"',
        "chroot": 'test "$2" = getent && test "$3" = passwd\nprintf "%s:x:1000:1000:Installed:/home/%s:/bin/bash\\n" "$4" "$4"',
    }.items():
        executable = tools / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o755)
    module = tmp_path / "confmodule"
    module.write_text('''db_get() {
    case "$1" in
        passwd/make-user) RET=true ;;
        passwd/username) RET=installed-user ;;
        deb-dev/autologin) RET="$ANSWER" ;;
        *) return 10 ;;
    esac
}
db_input() { printf '%s\n' "$*" >> "$EVENTS"; return 30; }
db_go() { return 0; }
''')
    entries = autologin_entries(config, runtime)
    script = next(entry.data.decode() for entry in entries if entry.name == policy["finish_hook"])
    script = script.replace("/usr/share/debconf/confmodule", shlex.quote(str(module)))
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "EVENTS": str(tmp_path / "events"), "ANSWER": answer}
    subprocess.run(["sh", "-n"], input=script.encode(), check=True, capture_output=True)
    for _ in range(2):
        subprocess.run(["sh"], input=script.encode(), check=True, capture_output=True, env=env)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(result_path)
    assert parser["daemon"]["WaylandEnable"] == "false"
    assert parser["security"]["DisallowTCP"] == "true"
    if answer == "true":
        section = {"lightdm": "Seat:*", "sddm": "Autologin", "gdm3": "daemon"}[manager]
        key = {"lightdm": "autologin-user", "sddm": "User", "gdm3": "AutomaticLogin"}[manager]
        assert parser[section][key] == "installed-user"
        if manager == "sddm":
            assert parser[section]["Session"] == session_file.name
        assert parser["daemon"]["WaylandEnable"] == "false"
        assert parser["security"]["DisallowTCP"] == "true"
        assert "liveuser" not in result_path.read_text()
        # Changing the answer on a finish-install retry must undo our previous
        # choice while retaining unrelated target settings.
        subprocess.run(["sh"], input=script.encode(), check=True, capture_output=True, env={**env, "ANSWER": "false"})
        parser.read(result_path)
    for section, values in policy["managers"][manager]["disable_sections"].items():
        for key, value in values.items():
            assert parser[section][key] == value
    assert not list(target.glob("etc/sudoers.d/*"))


def test_templates_are_registered_before_initrd_preseed_and_question_is_optional():
    runtime = load_runtime()
    assert "S25" in Path(runtime["installer"]["autologin"]["startup_hook"]).name
    entries = autologin_entries({"desktop": "xfce", "display_manager": "lightdm"}, runtime)
    template = entries[0].data.decode()
    assert "Default: false" in template and "Description: Enable automatic desktop login?" in template
    assert autologin_entries({}, runtime) == []
    runtime["installer"]["autologin"]["enabled"] = False
    assert autologin_entries({"desktop": "xfce", "display_manager": "lightdm"}, runtime) == []


def test_autologin_is_embedded_in_both_live_installer_frontends(tmp_path):
    config = {"suite": "trixie", "architecture": "amd64", "desktop": "xfce", "display_manager": "lightdm"}
    runtime = load_runtime(config)
    for path in (tmp_path / "initrd.gz", tmp_path / "gtk/initrd.gz"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(preseed_archive(""))
    LiveInstallerMedia(config, runtime["installer"]).embed_preseed(tmp_path, "d-i deb-dev/autologin boolean true\n")
    for path in (tmp_path / "initrd.gz", tmp_path / "gtk/initrd.gz"):
        entries = unpack_initrd(path.read_bytes())
        for key in ("template_path", "startup_hook", "finish_hook"):
            assert runtime["installer"]["autologin"][key] in entries
        assert b"d-i deb-dev/autologin boolean true" in entries["preseed.cfg"].data
