import gzip
import hashlib
import io
from pathlib import Path

import pytest

from deb_dev_builder.core.installer_media import LiveInstallerMedia, preseed_archive, apt_source_hooks
from deb_dev_builder.core.iso_engine import ISOEngine
from deb_dev_builder.core.runtime_config import load_runtime
import subprocess
import shutil


@pytest.mark.parametrize("has_main", [False, True])
def test_target_apt_sources_are_isolated_and_restored_without_live_mount_uri(tmp_path, has_main):
    target = tmp_path / "installed system"
    policy = load_runtime()["customization"]["offline_repository"]
    parts = target / policy["sources_directory"]
    parts.mkdir(parents=True)
    live = target / policy["source_file"]
    live.write_text(policy["entry_template"].format(uri=policy["live_uri"]))
    source = parts / "debian.sources"
    original_deb822 = "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\n"
    source.write_text(original_deb822)
    other = parts / "custom.list"
    other.write_text("deb https://example.test/debian trixie main\n")
    keep = parts / "notes.txt"
    keep.write_text("not an APT source")
    main = target / policy["main_source_file"]
    if has_main:
        main.write_text("deb https://deb.debian.org/debian trixie main\n")
    prepare, finish = apt_source_hooks(target, policy)
    subprocess.run(["sh", "-ec", prepare], check=True)
    assert not live.exists() and not source.exists() and not other.exists()
    assert not main.exists()
    assert keep.read_text() == "not an APT source"
    # Native d-i configure_apt / apt-setup creates only the CD source.
    main.write_text("deb cdrom:[Offline ISO]/ trixie main\n")
    subprocess.run(["sh", "-ec", prepare], check=True)
    for _ in range(2):
        subprocess.run(["sh", "-ec", finish], check=True)
    assert not live.exists()
    assert source.read_text() == original_deb822
    assert other.read_text() == "deb https://example.test/debian trixie main\n"
    if has_main:
        assert main.read_text() == "deb https://deb.debian.org/debian trixie main\n"
    else:
        assert not main.exists()
    assert not (target / policy["installer_backup_directory"]).exists()
    assert (target / policy["restored_marker"]).is_file()


def config(mode="live"):
    return {"base_distro": "debian", "suite": "trixie", "architecture": "amd64", "dpkg_arch": "amd64", "di_mode": mode, "with_debian_installer": True}


def test_live_copy_layout_guard_precedes_native_hooks_and_is_required_by_final_validation(tmp_path):
    from deb_dev_builder.core.initrd_editor import unpack_initrd
    cfg = config()
    policy = load_runtime(cfg)['installer']
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'lib64').symlink_to('usr/lib64')
    install = tmp_path / 'install'
    install.mkdir()
    (install / 'initrd.gz').write_bytes(preseed_archive(''))
    LiveInstallerMedia(cfg, policy).embed_preseed(install, source_root=root)
    entries = unpack_initrd((install / 'initrd.gz').read_bytes())
    hook = entries[policy['layout_hook']]
    assert hook.mode & 0o111
    assert Path(policy['layout_hook']).name < 'openssh-server'
    assert b'usr/lib64' in hook.data and b'chroot "$target" /usr/bin/true' in hook.data
    assert policy['layout_marker'].encode() in entries[policy['target_boot_hook']].data


@pytest.mark.parametrize('suite', ['testing', 'forky', 'sid'])
@pytest.mark.parametrize('mode', ['live', 'netinstall', 'netboot'])
def test_moving_debian_suites_pair_daily_images_with_sid_udebs(tmp_path, suite, mode):
    from deb_dev_builder.core.installer_source import installer_udeb_suite, default_preseed
    cfg = {**config(mode), 'suite': suite}
    engine = ISOEngine(tmp_path, tmp_path / 'root', 'test', cfg, 'mock', None)
    source = 'cdrom' if mode == 'live' else 'netboot'
    assert engine._installer_image_url('initrd.gz') == f'https://d-i.debian.org/daily-images/amd64/daily/{source}/initrd.gz'
    assert engine._installer_image_url('netboot.tar.gz').endswith('/daily/netboot/netboot.tar.gz')
    assert installer_udeb_suite(cfg, engine.runtime['installer']) == 'sid'
    preseed = default_preseed('', cfg, engine.runtime['installer'])
    assert f'd-i mirror/suite string {suite}\n' in preseed
    assert 'd-i mirror/udeb/suite string sid\n' in preseed
    assert cfg['suite'] == suite


