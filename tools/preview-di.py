#!/usr/bin/env python3
"""Render representative d-i widgets with the real GTK2 theme parser.

Run under Xvfb or a disposable X display. This does not launch an installer,
read installation answers, or access disks. It previews theme geometry only.
"""
import argparse
import ctypes as C
import ctypes.util
import os
from pathlib import Path
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", default="800x600")
    parser.add_argument("--gtk-path", help="GTK2 engine directory, as accepted by GTK_PATH")
    args = parser.parse_args()
    width, height = map(int, args.size.split("x"))
    if width < 640 or height < 480:
        parser.error("Use at least 640x480")
    overlay = Path(__file__).resolve().parents[1] / "configs/debian-installer/includes/gtk"
    theme = overlay / "usr/share/themes/DebDevModern/gtk-2.0/gtkrc"
    if args.gtk_path:
        os.environ["GTK_PATH"] = args.gtk_path
    gtk = C.CDLL(ctypes.util.find_library("gtk-x11-2.0"))
    gdk = C.CDLL(ctypes.util.find_library("gdk-x11-2.0"))
    pixbuf = C.CDLL(ctypes.util.find_library("gdk_pixbuf-2.0"))
    pointer, integer, string = C.c_void_p, C.c_int, C.c_char_p

    def bind(library, name, result, *parameters):
        function = getattr(library, name)
        function.restype, function.argtypes = result, list(parameters)
        return function

    init = bind(gtk, "gtk_init_check", integer, pointer, pointer)
    window_new = bind(gtk, "gtk_window_new", pointer, integer)
    window_size = bind(gtk, "gtk_window_set_default_size", None, pointer, integer, integer)
    box_new = bind(gtk, "gtk_vbox_new", pointer, integer, integer)
    row_new = bind(gtk, "gtk_hbox_new", pointer, integer, integer)
    add = bind(gtk, "gtk_container_add", None, pointer, pointer)
    border = bind(gtk, "gtk_container_set_border_width", None, pointer, C.c_uint)
    pack = bind(gtk, "gtk_box_pack_start", None, pointer, pointer, integer, integer, C.c_uint)
    label_new = bind(gtk, "gtk_label_new", pointer, string)
    align = bind(gtk, "gtk_misc_set_alignment", None, pointer, C.c_float, C.c_float)
    entry_new = bind(gtk, "gtk_entry_new", pointer)
    visibility = bind(gtk, "gtk_entry_set_visibility", None, pointer, integer)
    image_new = bind(gtk, "gtk_image_new_from_file", pointer, string)
    button_new = bind(gtk, "gtk_button_new_with_label", pointer, string)
    check_new = bind(gtk, "gtk_check_button_new_with_label", pointer, string)
    progress_new = bind(gtk, "gtk_progress_bar_new", pointer)
    fraction = bind(gtk, "gtk_progress_bar_set_fraction", None, pointer, C.c_double)
    progress_text = bind(gtk, "gtk_progress_bar_set_text", None, pointer, string)
    show = bind(gtk, "gtk_widget_show_all", None, pointer)
    pending = bind(gtk, "gtk_events_pending", integer)
    iteration = bind(gtk, "gtk_main_iteration", integer)
    get_window = bind(gtk, "gtk_widget_get_window", pointer, pointer)
    get_size = bind(gtk, "gtk_window_get_size", None, pointer, C.POINTER(integer), C.POINTER(integer))
    get_map = bind(gdk, "gdk_drawable_get_colormap", pointer, pointer)
    capture = bind(gdk, "gdk_pixbuf_get_from_drawable", pointer, pointer, pointer, pointer,
                   integer, integer, integer, integer, integer, integer)
    save = bind(pixbuf, "gdk_pixbuf_save", integer, pointer, string, string, pointer)
    destroy = bind(gtk, "gtk_widget_destroy", None, pointer)

    with tempfile.TemporaryDirectory(prefix="di-preview-") as directory:
        rc = Path(directory) / "gtkrc"
        rc.write_text(f'gtk-font-name = "DejaVu Sans 9"\ninclude "{theme}"\n')
        os.environ["GTK2_RC_FILES"] = str(rc)
        if not init(None, None):
            raise SystemExit("GTK2 needs an X display; run this tool under xvfb-run")
        window = window_new(0)
        window_size(window, width, height)
        outer = box_new(0, 0)
        add(window, outer)
        banner = image_new(os.fsencode(overlay / "usr/share/graphics/logo_installer.png"))
        align(banner, 0, 0.5)
        pack(outer, banner, 0, 0, 0)
        content = box_new(0, 12)
        border(content, 18)
        pack(outer, content, 1, 1, 0)

        def label(text):
            widget = label_new(text.encode())
            align(widget, 0, 0.5)
            pack(content, widget, 0, 0, 0)

        label("Set up users and passwords")
        label("Choose a passphrase to protect your encrypted installation.")
        label("Encryption passphrase:")
        entry = entry_new()
        visibility(entry, 0)
        pack(content, entry, 0, 0, 0)
        label("Re-enter passphrase to verify:")
        entry = entry_new()
        visibility(entry, 0)
        pack(content, entry, 0, 0, 0)
        pack(content, check_new(b"Show password in clear"), 0, 0, 0)
        progress = progress_new()
        fraction(progress, 0.62)
        progress_text(progress, b"Installing the system")
        pack(content, progress, 0, 0, 0)
        pack(content, label_new(b""), 1, 1, 0)
        buttons = row_new(0, 10)
        for text in (b"Screenshot", b"Go Back", b"Continue"):
            pack(buttons, button_new(text), 1, 1, 0)
        pack(content, buttons, 0, 0, 0)
        show(window)
        for _ in range(20):
            for _ in range(100):
                if not pending():
                    break
                iteration()
            time.sleep(0.02)
        actual_width, actual_height = integer(), integer()
        get_size(window, C.byref(actual_width), C.byref(actual_height))
        drawable = get_window(window)
        picture = capture(None, drawable, get_map(drawable), 0, 0, 0, 0,
                          actual_width.value, actual_height.value)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not picture or not save(picture, os.fsencode(args.output), b"png", None, None):
            raise SystemExit("Could not save GTK2 preview")
        destroy(window)
        if (actual_width.value, actual_height.value) != (width, height):
            raise SystemExit(f"Preview exceeds requested geometry: {actual_width.value}x{actual_height.value}")
        print(args.output)


if __name__ == "__main__":
    main()
