"""Moving archives must never mix native installer generations."""
import gzip
from pathlib import Path

import pytest

from deb_dev_builder.core.installer_bundle import InstallerBundleResolver, digest, load_bundle
from deb_dev_builder.core.installer_media import LiveInstallerMedia, preseed_archive
from deb_dev_builder.core.iso_engine import ISOEngine, ISOEngineError
from deb_dev_builder.core.runtime_config import load_runtime, RuntimeConfigError


def source(tmp_path, monkeypatch, family='debian'):
    config = dict(base_distro=family, suite='sid' if family == 'debian' else 'ceres',
                  architecture='amd64', dpkg_arch='amd64', di_mode='live', with_debian_installer=True)
    engine = ISOEngine(tmp_path / 'work', tmp_path / 'root', 'test', config, 'real', None)
    policy = engine.runtime['installer']
    policy['bundle']['directory'] = str(tmp_path / 'cache' / '{family}' / '{suite}' / '{architecture}' / '{source}')
    policy['bundle']['generation_attempts'] = 1
    policy['bundle']['snapshot_offsets_hours'] = [0]
    policy['cache_directory'] = str(tmp_path / 'legacy')
    policy['offline_package_sets'] = {}
    resolver = InstallerBundleResolver(config, policy, engine._installer_image_url().rsplit('/', 1)[0])
    initrd = preseed_archive('', [('usr/lib/modules/7.test/modules.dep', b'', 0o100644),
                                  ('var/lib/dpkg/status', b'Package: busybox-udeb\nVersion: 1\n', 0o100644)])
    images = {'cdrom/vmlinuz': b'kernel', 'cdrom/initrd.gz': initrd,
              'cdrom/gtk/vmlinuz': b'kernel', 'cdrom/gtk/initrd.gz': initrd}
    urls = {resolver.image_base + '/' + name: (data, None) for name, data in images.items()}
    urls[resolver.image_base + '/SHA256SUMS'] = (''.join(digest(data) + '  ./' + name + '\n' for name, data in images.items()).encode(), 'Wed, 11 Nov 2026 00:00:00 GMT')
    calls = []

    def fetch(url):
        calls.append(url)
        if url not in urls:
            raise OSError('Unavailable upstream input: ' + url)
        return urls[url]

    monkeypatch.setattr(resolver, 'fetch', fetch)
    return engine, resolver, urls, calls


def repository(resolver, urls, mirror=None, release='7.test', busybox_version='1', broken=False):
    mirror = mirror or resolver.mirror
    records = []
    for name in [*resolver.policy['live_modules'], 'busybox-udeb', f'kernel-image-{release}-di']:
        data = ('verified ' + name).encode()
        filename = f'pool/native/{name}.udeb'
        version = busybox_version if name == 'busybox-udeb' else '1'
        records.append(f'Package: {name}\nVersion: {version}\nFilename: {filename}\nSHA256: {digest(data)}\n')
        urls[mirror + '/' + filename] = (b'corrupted' if broken else data, None)
    index = '\n'.join(records).encode()
    relative = resolver.policy['udeb_index'].format(suite=resolver.udeb_suite, architecture=resolver.arch)
    urls[mirror + '/' + relative] = (gzip.compress(index), None)


def test_snapshot_recovers_removed_abi_and_freezes_full_generation(tmp_path, monkeypatch):
    engine, resolver, urls, calls = source(tmp_path, monkeypatch)
    repository(resolver, urls, release='7.new')
    snapshot = list(resolver._repositories('Wed, 11 Nov 2026 00:00:00 GMT'))[1]
    repository(resolver, urls, snapshot)
    path = resolver.resolve()
    bundle = load_bundle(path, resolver.identity)
    assert bundle['repository'] == snapshot and bundle['kernel'] == '7.test'
    assert len(bundle['packages']) == 3
    assert any(url.startswith(snapshot) and url.endswith('.udeb') for url in calls)
    calls.clear()
    # Repository updates cannot replace packages while the image is unchanged.
    repository(resolver, urls, release='7.newer')
    assert resolver.resolve() == path
    assert calls == [resolver.image_base + '/SHA256SUMS']


