import configparser
import pytest

from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.desktop_sessions import detect_session
from deb_dev_builder.core.runtime_config import RuntimeConfigError, load_runtime


@pytest.mark.parametrize("directory,name", [
    ("xsessions", "plasma"),
    ("xsessions", "plasmax11"),
    ("wayland-sessions", "plasma"),
])
def test_live_plasma_autologin_names_an_existing_session(tmp_path, directory, name):
    session = tmp_path / "usr/share" / directory / (name + ".desktop")
    session.parent.mkdir(parents=True)
    session.write_text("[Desktop Entry]\n")
    customizer = SystemCustomizer(ChrootManager(tmp_path, mode="real"), {
        "desktop": "kde", "display_manager": "sddm", "init_system": "sysvinit",
    })
    customizer.configure_autologin()
    for relative in ("etc/sddm.conf", "etc/sddm.conf.d/autologin.conf"):
        parser = configparser.ConfigParser()
        parser.read(tmp_path / relative)
        assert parser["Autologin"]["Session"] == session.name


def test_session_selection_does_not_switch_to_an_unrelated_desktop(tmp_path):
    path = tmp_path / "usr/share/xsessions/xfce.desktop"
    path.parent.mkdir(parents=True)
    path.touch()
    assert detect_session(tmp_path, {"desktop": "kde"}, load_runtime()) == "plasma"


def test_explicit_session_takes_precedence_over_desktop_defaults(tmp_path):
    config = {"desktop": "kde", "desktop_session": "custom.desktop"}
    assert detect_session(tmp_path, config, load_runtime()) == "custom"


@pytest.mark.parametrize("candidates", [["../plasma"], ["plasma.desktop"], [], [True]])
def test_invalid_session_candidates_are_rejected(candidates):
    with pytest.raises(RuntimeConfigError, match="session.candidates"):
        load_runtime({"runtime": {"desktop": {"session_candidates": {"kde": candidates}}}})
