import json
import shutil

import pytest

import cli
from deb_dev_builder.core.config_loader import ConfigLoader
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.orchestrator import BuildOrchestrator, BuildOrchestratorError
from deb_dev_builder.core.path_utils import resolve_from_project


@pytest.fixture
def live_catalog(tmp_path):
    for name in ("base_customizations.json", "global_build.json", "system/debian-13.json",
                 "system/systemd.json", "architectures/amd64.json", "software/base.json",
                 "live-users/liveuser.json", "live-users/guest.json"):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolve_from_project("configs") / name, destination)
    return tmp_path


def test_password_and_groups_come_from_selected_live_user_profile(live_catalog):
    tmp_path = live_catalog
    profile = tmp_path / "live-users/guest.json"
    profile.write_text(json.dumps({"live_user": {
        "name": "guest", "password": "profile-test-credential", "groups": ["audio", "video"],
    }}))
    config = ConfigLoader(tmp_path).assemble_build_config(
        global_config_path=tmp_path / "global_build.json", architecture="amd64",
        distro="debian-13", init_system="systemd", live_profile="guest",
    )
    assert config["live_user"] == {
        "name": "guest", "password": "profile-test-credential", "groups": ["audio", "video"],
    }


def test_default_liveuser_profile_provides_account_and_password(live_catalog):
    profile = live_catalog / "live-users/liveuser.json"
    data = json.loads(profile.read_text())
    data["live_user"]["password"] = "default-profile-test-credential"
    profile.write_text(json.dumps(data))
    loader = ConfigLoader(live_catalog)
    config = loader.assemble_build_config(live_catalog / "global_build.json", architecture="amd64", distro="debian-13")
    assert config["live_user"] == data["live_user"]


def test_guest_does_not_inherit_default_liveuser_credentials(live_catalog):
    profile = live_catalog / "live-users/liveuser.json"
    data = json.loads(profile.read_text())
    data["live_user"]["password"] = "default-profile-test-credential"
    profile.write_text(json.dumps(data))
    guest = live_catalog / "live-users/guest.json"
    guest_data = json.loads(guest.read_text())
    guest_data["live_user"].pop("password", None)
    guest.write_text(json.dumps(guest_data))
    config = ConfigLoader(live_catalog).assemble_build_config(
        live_catalog / "global_build.json", architecture="amd64", distro="debian-13", live_profile="guest",
    )
    assert config["live_user"] == {"name": "guest", "groups": ["audio", "video"]}


def test_global_configuration_can_override_default_liveuser(live_catalog):
    global_path = live_catalog / "global_build.json"
    config = json.loads(global_path.read_text())
    config["live_user"] = {"password": "global-test-credential"}
    global_path.write_text(json.dumps(config))
    assembled = ConfigLoader(live_catalog).assemble_build_config(global_path, architecture="amd64", distro="debian-13")
    assert assembled["live_user"]["name"] == "liveuser"
    assert assembled["live_user"]["password"] == "global-test-credential"


def test_missing_profile_password_is_reported_without_interactive_input(monkeypatch, capsys):
    original_builder = cli.BuildOrchestrator
    def without_password(**kwargs):
        builder = original_builder(**kwargs)
        builder.config["live_user"].pop("password", None)
        return builder
    monkeypatch.setattr(cli, "BuildOrchestrator", without_password)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: pytest.fail("Unexpected interactive input"))
    monkeypatch.setattr("sys.argv", ["cli.py", "amd64", "--distro", "debian-13", "--desktop", "xfce",
                                    "--live-profile", "guest", "--mode", "real"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "configs/live-users/guest.json" in output.err
    assert "passwords are read from configuration, never prompted" in output.err


def test_desktop_api_refuses_locked_account_before_changing_workdirs():
    builder = BuildOrchestrator(distro="debian-13", desktop="xfce", mode="real")
    builder.config["live_user"].pop("password", None)
    with pytest.raises(BuildOrchestratorError, match="screen unlocking"):
        builder.build()


@pytest.mark.parametrize("value", ["", "bad\nvalue", "bad\0value"])
def test_invalid_profile_passwords_fail_validation(value):
    builder = BuildOrchestrator(distro="debian-13", desktop="xfce", mode="mock")
    builder.config["live_user"]["password"] = value
    assert not builder.validate()["valid"]


def test_root_cannot_be_selected_as_the_live_account(tmp_path):
    builder = BuildOrchestrator(distro="debian-13", live_user_name="root")
    assert not builder.validate()["valid"]
    class Chroot:
        mode = "real"
        target_root = tmp_path
    with pytest.raises(ValueError, match="Invalid live user name"):
        SystemCustomizer(Chroot(), {"live_user": {"name": "root", "password": "example"}}).setup_live_users()


def test_packaged_autologin_pam_and_shadow_permissions_are_preserved(tmp_path):
    class Chroot:
        mode = "real"
        target_root = tmp_path
    directory = tmp_path / "etc/pam.d"
    directory.mkdir(parents=True)
    files = {"lightdm": "@include common-auth\n", "lightdm-autologin": "auth required pam_nologin.so\n@include common-session\n"}
    for name, content in files.items():
        path = directory / name
        path.write_text(content)
        path.chmod(0o644)
    shadow = tmp_path / "etc/shadow"
    shadow.write_text("existing shadow data\n")
    shadow.chmod(0o640)
    SystemCustomizer(Chroot(), {"display_manager": "lightdm"}).configure_autologin()
    for name, content in files.items():
        assert (directory / name).read_text() == content
        assert (directory / name).stat().st_mode & 0o777 == 0o644
    assert shadow.read_text() == "existing shadow data\n"
    assert shadow.stat().st_mode & 0o777 == 0o640
    assert not (directory / "sddm-autologin").exists()