def test_explicit_image_mirror_keeps_archive_layout_and_suite(tmp_path):
    from deb_dev_builder.core.installer_source import installer_udeb_suite
    cfg = {**config(), 'suite': 'testing', 'installer_image_mirror': 'https://images.example/debian'}
    engine = ISOEngine(tmp_path, tmp_path / 'root', 'test', cfg, 'mock', None)
    assert engine._installer_image_url('vmlinuz') == 'https://images.example/debian/dists/testing/main/installer-amd64/current/images/cdrom/vmlinuz'
    assert installer_udeb_suite(cfg, engine.runtime['installer']) == 'testing'


@pytest.mark.parametrize('matches', [True, False])
def test_daily_live_repository_requires_exact_kernel_abi_and_preserves_target_suite(tmp_path, monkeypatch, matches):
    cfg = {**config(), 'suite': 'testing'}
    policy = load_runtime(cfg)['installer']
    policy['offline_package_sets'] = {}
    policy['cache_directory'] = str(tmp_path / 'cache')
    staging = tmp_path / 'iso'
    (staging / 'install').mkdir(parents=True)
    (staging / 'install/initrd.gz').write_bytes(preseed_archive('', [('usr/lib/modules/7.test-amd64/modules.dep', b'', 0o100644)]))
    release = '7.test-amd64' if matches else '6.old-amd64'
    data = b'verified installer payload'
    index = '\n\n'.join(f'Package: {name}\nFilename: pool/main/t/test/{name}.udeb\nSHA256: {hashlib.sha256(data).hexdigest()}\n' for name in ['live-installer', f'kernel-image-{release}-di'])
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith('Packages.gz'):
            return io.BytesIO(gzip.compress(index.encode()))
        if url.endswith('/Release'):
            return io.BytesIO(b'Architecture: amd64\nComponent: main\n')
        return io.BytesIO(data)
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    media = LiveInstallerMedia(cfg, policy)
    monkeypatch.setattr(media, '_archive_tool', lambda tool, stage, args, output: output.write_text('test metadata\n'))
    if matches:
        media.prepare(staging, None)
        assert (staging / 'dists/testing/main/debian-installer/binary-amd64/Packages.gz').is_file()
        assert (staging / 'dists/sid').resolve() == (staging / 'dists/testing').resolve()
    else:
        with pytest.raises(ValueError, match='no matching module udebs'):
            media.prepare(staging, None)
        assert not any(url.endswith('.udeb') for url in calls)
    assert calls[0].endswith('/dists/sid/main/debian-installer/binary-amd64/Packages.gz')


@pytest.mark.parametrize("mode,source,kernel", [("live", "cdrom", "vmlinuz"), ("netinstall", "netboot", "debian-installer/amd64/linux")])
def test_installer_sources_and_boot_menus(tmp_path, mode, source, kernel):
    cfg = config(mode)
    cfg["desktop"] = "xfce"
    engine = ISOEngine(tmp_path, tmp_path / "root", str(tmp_path / "artifact"), cfg, "mock", None)
    assert engine._installer_image_url(kernel).endswith(f"/images/{source}/{kernel}")
    assert engine._installer_image_url("netboot.tar.gz").endswith("/netboot/netboot.tar.gz")
    downloads = []
    engine.mode = "real"
    def fake_download(relative, destination):
        downloads.append(relative)
        destination.write_bytes(b"installer")
    engine._download_installer_file = fake_download
    engine._stage_debian_installer(tmp_path / "stage")
    assert downloads[0] == kernel
    engine.mode = "mock"
    engine.build_iso()
    menu = (engine.iso_staging / "boot/grub/grub.cfg").read_text()
    assert ("Install live system" if mode == "live" else "Network install") in menu
    assert "preseed/file=/cdrom/install/preseed.cfg" in menu
    assert (engine.iso_staging / "live/filesystem.squashfs").exists() == (mode == "live")
    preseed = (engine.iso_staging / "install/preseed.cfg").read_text()
    if mode == "live":
        assert "live-installer/enable boolean true" in preseed
        assert "pkgsel/include string xfce4" not in preseed
        assert "late_command.sh" not in preseed


