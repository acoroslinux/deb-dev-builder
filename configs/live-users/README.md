# Live-user profiles

`liveuser.json` is the default profile. Select a different account with
`--live-profile admin` or `--live-profile guest`, or select the default
explicitly with `--live-profile liveuser`.
These files are the source of the selected account's settings; the builder
never asks for a password interactively.

The bundled profiles define the username, password and groups:

| Profile | Username | Password |
|---|---|---|
| `liveuser` (default) | `liveuser` | `liveuser` |
| `admin` | `admin` | `admin` |
| `guest` | `guest` | `guest` |

To customize a profile, edit its `password` field inside `live_user`:

```json
{
  "live_user": {
    "name": "guest",
    "password": "REPLACE_WITH_YOUR_LIVE_PASSWORD",
    "groups": ["audio", "video"]
  }
}
```

Replace the example value locally. A named
profile overrides matching settings in the global configuration, and its
`groups` list replaces the default group list. The guest profile has no sudo
permissions.

Without `--live-profile`, the account settings come from `liveuser.json`;
`live_user.password` can also be overridden in the global build JSON. If a
password is removed from a profile, a real graphical build reports the missing
field instead of producing an account that cannot log in or unlock the screen.
Mock builds can use profiles without credentials.

The configured password is applied only to the named live account using
`chpasswd` on standard input. The root account stays locked. Desktop autologin
uses the display manager's separate PAM service; manual login and screen
unlocking use the configured account password.
