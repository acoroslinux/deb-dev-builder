# Installer artwork and fonts

`banner.svg` is the editable source for the GTK2 installer header. The installer
loads `logo_installer.png` and `logo_installer_dark.png` from the GTK overlay.
Both PNGs currently use the same dark header, which also keeps the native white
status text readable in high-contrast mode.

The GTK overlay also supplies the unmodified `DejaVuSans.ttf` regular face from
Debian's `fonts-dejavu-core` 2.37-8 package. The matching `fonts-dejavu-udeb`
contains only bold and oblique faces, so Fontconfig otherwise resolves a regular
DejaVu Sans request to bold. Adding the regular face restores normal body text
without changing d-i's explicit bold title markup, font size, per-language font
selection, high-contrast theme or zoom handling. This font is not added to the
text initrd.

The upstream font copyright and license notices are distributed alongside it at
`usr/share/doc/deb-dev-installer/fonts-dejavu-copyright` in the GTK overlay.