def test_local_live_repository_and_cached_hashes(tmp_path, monkeypatch):
    cfg = config()
    policy = load_runtime(cfg)["installer"]
    policy["cache_directory"] = str(tmp_path / "cache")
    policy["offline_package_sets"] = {}
    data = b"verified udeb payload"
    index = f"Package: live-installer\nFilename: pool/main/l/live-installer/live-installer.udeb\nSHA256: {hashlib.sha256(data).hexdigest()}\nSize: {len(data)}\n\n"
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("/Release"):
            return io.BytesIO(b"Archive: stable\nArchitecture: amd64\nComponent: main\n")
        return io.BytesIO(gzip.compress(index.encode()) if url.endswith("Packages.gz") else data)
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    media = LiveInstallerMedia(cfg, policy)
    monkeypatch.setattr(media, "_archive_tool", lambda tool, staging, args, output: output.write_text("test Release\n"))
    for number in range(2):
        staging = tmp_path / str(number)
        media.prepare(staging, None)
        assert (staging / "pool-udeb/main/l/live-installer/live-installer.udeb").read_bytes() == data
        assert (staging / ".disk/udeb_include").read_text() == "live-installer\n"
        assert "calamares\n" in (staging / "live/filesystem.packages-remove").read_text()
        assert (staging / "dists/trixie/Release").exists()
        assert gzip.decompress((staging / "dists/trixie/main/debian-installer/binary-amd64/Packages.gz").read_bytes()).decode().strip() == index.replace("Filename: pool/", "Filename: pool-udeb/").strip()
    assert sum(url.endswith(".udeb") for url in calls) == 1


@pytest.mark.skipif(not shutil.which("apt-ftparchive") or not shutil.which("dpkg-deb"), reason="Local archive tools needed for repository contract test")
def test_repository_layout_and_release_follow_live_build_contract(tmp_path, monkeypatch):
    cfg = config()
    # This synthetic repository exercises pool layout, not the boot-support
    # dependency closure supplied by the orchestrator in production.
    cfg["runtime"] = {"build": {"installer_storage": {"packages": [], "init_packages": {"systemd": []}}}}
    repo = tmp_path / "repo"
    repo.mkdir()
    package = tmp_path / "package"
    (package / "DEBIAN").mkdir(parents=True)
    (package / "DEBIAN/control").write_text("Package: sample\nSource: libsample\nVersion: 1\nArchitecture: all\nSection: utils\nMaintainer: Test <test@example.invalid>\nDescription: Repository contract test\n")
    subprocess.run(["dpkg-deb", "--build", str(package), str(repo / "sample_1_all.deb")], check=True, capture_output=True)
    result = subprocess.run(["apt-ftparchive", "packages", "."], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "Packages").write_text(result.stdout)
    cfg["offline_repo_dir"] = str(repo)
    policy = load_runtime(cfg)["installer"]
    policy["offline_package_sets"] = {"amd64": [["sample"]]}
    policy["cache_directory"] = str(tmp_path / "cache")
    data = b"test udeb"
    index = f"Package: live-installer\nFilename: pool/main/l/live-installer/live-installer.udeb\nSHA256: {hashlib.sha256(data).hexdigest()}\n"
    def fetch(url, **kwargs):
        if url.endswith("/Release"):
            return io.BytesIO(b"Archive: stable\nArchitecture: amd64\nComponent: main\n")
        return io.BytesIO(gzip.compress(index.encode()) if url.endswith("Packages.gz") else data)
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    class LocalArchiveRunner:
        def run_in_build_host(self, command, check=True):
            # Explicit local QA runner; production always uses its build host.
            return subprocess.run(command, check=check, capture_output=True)
    staging = tmp_path / "iso"
    LiveInstallerMedia(cfg, policy).prepare(staging, LocalArchiveRunner())
    assert (staging / "pool/main/libs/libsample/sample_1_all.deb").is_file()
    assert (staging / "pool-udeb/main/l/live-installer/live-installer.udeb").is_file()
    binary = staging / "dists/trixie/main/binary-amd64"
    assert "Filename: pool/main/libs/libsample/sample_1_all.deb" in (binary / "Packages").read_text()
    assert "Package: live-installer" not in (binary / "Packages").read_text()
    assert "Architecture: amd64" in (binary / "Release").read_text()
    release = (staging / "dists/trixie/Release").read_text()
    for field in ("Date:", "MD5Sum:", "SHA256:", "SHA512:", "Components: main"):
        assert field in release
    assert "main/binary-amd64/Release" in release
    assert (staging / ".disk/base_components").read_text() == "main\n"
    assert (staging / ".disk/cd_type").read_text() == "live\n"
    assert (staging / "debian").readlink() == Path(".")