@pytest.mark.parametrize('family', ['debian', 'devuan'])
def test_last_good_bundle_survives_failed_upstream_refresh(tmp_path, monkeypatch, family):
    _, resolver, urls, calls = source(tmp_path, monkeypatch, family)
    repository(resolver, urls)
    path = resolver.resolve()
    urls[resolver.image_base + '/SHA256SUMS'] = (b'upstream publishing an incomplete generation', None)
    assert resolver.resolve() == path
    urls.clear()
    assert resolver.resolve() == path
    assert (resolver.cache / 'last-good.json').is_file()
    if family == 'devuan':
        assert 'choose-init' in load_bundle(path)['packages']
        assert all('snapshot.debian.org' not in url and 'd-i.debian.org' not in url for url in calls)
        assert '/devuan/ceres/' in str(path)


def test_devuan_cold_cache_rejects_mismatched_native_archive(tmp_path, monkeypatch):
    _, resolver, urls, calls = source(tmp_path, monkeypatch, 'devuan')
    repository(resolver, urls, release='7.new')
    with pytest.raises(ValueError, match='No complete compatible native installer bundle'):
        resolver.resolve()
    assert not (resolver.cache / 'last-good.json').exists()
    assert all('snapshot.debian.org' not in url for url in calls)


@pytest.mark.parametrize('failure', ['package', 'image'])
def test_partial_or_inconsistent_generation_is_never_promoted(tmp_path, monkeypatch, failure):
    _, resolver, urls, _ = source(tmp_path, monkeypatch)
    repository(resolver, urls, broken=failure == 'package')
    if failure == 'image':
        urls[resolver.image_base + '/cdrom/initrd.gz'] = (b'changed during publication', None)
    with pytest.raises(ValueError, match='No complete compatible native installer bundle'):
        resolver.resolve()
    assert not (resolver.cache / 'last-good.json').exists()


def test_corrupt_cache_is_not_used_as_fallback(tmp_path, monkeypatch):
    _, resolver, urls, _ = source(tmp_path, monkeypatch)
    repository(resolver, urls)
    path = resolver.resolve()
    (path.parent / 'images/initrd').write_bytes(b'corrupt cached bytes')
    urls.clear()
    with pytest.raises(ValueError, match='No complete compatible native installer bundle'):
        resolver.resolve()


def test_build_consumes_pinned_images_index_and_packages_without_refresh(tmp_path, monkeypatch):
    engine, resolver, urls, calls = source(tmp_path, monkeypatch)
    repository(resolver, urls)
    path = resolver.resolve()
    engine.config['_installer_bundle'] = str(path)
    engine.prepare_installer_bundle()
    assert engine.config['installer_bundle']['kernel'] == '7.test'
    stage = tmp_path / 'stage'
    engine._stage_debian_installer(stage / 'install')
    before = (path.parent / 'images/initrd').read_bytes()
    (stage / 'install/initrd.gz').write_bytes(before + preseed_archive('customized=true'))
    assert (path.parent / 'images/initrd').read_bytes() == before
    media = LiveInstallerMedia(engine.config, resolver.policy)
    monkeypatch.setattr(media, '_archive_tool', lambda tool, stage, args, output: output.write_text('metadata\n'))
    import io
    network = []
    def fetch(url, **kwargs):
        network.append(url)
        assert url.endswith('/binary-amd64/Release')
        return io.BytesIO(b'Architecture: amd64\nComponent: main\n')
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    media.prepare(stage, None)
    assert len(network) == 1
    index = stage / 'dists/sid/main/debian-installer/binary-amd64/Packages'
    assert 'kernel-image-7.test-di' in index.read_text()
    assert (stage / 'pool-udeb/main/l/live-installer/live-installer.udeb').is_file()


def test_preflight_failure_prevents_rootfs_work(tmp_path, monkeypatch):
    from deb_dev_builder.core.orchestrator import BuildOrchestrator
    from deb_dev_builder.core.toolchain_manager import ToolchainManager
    build = BuildOrchestrator(distro='debian-testing', with_debian_installer=True, mode='mock')
    build.workdir = tmp_path / 'work'
    build.target_root = build.workdir / 'chroot'
    def fail(self):
        raise ISOEngineError('No coherent installer bundle')
    monkeypatch.setattr(ISOEngine, 'prepare_installer_bundle', fail)
    def unexpected(self):
        pytest.fail('Toolchain setup must follow installer preflight')
    monkeypatch.setattr(ToolchainManager, 'setup', unexpected)
    with pytest.raises(ISOEngineError, match='coherent'):
        build.build(output_name=str(tmp_path / 'output'))


