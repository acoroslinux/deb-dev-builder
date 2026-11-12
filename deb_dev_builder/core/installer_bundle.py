"""Resolve and retain a complete native live-installer generation before building."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import http.client
import json
import logging
from pathlib import Path
import re
import shutil
import tempfile
import urllib.error
import urllib.request

from .initrd_editor import archive_path, unpack_initrd
from .installer_source import control_fields, installer_mirror, installer_udeb_suite, select_udeb_records
from .path_utils import resolve_from_project

logger = logging.getLogger(__name__)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_bundle(path, identity=None):
    """Recheck every cached byte; a directory or pointer alone proves nothing."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Installer bundle manifest must not be a symlink')
    manifest = json.loads(path.read_text())
    if manifest['schema'] != 1 or (identity is not None and manifest['identity'] != identity):
        raise ValueError('Installer bundle identity does not match the requested source')
    for name, expected in manifest['files'].items():
        relative = archive_path(name)
        item = path.parent / relative
        if item.is_symlink() or not item.is_file() or not item.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError(f'Installer bundle file is missing or unsafe: {name}')
        if digest(item.read_bytes()) != expected:
            raise ValueError(f'Installer bundle checksum mismatch: {name}')
    for name in ['Packages', *manifest['images'].values(), *manifest['packages'].values()]:
        if name not in manifest['files']:
            raise ValueError(f'Installer bundle file is not verified: {name}')
    return manifest