def test_rejects_damaged_component(tmp_path, monkeypatch):
    cfg = config()
    policy = load_runtime(cfg)["installer"]
    policy["cache_directory"] = str(tmp_path / "cache")
    policy["offline_package_sets"] = {}
    index = "Package: live-installer\nFilename: pool/live.udeb\nSHA256: " + "0" * 64 + "\n"
    monkeypatch.setattr("urllib.request.urlopen", lambda url, **kwargs: io.BytesIO(gzip.compress(index.encode()) if url.endswith("Packages.gz") else b"damaged"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        LiveInstallerMedia(cfg, policy).prepare(tmp_path / "iso", None)
    assert not list((tmp_path / "cache").glob("*.partial"))


def test_live_media_rejects_repo_without_grub_metapackage_before_download(tmp_path, monkeypatch):
    cfg = config()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Packages").write_text("Package: grub-efi-amd64-bin\n")
    cfg["offline_repo_dir"] = str(repo)
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid offline repository must fail before downloading installer components")
    monkeypatch.setattr("urllib.request.urlopen", unexpected)
    with pytest.raises(ValueError, match="grub-efi-amd64"):
        LiveInstallerMedia(cfg, load_runtime(cfg)["installer"]).prepare(tmp_path / "iso", None)


def test_preseed_and_cleanup_are_embedded_in_both_initrds(tmp_path):
    cfg = config()
    media = LiveInstallerMedia(cfg, load_runtime(cfg)["installer"])
    original = preseed_archive("original preseed")
    for path in (tmp_path / "initrd.gz", tmp_path / "gtk/initrd.gz"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
    media.embed_preseed(tmp_path, "d-i custom/answer string yes\n")
    for path in (tmp_path / "initrd.gz", tmp_path / "gtk/initrd.gz"):
        content = gzip.decompress(path.read_bytes())
        assert b"usr/lib/live-installer.d\0" in content
        assert b"live-installer/enable boolean true" in content
        assert b"live-installer/mode select normal" in content
        assert b"chroot /target userdel --remove -- liveuser" in content
        assert b"d-i custom/answer string yes" in content
        assert b'APT::Authentication::TrustCDROM' not in content
        assert b'APT::Install-Recommends "false";' in content
        assert b"usr/lib/live-installer.d/98deb-dev-offline-apt" in content
        assert b"usr/lib/finish-install.d/15deb-dev-offline-cdrom" in content
        assert b"rm -f -- /target/etc/apt/apt.conf.d/zz-deb-dev-offline-recommends" in content
        assert b"AllowUnauthenticated" not in content
    with pytest.raises(ValueError, match="initramfs include"):
        preseed_archive("", [("../bad", b"", 0)])


@pytest.mark.skipif(not shutil.which("apt-config"), reason="APT configuration parser needed")
def test_installer_policy_overrides_live_optimization_without_overriding_native_cd_trust(tmp_path):
    import os
    parts = tmp_path / "apt.conf.d"
    parts.mkdir()
    # The first file is created by Debian base-installer configure_apt_preferences.
    (parts / "00trustcdrom").write_text('APT::Authentication::TrustCDROM "true";\n')
    (parts / "99optimize").write_text('APT::Install-Recommends "true";\n')
    policy = load_runtime()["installer"]["offline_apt_config"]
    (parts / Path(policy["path"]).name).write_text(policy["content"])
    config_file = tmp_path / "apt.conf"
    config_file.write_text(f'Dir::Etc::Parts "{parts}";\nDir::Etc::main "/dev/null";\n')
    result = subprocess.run(["apt-config", "shell", "RECOMMENDS", "APT::Install-Recommends", "CDTRUST", "APT::Authentication::TrustCDROM"], env={**os.environ, "APT_CONFIG": str(config_file)}, check=True, capture_output=True, text=True)
    assert "RECOMMENDS='false'" in result.stdout
    assert "CDTRUST='true'" in result.stdout