@pytest.mark.parametrize('options', [dict(download_attempts=0), dict(generation_attempts=99),
                                   dict(snapshot_offsets_hours=['yesterday']), dict(directory='../outside'),
                                   dict(snapshot_mirrors={'devuan': ['https://example.test/archive']})])
def test_invalid_bundle_policy_is_rejected_before_build(options):
    with pytest.raises(RuntimeConfigError):
        load_runtime({'runtime': {'installer': {'bundle': options}}})


def test_frontend_specific_installer_status_is_not_an_archive_package(tmp_path, monkeypatch):
    from deb_dev_builder.core.initrd_editor import Entry, pack_initrd, unpack_initrd
    _, resolver, urls, _ = source(tmp_path, monkeypatch)
    repository(resolver, urls)
    for frontend, relative in [('text', 'cdrom/initrd.gz'), ('gtk', 'cdrom/gtk/initrd.gz')]:
        data, _ = urls[resolver.image_base + '/' + relative]
        entries = unpack_initrd(data)
        entries['var/lib/dpkg/status'] = Entry('var/lib/dpkg/status',
            f'Package: busybox-udeb\nVersion: 1\n\nPackage: debian-installer\nVersion: cdrom-{frontend}-20261111\n'.encode(), 0o100644)
        data = pack_initrd(entries)
        urls[resolver.image_base + '/' + relative] = (data, None)
    image_urls = {name.removeprefix(resolver.image_base + '/'): data for name, (data, _) in urls.items() if name.startswith(resolver.image_base + '/cdrom/')}
    urls[resolver.image_base + '/SHA256SUMS'] = (''.join(digest(data) + '  ' + name + '\n' for name, data in image_urls.items()).encode(), None)
    assert load_bundle(resolver.resolve())['kernel'] == '7.test'


def test_download_retries_transient_failures(tmp_path, monkeypatch):
    import io
    import urllib.error
    _, resolver, _, _ = source(tmp_path, monkeypatch)
    # Exercise the real fetch implementation rather than the fixture transport.
    del resolver.fetch
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.URLError('temporary mirror error')
        return io.BytesIO(b'complete input')
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    assert resolver.fetch('https://native.test/input') == (b'complete input', None)
    assert len(calls) == 3


def test_native_userspace_updates_do_not_require_identical_embedded_versions(tmp_path, monkeypatch):
    _, resolver, urls, _ = source(tmp_path, monkeypatch, 'devuan')
    repository(resolver, urls, busybox_version='2')
    manifest = load_bundle(resolver.resolve())
    assert manifest['image_packages']['busybox-udeb'] == ['1']
    assert 'Version: 2' in (resolver.resolve().parent / 'Packages').read_text()


def test_publication_race_is_retried_as_a_whole_generation(tmp_path, monkeypatch):
    _, resolver, urls, calls = source(tmp_path, monkeypatch)
    repository(resolver, urls)
    resolver.options['generation_attempts'] = 2
    original = resolver.fetch
    attempts = []
    def fetch(url):
        if url == resolver.image_base + '/SHA256SUMS':
            attempts.append(url)
            if len(attempts) == 1:
                return b'incomplete publication', None
        return original(url)
    monkeypatch.setattr(resolver, 'fetch', fetch)
    assert load_bundle(resolver.resolve())['kernel'] == '7.test'
    assert len(attempts) == 2


def test_cached_bundle_from_another_distribution_is_rejected(tmp_path, monkeypatch):
    _, resolver, urls, _ = source(tmp_path, monkeypatch, 'devuan')
    repository(resolver, urls)
    path = resolver.resolve()
    with pytest.raises(ValueError, match='identity does not match'):
        load_bundle(path, {**resolver.identity, 'family': 'debian'})
