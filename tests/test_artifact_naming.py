from pathlib import Path

import pytest

from deb_dev_builder.core.orchestrator import BuildOrchestrator, BuildOrchestratorError


def orchestrator(**kwargs):
    return BuildOrchestrator(arch="amd64", distro="debian-13", mode="mock", **kwargs)


def test_live_name_identifies_selected_build_and_custom_output_directory(tmp_path):
    build = orchestrator(desktop="xfce", variant="live", with_calamares=True)
    name = Path(build._artifact_output_name(str(tmp_path / "my-build.iso")))
    assert name.parent == tmp_path
    assert name.name.startswith("my-build-debian-13-live-iso-amd64-calamares-xfce-live-systemd-generic-grub2-hybrid-ext4-mock-")
    assert name.suffix == ""
    assert name != Path(build._artifact_output_name(str(tmp_path / "my-build.iso")))


@pytest.mark.parametrize("output_format", ["iso", "netboot", "img", "raw", "qcow2", "vmdk", "vdi", "vhd", "vhdx", "tarball", "container", "oci"])
def test_every_format_has_unique_descriptive_names(output_format):
    build = orchestrator(output_format=output_format)
    first = Path(build._artifact_output_name())
    second = Path(build._artifact_output_name())
    image_type = build.runtime["build"]["artifact_naming"]["image_types"][output_format].format(di_mode="live")
    assert f"-debian-13-{image_type}-amd64-" in first.name
    assert "-no-desktop-" in first.name
    assert first != second


@pytest.mark.parametrize("di_mode", ["netinstall", "netboot"])
def test_debian_installer_identified_without_explicit_flag(di_mode):
    build = orchestrator(di_mode=di_mode)
    assert f"-{di_mode}-iso-amd64-debian-installer-" in build._artifact_output_name()


def test_both_installers_are_identified():
    build = orchestrator(with_calamares=True, with_debian_installer=True)
    assert "-calamares-debian-installer-" in build._artifact_output_name()


def test_template_is_configurable_and_rejects_paths():
    build = orchestrator()
    policy = build.runtime["build"]["artifact_naming"]
    policy["template"] = "{prefix}-{architecture}-{build_id}"
    assert Path(build._artifact_output_name()).name.startswith("deb-dev-amd64-")
    policy["template"] = "../{prefix}"
    with pytest.raises(BuildOrchestratorError, match="safe filename"):
        build._artifact_output_name()
