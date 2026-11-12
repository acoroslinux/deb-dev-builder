"""Exercise the shipped theme using Debian's actual script/image/sprite engine."""
import ctypes
import io
from pathlib import Path
import tarfile
import subprocess

import pytest

from deb_dev_builder.core.path_utils import resolve_from_project
from deb_dev_builder.core.plymouth_theme import render_theme_script
from deb_dev_builder.core.runtime_config import load_runtime


class ScriptReturn(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('object', ctypes.c_void_p)]


class ScriptState(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p), ('globals', ctypes.c_void_p),
                ('locals', ctypes.c_void_p), ('this', ctypes.c_void_p)]


@pytest.fixture
def engine(tmp_path, request):
    packages = sorted(resolve_from_project('cache/debian').glob('*/amd64/apt/archives/plymouth_*.deb'))
    if not packages:
        pytest.skip('Cached native Debian Plymouth plugin required')
    payload = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(packages[-1])], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        member = next(item for item in archive if item.name.endswith('/plymouth/script.so'))
        plugin = tmp_path / 'script.so'
        plugin.write_bytes(archive.extractfile(member).read())
    try:
        native = ctypes.CDLL(str(plugin))
        ply = ctypes.CDLL('libply.so.5')
    except OSError as exc:
        pytest.skip(f'Native Plymouth runtime libraries unavailable: {exc}')
    signatures = {
        'script_state_new': ([ctypes.c_void_p], ctypes.c_void_p),
        'script_lib_plymouth_setup': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
        'script_lib_plymouth_on_display_message': ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p], None),
        'script_lib_plymouth_on_hide_message': ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p], None),
        'script_lib_plymouth_on_display_normal': ([ctypes.c_void_p, ctypes.c_void_p], None),
        'script_lib_plymouth_on_refresh': ([ctypes.c_void_p, ctypes.c_void_p], None),
        'script_parse_string': ([ctypes.c_char_p, ctypes.c_char_p], ctypes.c_void_p),
        'script_execute': ([ctypes.c_void_p, ctypes.c_void_p], ScriptReturn),
        'script_lib_image_setup': ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p),
        'script_lib_sprite_setup': ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
        'script_obj_hash_get_number': ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_double),
    }
    for name, (args, result) in signatures.items():
        function = getattr(native, name)
        function.argtypes, function.restype = args, result
    ply.ply_list_new.restype = ctypes.c_void_p
    state = native.script_state_new(None)
    callbacks = native.script_lib_plymouth_setup(state, getattr(request, 'param', 0), 50, None)
    policy = resolve_from_project('configs/custom_files/plymouth/themes/deb-dev-modern')
    native.script_lib_image_setup(state, str(policy).encode())
    native.script_lib_sprite_setup(state, ply.ply_list_new())

    class Engine:
        def image_directory(self, directory):
            native.script_lib_image_setup(state, str(directory).encode())

        def execute(self, source):
            operation = native.script_parse_string(source.encode(), b'theme-test')
            assert operation, 'Plymouth parser rejected script'
            assert native.script_execute(state, operation).type == 0

        def dispatch(self, event, *args):
            getattr(native, 'script_lib_plymouth_on_' + event)(state, callbacks, *args)

        def value(self, expression):
            self.execute('test_result=' + expression + ';')
            return native.script_obj_hash_get_number(ScriptState.from_address(state).globals, b'test_result')

    result = Engine()
    # Use actual image decoding and sprites; simulate only display availability.
    result.execute('''test_width=0; test_height=0;
Window.GetWidth=fun(){ return test_width; };
Window.GetHeight=fun(){ return test_height; };
Window.GetX=fun(){ return 0; }; Window.GetY=fun(){ return 0; };
''')
    result.source = render_theme_script((policy / 'deb-dev-modern.script').read_text(), load_runtime()['customization']['plymouth'])
    return result


@pytest.mark.parametrize('late_display', [False, True])
def test_theme_draws_after_display_appears_and_after_resolution_changes(engine, late_display):
    if not late_display:
        engine.execute('test_width=1024; test_height=768;')
    engine.execute(engine.source)
    engine.execute('test_width=1024; test_height=768; refresh_callback();')
    assert engine.value('wallpaper_sprite.GetImage().GetWidth()') >= 1024
    assert engine.value('wallpaper_sprite.GetImage().GetHeight()') >= 768
    assert engine.value('wallpaper_sprite.GetOpacity()') > 0
    engine.execute('test_width=1920; test_height=1080; refresh_callback();')
    assert engine.value('wallpaper_sprite.GetImage().GetWidth()') == 1920
    assert engine.value('wallpaper_sprite.GetImage().GetHeight()') == 1080
    assert engine.value('wallpaper_sprite.GetX()') == 0
    assert engine.value('wallpaper_sprite.GetY()') == 0


def test_theme_avoids_scaling_until_a_display_is_available(engine):
    engine.execute(engine.source)
    engine.execute('refresh_callback(); refresh_callback();')
    assert engine.value('wallpaper_width') == 0
    assert engine.value('wallpaper_height') == 0
    engine.execute('test_width=800; test_height=600; refresh_callback();')
    assert engine.value('wallpaper_sprite.GetImage().GetWidth()') >= 800
    assert engine.value('wallpaper_sprite.GetImage().GetHeight()') >= 600


