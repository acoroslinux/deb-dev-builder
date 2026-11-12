"""Standalone 32-bit media use native kernels and IA32 boot components."""
import pytest

from deb_dev_builder.core.orchestrator import BuildOrchestrator
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.runtime_config import load_runtime, RuntimeConfigError


@pytest.mark.parametrize('architecture', ['i386', 'i686'])
@pytest.mark.parametrize('distro,suite', [('debian-12', 'bookworm'), ('devuan-5', 'daedalus')])
def test_native_32_bit_profiles_and_installers(architecture, distro, suite, tmp_path):
    build = BuildOrchestrator(arch=architecture, distro=distro, variant='live', desktop='xfce',
                              with_calamares=True, with_debian_installer=True)
    config = build.config
    assert build.validate()['valid']
    assert build.arch == config['architecture'] == config['dpkg_arch'] == 'i386'
    packages = set(config['software'])
    assert {'linux-image-686-pae', 'linux-headers-686-pae', 'grub-pc-bin', 'grub-efi-ia32-bin'} <= packages
    assert not any(package.startswith(('linux-image-amd64', 'grub-efi-amd64', 'shim-signed')) for package in packages)
    engine = ISOEngine(tmp_path, tmp_path / 'root', 'test', config, 'mock', None)
    assert engine.arch_policy['efi_target'] == 'i386-efi'
    assert engine.arch_policy['efi_filename'] == 'BOOTIA32.EFI'
    assert engine.arch_policy['bios']
    assert f'/dists/{suite}/main/installer-i386/current/images/cdrom/initrd.gz' in engine._installer_image_url('initrd.gz')
    groups = build.runtime['installer']['offline_package_sets']['i386']
    assert {'grub-efi-ia32', 'efibootmgr', 'os-prober'} <= set(groups[0])
    assert {'grub-pc', 'os-prober'} <= set(groups[1])


@pytest.mark.parametrize('distro', ['debian-13', 'debian-14', 'debian-testing', 'debian-sid',
                                   'devuan-6', 'devuan-testing', 'devuan-ceres'])
@pytest.mark.parametrize('installer', ['calamares', 'di', 'none'])
def test_newer_suites_reject_standalone_32_bit_bootable_media(distro, installer):
    build = BuildOrchestrator(arch='i686', distro=distro, variant='live', desktop='xfce',
                              with_calamares=installer == 'calamares', with_debian_installer=installer == 'di')
    result = build.validate()
    assert not result['valid']
    assert any('native 32-bit kernel/installer' in error for error in result['errors'])


def test_support_policy_is_overridable_for_custom_native_sources():
    build = BuildOrchestrator(arch='i686', distro='debian-13', desktop='xfce')
    build.runtime['architectures']['i386']['bootable_suites']['debian'] = ['trixie']
    assert build.validate()['valid']


def test_invalid_architecture_suite_policy_is_rejected():
    with pytest.raises(RuntimeConfigError, match='bootable_suites'):
        load_runtime({'runtime': {'architectures': {'i386': {'bootable_suites': {'debian': ['../sid']}}}}})


@pytest.mark.parametrize('distro', ['debian-12', 'devuan-5'])
def test_32_bit_live_iso_mock_pipeline(tmp_path, distro):
    build = BuildOrchestrator(arch='i686', distro=distro, variant='live', desktop='xfce',
                              with_calamares=True, with_debian_installer=True, mode='mock', hooks_enabled=False)
    build.workdir = tmp_path / 'work'
    build.target_root = build.workdir / 'chroot'
    artifact = build.build(output_name=str(tmp_path / 'test-i386'))
    assert artifact.is_file()
    staging = build.workdir / 'iso_root'
    assert (staging / 'live/filesystem.squashfs').is_file()
    assert (staging / 'install/initrd.gz').is_file()
    assert (staging / 'boot/grub/grub_eltorito').is_file()
    menu = (staging / 'boot/grub/grub.cfg').read_text()
    assert 'Install live system' in menu
    assert 'linux /live/vmlinuz' in menu


@pytest.mark.parametrize('distro', ['debian-12', 'devuan-5'])
@pytest.mark.parametrize('flavor,valid', [('generic', True), ('rt', True), ('kernel-rt', True), ('cloud', False)])
def test_i386_kernel_flavors_match_available_native_metapackages(distro, flavor, valid):
    build = BuildOrchestrator(arch='i686', distro=distro, kernel=flavor)
    result = build.validate()
    assert result['valid'] is valid
    if not valid:
        assert any("Kernel flavor 'cloud' is unavailable for i386" in error for error in result['errors'])