class InstallerBundleResolver:
    def __init__(self, config, policy, image_base):
        self.config, self.policy, self.image_base = config, policy, image_base.rstrip('/')
        self.options = policy['bundle']
        self.arch = config.get('dpkg_arch', config['architecture'])
        self.source = policy['sources'][config.get('di_mode', 'live')]
        self.mirror = installer_mirror(config, policy)
        self.udeb_suite = installer_udeb_suite(config, policy)
        self.identity = {
            'family': config['base_distro'], 'suite': config['suite'], 'architecture': self.arch,
            'source': self.source, 'images': self.image_base, 'archive': self.mirror,
            'udeb_suite': self.udeb_suite, 'required_modules': policy['live_modules'],
            'exclude_udebs': policy['exclude_udebs'], 'kernel_pattern': policy['kernel_udeb_pattern'],
        }
        context = {**self.identity, 'family': config['base_distro']}
        self.cache = resolve_from_project(self.options['directory'].format_map(context))
        self.legacy_cache = resolve_from_project(policy['cache_directory'].format(suite=config['suite'], architecture=self.arch))

    def fetch(self, url):
        error = None
        for _ in range(self.options['download_attempts']):
            try:
                with urllib.request.urlopen(url, timeout=self.policy['download_timeout']) as response:
                    data = response.read()
                    headers = getattr(response, 'headers', {})
                    date = headers.get('Last-Modified')
                return data, date
            except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
                error = exc
        raise OSError(f'Cannot retrieve installer input {url}: {error}') from error

    @staticmethod
    def checksums(data):
        entries = {}
        for line in data.decode().splitlines():
            match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?(.+)', line)
            if match:
                name = archive_path(match.group(2))
                entries[name] = match.group(1).lower()
        if not entries:
            raise ValueError('Installer SHA256SUMS contains no usable checksums')
        return entries

    def _write(self, directory, name, data, expected=None):
        name = archive_path(name)
        actual = digest(data)
        if expected is not None and actual != expected:
            raise ValueError(f'Installer input checksum mismatch: {name}')
        destination = directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return actual

    def _images(self, directory, sums, previous):
        targets = {key: value.format(architecture=self.arch) for key, value in self.policy['targets'][self.source].items()}
        keys = ['kernel', 'initrd']
        if all(self.source + '/' + targets[key] in sums for key in ['gtk_kernel', 'gtk_initrd']):
            keys += ['gtk_kernel', 'gtk_initrd']
        images, files, releases, installed = {}, {}, set(), {}
        for key in keys:
            remote = self.source + '/' + targets[key]
            if remote not in sums:
                raise ValueError(f'Installer SHA256SUMS is missing {remote}')
            local = 'images/' + key
            if previous and previous[1]['files'].get(local) == sums[remote]:
                data = (previous[0].parent / local).read_bytes()
            else:
                data, _ = self.fetch(self.image_base + '/' + remote)
            files[local] = self._write(directory, local, data, sums[remote])
            images[key] = local
            if key in ('initrd', 'gtk_initrd'):
                entries = unpack_initrd(data)
                abi = {m.group(1) for name in entries if (m := re.match(r'^(?:usr/)?lib/modules/([^/]+)(?:/|$)', name))}
                if len(abi) != 1:
                    raise ValueError(f'Installer {key} must contain exactly one kernel release')
                releases.update(abi)
                status = entries.get('var/lib/dpkg/status')
                if status:
                    for paragraph in status.data.decode().split('\n\n'):
                        fields = control_fields(paragraph)
                        if 'Package' in fields and 'Version' in fields:
                            name, version = fields['Package'], fields['Version']
                            # Native media can legitimately predate userspace
                            # security updates. Record these versions without
                            # requiring unchanged archive packages. Kernel ABI
                            # matching remains mandatory below.
                            installed.setdefault(name, set()).add(version)
        if len(releases) != 1:
            raise ValueError('Text and graphical installer kernel generations differ')
        return images, files, releases.pop(), installed

    def _repositories(self, image_date):
        yield self.mirror
        mirrors = self.options['snapshot_mirrors'].get(self.config['base_distro'], [])
        if not mirrors or not image_date:
            return
        date = parsedate_to_datetime(image_date).astimezone(timezone.utc)
        for offset in self.options['snapshot_offsets_hours']:
            timestamp = (date + timedelta(hours=offset)).strftime('%Y%m%dT%H%M%SZ')
            for mirror in mirrors:
                yield mirror.format(timestamp=timestamp).rstrip('/')

    def _repository(self, directory, repository, release):
        index_path = self.policy['udeb_index'].format(suite=self.udeb_suite, architecture=self.arch)
        compressed, _ = self.fetch(repository + '/' + index_path)
        index = gzip.decompress(compressed).decode()
        records = select_udeb_records(index, release, self.policy)
        logger.info('Freezing %s native installer udebs for kernel %s from %s', len(records), release, repository)

        def download(record):
            _, fields = record
            relative = archive_path(fields['Filename'])
            name = Path(relative).name
            expected = fields['SHA256']
            candidates = [directory / 'packages' / name, self.legacy_cache / name]
            cached = next((path for path in candidates if path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == expected), None)
            data = cached.read_bytes() if cached else self.fetch(repository + '/' + relative)[0]
            local = 'packages/' + name
            return fields['Package'], local, self._write(directory, local, data, expected)

        with ThreadPoolExecutor(max_workers=self.policy['download_workers']) as executor:
            downloaded = list(executor.map(download, records))
        packages = {name: local for name, local, _ in downloaded}
        files = {local: checksum for _, local, checksum in downloaded}
        files['Packages'] = self._write(directory, 'Packages', index.encode())
        return packages, files

    def _previous(self):
        try:
            pointer = json.loads((self.cache / 'last-good.json').read_text())
            generation = pointer['generation']
            if not re.fullmatch(r'[0-9a-f]{64}', generation):
                raise ValueError('Invalid installer bundle generation')
            path = self.cache / generation / 'manifest.json'
            if digest(path.read_bytes()) != pointer['manifest_sha256']:
                raise ValueError('Installer bundle manifest checksum mismatch')
            return path, load_bundle(path, self.identity)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if (self.cache / 'last-good.json').exists():
                logger.warning('Ignoring incomplete or corrupt cached installer bundle: %s', exc)
            return None

    def resolve(self):
        logger.info('Resolving a complete native %s installer bundle for %s/%s', self.config['base_distro'], self.config['suite'], self.arch)
        self.cache.mkdir(parents=True, exist_ok=True)
        previous = self._previous()
        errors = []
        for attempt in range(self.options['generation_attempts']):
            try:
                raw_sums, image_date = self.fetch(self.image_base + '/SHA256SUMS')
                sums = self.checksums(raw_sums)
                if previous and previous[1]['checksums_sha256'] == digest(raw_sums):
                    logger.info('Reusing verified native installer bundle %s', previous[0].parent.name)
                    return previous[0]
                with tempfile.TemporaryDirectory(prefix='.resolve-', dir=self.cache) as temporary:
                    directory = Path(temporary)
                    images, image_files, release, installed = self._images(directory, sums, previous)
                    repositories = list(dict.fromkeys(self._repositories(image_date)))
                    for repository in repositories:
                        try:
                            packages, package_files = self._repository(directory, repository, release)
                            manifest = {
                                'schema': 1, 'identity': self.identity, 'kernel': release,
                                'image_base': self.image_base, 'image_date': image_date,
                                'image_packages': {name: sorted(versions) for name, versions in installed.items()},
                                'repository': repository, 'checksums_sha256': digest(raw_sums),
                                'images': images, 'packages': packages, 'files': {**image_files, **package_files},
                            }
                            data = json.dumps(manifest, indent=2, sort_keys=True).encode()
                            generation = digest(data)
                            final = self.cache / generation
                            if final.exists():
                                shutil.rmtree(final)
                            (directory / 'manifest.json').write_bytes(data)
                            directory.rename(final)
                            path = final / 'manifest.json'
                            load_bundle(path, self.identity)
                            pending = self.cache / '.last-good.json'
                            pending.write_text(json.dumps({'generation': generation, 'manifest_sha256': digest(data)}))
                            pending.replace(self.cache / 'last-good.json')
                            logger.info('Frozen native installer bundle %s: kernel %s, udebs from %s', generation, release, repository)
                            return path
                        except (OSError, ValueError, KeyError) as exc:
                            errors.append(f'{repository}: {exc}')
                            logger.warning('Installer generation candidate rejected: %s', errors[-1])
            except (OSError, ValueError, KeyError) as exc:
                errors.append(str(exc))
                logger.warning('Could not resolve installer generation (attempt %s): %s', attempt + 1, exc)
        if previous:
            # Revalidate after the failed refresh; never fall back to damaged bytes.
            load_bundle(previous[0], self.identity)
            logger.warning('Upstream installer inputs are unavailable or inconsistent; using verified cached generation %s', previous[0].parent.name)
            return previous[0]
        raise ValueError('No complete compatible native installer bundle is available. ' + ' | '.join(errors[-4:]))
