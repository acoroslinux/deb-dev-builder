import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile

import pytest

from deb_dev_builder.core.installer_locale import locale_entries
from deb_dev_builder.core.installer_media import LiveInstallerMedia, preseed_archive
from deb_dev_builder.core.initrd_editor import unpack_initrd
from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.runtime_config import load_runtime


@pytest.mark.parametrize("selected,language", [("pt_PT.UTF-8", "pt:en"), ("pt_BR.UTF-8", "pt_BR:pt:en"), ("en_US.UTF-8", "en"), ("pt_PT.UTF-8", ""), ("C", "en"), ("C.UTF-8", "en"), ("not_a_locale", "en")])
def test_d_i_choice_replaces_copied_live_locale_in_both_files(tmp_path, selected, language):
    packages = sorted(resolve_from_project("cache/debian").glob("*/amd64/apt/archives/locales_*.deb"))
    if not packages:
        pytest.skip("Cached locales package required to exercise native update-locale")
    payload = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(packages[-1])], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        native = next(item for item in archive if item.name.endswith("/sbin/update-locale"))
        updater = tmp_path / "update-locale"
        updater.write_bytes(archive.extractfile(native).read())
    updater.chmod(0o755)
    target = tmp_path / "installed root"
    (target / "etc/default").mkdir(parents=True)
    (target / "usr/share/i18n").mkdir(parents=True)
    (target / "usr/share/i18n/SUPPORTED").write_text("en_US.UTF-8 UTF-8\npt_PT.UTF-8 UTF-8\npt_BR.UTF-8 UTF-8\n")
    (target / "etc/locale.gen").write_text("en_US.UTF-8 UTF-8\n# pt_PT.UTF-8 UTF-8\n")
    for path in ("etc/locale.conf", "etc/default/locale"):
        (target / path).write_text("LANG=en_US.UTF-8\nLANGUAGE=en\nLC_TIME=en_US.UTF-8\nLC_ALL=en_US.UTF-8\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    wrapper = tools / "chroot"
    wrapper.write_text('''#!/bin/sh
target=$1; shift
printf '%s\\n' "$*" >> "$EVENTS"
case "$1" in
    locale-gen) exit 0 ;;
    update-locale)
        shift
        test "$1" = --reset; shift
        test "$1" = --locale-file; shift
        file=$1; shift
        exec "$NATIVE_UPDATE" --reset --no-checks --locale-file "$target$file" "$@" ;;
    *) exit 99 ;;
esac
''')
    wrapper.chmod(0o755)
    module = tmp_path / "confmodule"
    module.write_text('''db_get() {
case "$1" in
debian-installer/locale) RET="$SELECTED" ;;
debian-installer/language) RET="$LANGUAGE_CHOICE" ;;
*) return 10 ;;
esac
}
''')
    config = {"runtime": {"installer": {"target_root": str(target)}}}
    runtime = load_runtime(config)
    entry, = locale_entries(config, runtime)
    script = entry.data.decode().replace("/usr/share/debconf/confmodule", shlex.quote(str(module)))
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "NATIVE_UPDATE": str(updater), "SELECTED": selected, "LANGUAGE_CHOICE": language,
           "EVENTS": str(tmp_path / "events")}
    subprocess.run(["sh", "-n"], input=script, text=True, check=True)
    for _ in range(2):
        result = subprocess.run(["sh", "-e"], input=script, env=env, text=True, capture_output=True)
        if selected == "not_a_locale":
            assert result.returncode == 10
            assert not (tmp_path / "events").exists()
            return
        assert result.returncode == 0, result.stderr
    expected_language = "" if selected.startswith("C") else language or "pt_PT"
    values = []
    for path in ("etc/locale.conf", "etc/default/locale"):
        value = subprocess.run(["sh", "-ec", '. "$1"; printf "%s|%s|%s|%s" "$LANG" "$LANGUAGE" "${LC_ALL-}" "${LC_TIME-}"', "check", str(target / path)], capture_output=True, text=True, check=True,
                               env={key: value for key, value in os.environ.items() if not key.startswith("LC_") and key != "LANGUAGE"})
        values.append(value.stdout)
    assert values == [f"{selected}|{expected_language}||"] * 2
    generated = (target / "etc/locale.gen").read_text().splitlines()
    if selected not in runtime["installer"]["locale"]["builtin_locales"]:
        assert generated.count(selected + " UTF-8") == 1
    assert (target / runtime["installer"]["locale"]["log_path"]).read_text() == f"LANG={selected}\nLANGUAGE={expected_language}\n"


def test_locale_hook_is_embedded_in_both_live_installer_frontends(tmp_path):
    config = {"suite": "trixie", "architecture": "amd64"}
    runtime = load_runtime(config)
    for path in (tmp_path / "initrd.gz", tmp_path / "gtk/initrd.gz"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(preseed_archive(""))
    LiveInstallerMedia(config, runtime["installer"]).embed_preseed(tmp_path)
    for path in (tmp_path / "initrd.gz", tmp_path / "gtk/initrd.gz"):
        assert runtime["installer"]["locale"]["finish_hook"] in unpack_initrd(path.read_bytes())
    runtime["installer"]["locale"]["enabled"] = False
    assert locale_entries(config, runtime) == []