def test_theme_shows_unlock_prompt_and_hides_it_after_unlock(engine):
    if not list(Path('/usr/lib').glob('*/plymouth/label-pango.so')):
        pytest.skip('Native text renderer required')
    engine.execute('test_width=1024; test_height=768;')
    engine.execute(engine.source)
    engine.execute('display_password_callback("Unlock disk", 4);')
    assert engine.value('prompt_sprite.GetImage().GetWidth()') > 0
    assert engine.value('entry_sprite.GetImage().GetWidth()') > 0
    assert engine.value('prompt_sprite.GetOpacity()') == 1
    assert engine.value('entry_sprite.GetOpacity()') == 1
    engine.execute('display_normal_callback();')
    assert engine.value('prompt_sprite.GetOpacity()') == 0
    assert engine.value('entry_sprite.GetOpacity()') == 0


def test_password_field_remains_visible_when_empty_and_reappears_for_another_prompt(engine):
    if not list(Path('/usr/lib').glob('*/plymouth/label-pango.so')):
        pytest.skip('Native text renderer required')
    engine.execute('test_width=1024; test_height=768;')
    engine.execute(engine.source)
    for prompt in ['Unlock root', 'Unlock swap', 'Try again']:
        engine.execute('display_password_callback("' + prompt + '", 0);')
        assert engine.value('entry_border_sprite.GetOpacity()') == 1
        assert engine.value('entry_background_sprite.GetOpacity()') == 1
        assert engine.value('entry_border_sprite.GetImage().GetWidth()') > 100
        engine.execute('display_password_callback("' + prompt + '", 4);')
        assert engine.value('entry_sprite.GetImage().GetWidth()') > 0
        engine.execute('display_normal_callback();')
        assert engine.value('entry_border_sprite.GetOpacity()') == 0
        assert engine.value('entry_hint_sprite.GetOpacity()') == 0


def test_applied_theme_asset_is_decoded_by_native_plymouth(engine, tmp_path):
    from deb_dev_builder.core.customizer import SystemCustomizer
    from deb_dev_builder.core.chroot_manager import ChrootManager
    root = tmp_path / 'target'
    chroot = ChrootManager(root, mode='real')
    SystemCustomizer(chroot, {}).apply_theme_assets(chroot)
    directory = root / load_runtime()['customization']['plymouth']['directory']
    engine.image_directory(directory)
    engine.execute('copied_image=Image("splash.png");')
    assert engine.value('copied_image.GetWidth()') > 0
    assert engine.value('copied_image.GetHeight()') > 0


def test_theme_progress_and_animation_are_active(engine):
    engine.execute('test_width=1024; test_height=768;')
    engine.execute(engine.source)
    engine.execute('progress_callback(5, 0.5); refresh_callback();')
    assert engine.value('title_sprite.GetImage().GetWidth()') > 0
    assert engine.value('fill_sprite.GetImage().GetWidth()') > 0
    assert engine.value('fill_sprite.GetOpacity()') == 1
    frame = engine.value('frame')
    engine.execute('refresh_callback();')
    assert engine.value('frame') > frame
    engine.execute('progress_callback(5, 2);')
    assert engine.value('progress') == 1


def test_render_theme_settings_is_idempotent():
    policy = load_runtime()['customization']['plymouth']
    template = resolve_from_project('configs/custom_files/plymouth/themes/deb-dev-modern/deb-dev-modern.script').read_text()
    rendered = render_theme_script(template, policy)
    assert render_theme_script(rendered, policy) == rendered


def test_media_removal_message_is_prominent_and_survives_other_events(engine):
    engine.execute('test_width=1024; test_height=768;')
    engine.execute(engine.source)
    assert engine.value('media_title_sprite.GetImage().GetWidth()') > 0
    assert engine.value('media_title_sprite.GetOpacity()') == 0
    assert engine.value('media_hint_sprite.GetOpacity()') == 0
    engine.execute('display_message_callback(theme.media_removal_message);')
    assert engine.value('media_wait') == 1
    assert engine.value('media_title_sprite.GetImage().GetWidth()') < 1024
    assert engine.value('media_title_sprite.GetOpacity()') == 1
    assert engine.value('media_hint_sprite.GetOpacity()') == 1
    assert engine.value('track_sprite.GetOpacity()') == 0
    frame = engine.value('frame')
    engine.execute('display_normal_callback(); display_message_callback("Other status"); hide_message_callback("Other status"); refresh_callback();')
    assert engine.value('frame') == frame
    assert engine.value('media_title_sprite.GetOpacity()') == 1
    engine.execute('hide_message_callback(theme.media_removal_message);')
    assert engine.value('media_wait') == 0
    assert engine.value('media_title_sprite.GetOpacity()') == 0
    assert engine.value('track_sprite.GetOpacity()') == 1


def test_media_removal_keeps_english_project_copy_with_other_locales():
    policy = load_runtime()['customization']['plymouth']
    template = resolve_from_project('configs/custom_files/plymouth/themes/deb-dev-modern/deb-dev-modern.script').read_text()
    script = render_theme_script(template, policy, locale='pt_PT.UTF-8')
    assert 'theme.media_title = "Remove the installation medium";' in script
    assert 'theme.media_hint = "Press ENTER to continue";' in script


@pytest.mark.parametrize('engine', [0, 1, 2], indirect=True)
def test_native_daemon_callbacks_dispatch_removal_notice_during_boot_shutdown_and_reboot(engine):
    engine.execute('test_width=1024; test_height=768;')
    engine.execute(engine.source)
    message = load_runtime()['customization']['plymouth']['visual']['media_removal_message'].encode()
    engine.dispatch('display_message', message)
    engine.dispatch('display_normal')
    engine.dispatch('refresh')
    assert engine.value('media_wait') == 1
    assert engine.value('media_title_sprite.GetOpacity()') == 1
    assert engine.value('media_hint_sprite.GetOpacity()') == 1
    engine.dispatch('hide_message', message)
    assert engine.value('media_wait') == 0
    assert engine.value('media_title_sprite.GetOpacity()') == 0
