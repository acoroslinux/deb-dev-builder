# Deb-Dev Modern installer branding

The branding uses native Calamares sidebar/navigation widgets and an English
QML slideshow. It does not replace installation progress, log access, keyboard
focus handling or installer dialogs.

## Active resources

- `branding.desc`: product metadata, native widget selection, colors and
  slideshow API 2.
- `mark.svg`, `welcome.svg`: scalable product identity and welcome illustration.
- `stylesheet.qss`: a light content palette, dark sidebar, visible focus states
  and native progress styling, independent of the live desktop's theme.
- `show.qml`: five slides, navigation indicators and a nine-second timer that
  runs only while the installation presentation is active.
- `FeatureSlide.qml`: shared responsive layout and local artwork.
- `slide_*.jpg`: existing JPEG artwork with matching filename extensions.

The old custom sidebar/navigation/about QML prototypes are not selected by
`branding.desc`. The native widgets provide those parts of the active UI.
`logo.jpg` preserves the previous artwork; active product identity uses SVG.

## Preview and audit

With PyQt6 and Calamares' QML module available:

```bash
python3 tools/preview-calamares.py --output-dir /tmp/calamares-slides
python3 tools/preview-calamares.py --size 600x360 --output-dir /tmp/calamares-slides-small
```

The tool can use `--qml-import-path /path/to/extracted/usr/share/calamares/qml`
without installing or launching Calamares. It renders every slide, checks text
bounds, and verifies that leaving the presentation stops its timer.

The build's installer audit checks declared branding resources, image format
signatures and locally referenced QML artwork. This complements native-module
schema validation; it does not claim a complete graphical installation test.

The common modules are overlaid first, followed by the selected `systemd` or
`non-systemd` configuration. Keep those sequences distinct. The generated
displaymanager configuration selects the build's login manager, including
Calamares' native `gdm` name for GDM3.
