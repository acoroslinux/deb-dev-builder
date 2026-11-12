# Desktop and window-manager catalog

The catalog contains 66 selectable profiles. The names below are the values for
`--desktop`; `python3 cli.py --list-options` lists the installed project profiles.
Profiles use distribution packages and native appearance. Standalone window
managers intentionally retain their own interaction model rather than adding a
full desktop shell. New profiles require VM validation before release.

## Desktop environments and shell sessions

`budgie`, `cinnamon`, `enlightenment`, `gnome`, `gnome-classic`, `gnome-flashback`, `kde`, `lomiri`, `lxde`, `lxqt`, `mate`, `phosh`, `sugar`, `ukui`, `xfce`.

Sugar is an educational shell; Phosh and Lomiri are mobile-oriented shells.
GNOME Classic uses the packaged GNOME session and extensions. Enlightenment
uses its native X11 session in this profile. Phosh and Lomiri are limited to
Debian: their native session stacks have not been integrated with Devuan init.
UKUI is enabled where its full package set is available in Debian; the
corresponding Devuan package set is incomplete in the checked snapshots.
Budgie selects X11/LightDM on stable and oldstable, and its native
Wayland/Labwc session with SDDM on testing and unstable. This follows the
packaged 10.10 transition instead of forcing the older session type.

## Standalone X11 window managers

`9wm`, `aewmpp`, `afterstep`, `awesome`, `blackbox`, `bspwm`, `clfswm`, `compiz`, `ctwm`, `cwm`, `dwm`, `evilwm`, `fluxbox`, `flwm`, `fvwm`, `fvwm-crystal`, `fvwm3`, `herbstluftwm`, `i3`, `icewm`, `jwm`, `kwin`, `lwm`, `marco`, `matchbox`, `metacity`, `miwm`, `muffin`, `mwm`, `notion`, `openbox`, `pekwm`, `qtile`, `ratpoison`, `sawfish`, `spectrwm`, `stumpwm`, `twm`, `ukwm`, `vtwm`, `w9wm`, `windowlab`, `wm2`, `wmaker`, `xfwm4`, `xmonad`.

`aewmpp` installs aewm++; `kwin` installs standalone KWin/X11; `matchbox` installs
the Matchbox window manager. Marco, Metacity, Muffin, UKWM and Xfwm4 profiles
start those window managers independently of their usual desktop environments.

## Additional Wayland compositors

`hyprland`, `labwc`, `sway`, `wayfire`, `weston`.

The greeter remains on X11 while the selected session uses Wayland. Labwc uses
its native startup-command interface. Wayfire receives its packaged example
configuration with a session-services entry. Its effective runtime configuration
fills absent keyboard defaults from the installed system settings, preserving
explicit user choices and the original file. Weston uses its native desktop
shell and autolaunch interface. Weston retains its native lock implementation;
its notification daemon runs through XWayland because Weston does not provide
the layer-shell protocol used by mako.

## Minimum session integration

Complete standalone X11 sessions appear as `<name> (complete session)` in the
login manager. Autologin and the installers select their `deb-dev-<name>` session
entries. These sessions provide D-Bus activation, XDG autostart, authentication
and screen-lock services, and stop their owned processes when the window
manager exits. An individual autostart failure does not abort login.

Terminal commands used by native examples are installed, including xterm and
urxvt. The bspwm and sxhkd examples are copied unchanged from the installed
package to `/etc/skel`, only when no configuration is present. Very small window
managers without an application launcher start a terminal. Native window-manager
configuration and any existing user configuration remain authoritative.
LXDE keeps its native panel and desktop startup, explicitly starts its power
manager, and uses light-locker instead of the obsolete xscreensaver startup line.

## Automated verification

The expanded amd64 matrix accepts 540 of 594 configurations across nine
distribution profiles. All accepted configurations have their declared package
names checked against official archive snapshots. Native APT simulations resolve
all 480 distinct suite/desktop combinations, including the profile exclusions
and recommended packages. Debian's `testing` profile shares the Forky snapshot
and is not counted as a second independent dependency simulation.

The automated session checks cover generated launcher syntax, native example
preservation, logout cleanup, autostart failures, keyboard defaults, and Budgie's
protocol transition. These checks do not replace boot and installation tests in
VMs. No ISO rebuild or graphical runtime validation was performed for the new
profiles during this catalog expansion.

## Suite availability

The table describes configuration eligibility, not successful graphical boot
tests. Package-index and dependency checks use amd64 repository snapshots from
2026-11-09. Moving suites may change after that date. Debian testing follows the
Forky column. A dash means the profile is rejected before building.

| Profile | Bookworm | Trixie | Forky | Sid | Daedalus | Excalibur | Freia | Ceres |
|---|---|---|---|---|---|---|---|---|
| `9wm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `aewmpp` | Yes | Yes | — | — | Yes | Yes | — | — |
| `afterstep` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `awesome` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `blackbox` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `bspwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `budgie` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `cinnamon` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `clfswm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `compiz` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `ctwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `cwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `dwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `enlightenment` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `evilwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `fluxbox` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `flwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `fvwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `fvwm-crystal` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `fvwm3` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `gnome` | Yes | Yes | Yes | Yes | Yes | Yes | — | — |
| `gnome-classic` | Yes | Yes | Yes | Yes | Yes | Yes | — | — |
| `gnome-flashback` | Yes | Yes | — | — | Yes | Yes | — | — |
| `herbstluftwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `hyprland` | — | Yes | Yes | Yes | — | Yes | Yes | Yes |
| `i3` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `icewm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `jwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `kde` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `kwin` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `labwc` | — | Yes | Yes | Yes | — | Yes | Yes | Yes |
| `lomiri` | — | Yes | Yes | Yes | — | — | — | — |
| `lwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `lxde` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `lxqt` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `marco` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `matchbox` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `mate` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `metacity` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `miwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `muffin` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `mwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `notion` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `openbox` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `pekwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `phosh` | Yes | Yes | Yes | Yes | — | — | — | — |
| `qtile` | — | Yes | Yes | Yes | — | Yes | Yes | Yes |
| `ratpoison` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `sawfish` | Yes | Yes | — | — | Yes | Yes | — | — |
| `spectrwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `stumpwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `sugar` | Yes | Yes | — | Yes | Yes | Yes | — | Yes |
| `sway` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `twm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `ukui` | Yes | — | Yes | Yes | — | — | — | — |
| `ukwm` | Yes | Yes | Yes | Yes | Yes | Yes | — | — |
| `vtwm` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `w9wm` | Yes | Yes | — | — | Yes | Yes | — | — |
| `wayfire` | — | Yes | — | Yes | — | Yes | — | Yes |
| `weston` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `windowlab` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `wm2` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `wmaker` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `xfce` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `xfwm4` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `xmonad` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |

Stable Hyprland uses the corresponding official backports archive. GNOME and
GNOME Classic remain excluded on Freia and Ceres because of the current
GDM/elogind dependency conflict. Other exclusions reflect missing packages or
unintegrated native session requirements.

Niri, River, COSMIC, Trinity, Pantheon and a full Deepin desktop stack were
not available as complete installable desktop stacks in the checked official
archives. They are not added as package-name placeholders or through external
repositories. Kiosk-only compositors and alternate builds of an existing window
manager are not separate general desktop profiles.

See [desktop validation](desktop-validation.md) for runtime evidence and the
live/installed test procedure. Package sources: [Debian package archive](https://deb.debian.org/debian/dists/)
and [Devuan merged archive](https://pkgmaster.devuan.org/merged/dists/).
