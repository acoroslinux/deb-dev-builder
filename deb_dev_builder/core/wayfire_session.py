"""Runtime keyboard defaults for Wayfire without changing the user's config."""

WAYFIRE_SESSION_SCRIPT = r'''#!/usr/bin/python3
import os
from pathlib import Path
import re
import tempfile


def keyboard_config(content, environ):
    defaults = {
        'xkb_layout': environ.get('XKB_DEFAULT_LAYOUT', 'us'),
        'xkb_variant': environ.get('XKB_DEFAULT_VARIANT', ''),
        'xkb_options': environ.get('XKB_DEFAULT_OPTIONS', ''),
        'xkb_model': environ.get('XKB_DEFAULT_MODEL', 'pc105'),
    }
    lines = content.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip() == '[input]'), None)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines)) if start is not None else len(lines)
    present = {match.group(1) for line in lines[start + 1:end] if (match := re.match(r'\s*(xkb_\w+)\s*=', line))} if start is not None else set()
    additions = ''.join(key + ' = ' + value.replace('\n', '').replace('\r', '') + '\n' for key, value in defaults.items() if key not in present)
    if start is None:
        return content + '\n[input]\n' + additions
    lines.insert(start + 1, additions)
    return ''.join(lines)


def main():
    runtime = Path(os.environ['XDG_RUNTIME_DIR'])
    if not runtime.is_dir() or runtime.stat().st_uid != os.getuid():
        raise RuntimeError('Wayfire requires a user-owned runtime directory')
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    source = config_home / 'wayfire.ini'
    content = source.read_text()
    descriptor, filename = tempfile.mkstemp(prefix='deb-dev-wayfire-', suffix='.ini', dir=runtime)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(keyboard_config(content, os.environ))
    os.execvp('wayfire', ['wayfire', '-c', filename])


if __name__ == '__main__':
    main()
'''
