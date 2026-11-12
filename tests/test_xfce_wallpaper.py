from pathlib import Path

import pytest

from deb_dev_builder.core.chroot_manager import ChrootManager
from deb_dev_builder.core.customizer import SystemCustomizer
from deb_dev_builder.core.path_utils import rootfs_destination


BACKDROP = Path('usr/share/images/desktop-base/default')
NATIVE_IMAGE = Path('usr/share/images/desktop-base/vendor.png')


def native_background(root):
    image = root / NATIVE_IMAGE
    image.parent.mkdir(parents=True)
    image.write_bytes(b'Native distribution wallpaper')
    alternatives = root / 'etc/alternatives'
    alternatives.mkdir(parents=True)
    (alternatives / 'desktop-background').symlink_to('/' + str(NATIVE_IMAGE))
    (image.parent / 'desktop-background').symlink_to('/etc/alternatives/desktop-background')
    return image


def customizer(root, distro='devuan-ceres', desktop='xfce'):
    return SystemCustomizer(ChrootManager(root, mode='real'), {'distro': distro, 'desktop': desktop})


def test_ceres_xfce_restores_compiled_fallback_using_native_distribution_image(tmp_path):
    image = native_background(tmp_path)
    for _ in range(2):
        customizer(tmp_path).configure_artwork()
    assert (tmp_path / BACKDROP).readlink() == Path('/usr/share/images/desktop-base/desktop-background')
    assert rootfs_destination(tmp_path, BACKDROP, follow_leaf=True) == image
    assert image.read_bytes() == b'Native distribution wallpaper'


@pytest.mark.parametrize('distro,desktop', [
    ('debian-sid', 'xfce'), ('devuan-testing', 'xfce'),
    ('devuan-6', 'xfce'), ('devuan-ceres', 'mate'),
])
def test_wallpaper_compatibility_is_limited_to_ceres_xfce(tmp_path, distro, desktop):
    native_background(tmp_path)
    customizer(tmp_path, distro, desktop).configure_artwork()
    assert not (tmp_path / BACKDROP).is_symlink()
    assert not (tmp_path / BACKDROP).exists()


def test_existing_valid_backdrop_is_preserved(tmp_path):
    path = tmp_path / BACKDROP
    path.parent.mkdir(parents=True)
    path.write_bytes(b'Existing wallpaper')
    customizer(tmp_path).configure_artwork()
    assert path.read_bytes() == b'Existing wallpaper'


def test_broken_backdrop_link_is_repaired_without_touching_host_target(tmp_path):
    root = tmp_path / 'root'
    image = native_background(root)
    outside = tmp_path / 'host-wallpaper'
    outside.write_bytes(b'Host wallpaper')
    (root / BACKDROP).symlink_to(outside)
    customizer(root).configure_artwork()
    assert outside.read_bytes() == b'Host wallpaper'
    assert rootfs_destination(root, BACKDROP, follow_leaf=True) == image


def test_missing_native_image_is_reported_instead_of_creating_a_broken_link(tmp_path):
    with pytest.raises(ValueError, match='installed distribution wallpaper'):
        customizer(tmp_path).configure_artwork()
    assert not (tmp_path / BACKDROP).is_symlink()


def test_leaf_resolution_rejects_loops_and_root_escape(tmp_path):
    (tmp_path / 'loop').symlink_to('/loop')
    with pytest.raises(ValueError, match='Too many'):
        rootfs_destination(tmp_path, 'loop', follow_leaf=True)
    (tmp_path / 'escape').symlink_to('../../outside')
    with pytest.raises(ValueError, match='escapes'):
        rootfs_destination(tmp_path, 'escape', follow_leaf=True)
