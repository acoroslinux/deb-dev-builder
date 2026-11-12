"""Explicit early-unlock selection and validation of generated crypto tables."""
import shlex

from .installer_lifecycle import rooted, target_header
from .initrd_editor import archive_path


def keyfile_unlock_script(storage):
    """Prepare native Calamares keys for Debian's initramfs-tools backend.

    Never put an unlock key on unencrypted boot media. Native Calamares creates
    and enrolls the random key; this script checks enrollment before enabling
    the distribution's documented KEYFILE_PATTERN integration.
    """
    policy = storage['keyfile_unlock']
    key = policy['key_file']
    if not key.startswith('/') or archive_path(key.lstrip('/')) == '.':
        raise ValueError('Unlock key must name an absolute target file')
    lines = [*target_header(), 'umask 077',
             'keyfile=' + rooted(key.lstrip('/')),
             'table=' + rooted(storage['crypttab']),
             '[ -f "$keyfile" ] || exit 0',
             '[ ! -L "$keyfile" ] || { echo "Unlock key must not be a symlink" >&2; exit 1; }',
             "awk -v key=" + shlex.quote(key) + " '$0 !~ /^[[:space:]]*#/ && $3 == key { found=1 } END { exit !found }' \"$table\" || exit 0",
             'boot_source=$(' + shlex.join(policy['boot_source_command']) + ' "$target/boot")',
             'boot_source=${boot_source%%\\[*}',
             shlex.join(policy['ancestor_types_command']) + ' "$boot_source" | grep -Fxq crypt || { echo "Refusing to embed an unlock key on unencrypted boot storage" >&2; exit 1; }',
             '[ "$(stat -c %u "$keyfile")" = "$(id -u)" ] || { echo "Unexpected unlock-key owner" >&2; exit 1; }',
             'chmod ' + shlex.quote(policy['mode']) + ' "$keyfile"',
             "awk -v key=" + shlex.quote(key) + " '$0 !~ /^[[:space:]]*#/ && $3 == key { print $1, $2 }' \"$table\" |",
             'while read -r name source; do',
             '    case "$source" in',
             '        UUID=*|LABEL=*|PARTUUID=*|PARTLABEL=*) device=$(' + shlex.join(policy['resolve_source_command']) + ' "$source") ;;',
             '        /dev/*) device=$source ;;',
             '        *) echo "Unsupported encrypted-volume source" >&2; exit 1 ;;',
             '    esac',
             '    ' + shlex.join(policy['verify_key_command']) + ' --key-file "$keyfile" "$device" || { echo "Unlock key was not enrolled for $name" >&2; exit 1; }',
             'done',
             'pending=$(mktemp "$table.XXXXXX")',
             "awk -v key=" + shlex.quote(key) + ' -v early=' + shlex.quote(storage['early_unlock_option']) + r''' '
                 $0 !~ /^[[:space:]]*#/ && $3 == key && $4 !~ /(^|,)noauto(,|$)/ {
                     n=split($4, options, ","); found=0
                     for (i=1; i<=n; i++) if (options[i] == early) found=1
                     if (!found) $4=($4 == "" ? early : $4 "," early)
                 }
                 { print }
             ' "$table" > "$pending"''',
             'chmod 0644 "$pending"; mv -f -- "$pending" "$table"',
             'conf=' + rooted(storage['cryptsetup_config']['path']),
             'pattern=' + shlex.quote('KEYFILE_PATTERN="' + key + '"'),
             'mkdir -p "$(dirname "$conf")"',
             'if grep -Eq "^[[:space:]]*KEYFILE_PATTERN=" "$conf" 2>/dev/null; then',
             '    grep -Fqx -- "$pattern" "$conf" || { echo "Conflicting initramfs keyfile pattern" >&2; exit 1; }',
             'else', r'    printf "\n%s\n" "$pattern" >> "$conf"', 'fi',
             'mask=' + rooted(policy['initramfs_config']),
             'mkdir -p "$(dirname "$mask")"',
             r"printf '%s\n' " + shlex.quote('UMASK=' + policy['umask']) + ' > "$mask"',
             'chmod 0600 "$mask"',
             ]
    return ('\n'.join(lines) + '\n').encode()


def validate_keyfile_content(storage):
    """Verify embedded keys and restrictive image modes, without printing keys."""
    policy = storage['keyfile_unlock']
    key = policy['key_file']
    archive_path(policy['embedded_directory'])
    return [
        '    if awk -v key=' + shlex.quote(key) + " '$0 !~ /^[[:space:]]*#/ && $3 == key { found=1 } END { exit !found }' " + rooted(storage['crypttab']) + ' 2>/dev/null; then',
        '    (',
        '        [ ! -L "$image" ] || { echo "Initramfs containing unlock keys must not be a symlink" >&2; exit 1; }',
        '        [ "$(stat -c %u "$image")" = "$(id -u)" ] || { echo "Unexpected initramfs owner" >&2; exit 1; }',
        '        case "$(stat -c %a "$image")" in 400|600) ;; *) echo "Initramfs containing unlock keys must be owner-readable only" >&2; exit 1 ;; esac',
        '        scratch=$(mktemp -d "$target/tmp/deb-dev-keyfile.XXXXXX")',
        '        trap \'rm -rf -- "$scratch"\' 0',
        '        guest_scratch="/${scratch#"$target"/}"',
        '        chroot "$target" ' + shlex.join(storage['initramfs_extract']) + ' "$guest_image" "$guest_scratch"',
        '        content=$scratch',
        '        [ ! -d "$scratch/main" ] || content=$scratch/main',
        '        embedded="$content"/' + shlex.quote(storage['crypttab_initramfs_path']),
        '        awk -v key=' + shlex.quote(key) + " '$0 !~ /^[[:space:]]*#/ && $3 == key && $4 !~ /(^|,)noauto(,|$)/ { print $1, $2 }' " + rooted(storage['crypttab']) + ' |',
        '        while read -r name source; do',
        '''            encoded=$(awk -v name="$name" -v source="$source" '$1 == name && $2 == source { print $3; exit }' "$embedded")''',
        '            case "$encoded" in /' + policy['embedded_directory'] + '/*) ;; *) echo "Missing embedded unlock key for $name" >&2; exit 1 ;; esac',
        '            case "$encoded" in *..*) echo "Invalid embedded key path" >&2; exit 1 ;; esac',
        '            cmp -s -- ' + rooted(key.lstrip('/')) + ' "$content$encoded" || { echo "Embedded unlock key does not match for $name" >&2; exit 1; }',
        '        done',
        '    )', '    fi',
    ]


def select_early_unlock(storage):
    """Select boot-critical crypto ancestors and native early-unlock entries.

    Relying solely on chroot mount/device discovery can produce an empty
    cryptroot/crypttab even though the installed crypttab is populated.
    Preserve comments, keys and all existing options; never invent devices.
    """
    program = r'''
        BEGIN {
            n=split(boot_names, names, "\n")
            for (i=1; i<=n; i++) if (names[i] != "") required[names[i]]=1
        }
        /^[[:space:]]*#/ || NF < 4 { print; next }
        {
            n=split($4, options, ","); selected=($1 in required); present=0; disabled=0
            for (i=1; i<=n; i++) {
                if (options[i] == early) present=1
                if (options[i] == "noauto") disabled=1
                if (index("," triggers ",", "," options[i] ",")) selected=1
            }
            if ($1 in required) {
                seen[$1]=1
                if (disabled) failed=1
            }
            if (selected && !disabled && !present) $4=$4 "," early
            print
        }
        END {
            for (name in required) if (!(name in seen)) failed=1
            if (failed) {
                print "Boot-critical encrypted volume is missing or disabled in crypttab" > "/dev/stderr"
                exit 1
            }
        }
    '''
    return [
        'crypto_table=' + rooted(storage["crypttab"]),
        'if [ -f "$crypto_table" ]; then',
        '    boot_crypto_names=',
        '    if [ -f ' + rooted(storage['fstab']) + ' ]; then',
        '        boot_crypto_names=$(',
        '''            awk '$0 !~ /^[[:space:]]*#/ && ($2 == "/" || $2 == "/usr" || $3 == "swap") { print $1 }' ''' + rooted(storage['fstab']) + ' |',
        '            while read -r source; do',
        '                case "$source" in',
        '                    /dev/*) device=$source ;;',
        '                    UUID=*|LABEL=*|PARTUUID=*|PARTLABEL=*) device=$(chroot "$target" ' + shlex.join(storage['early_unlock_resolve_command']) + ' "$source") || exit 1 ;;',
        '                    *) continue ;;',
        '                esac',
        '                ancestors=$(chroot "$target" ' + shlex.join(storage['early_unlock_ancestor_command']) + ' "$device") || exit 1',
        r'''                printf '%s\n' "$ancestors" | awk '$1 == "crypt" { print $2 }' ''',
        '            done',
        '        )',
        '    fi',
        '    pending=$(mktemp "$crypto_table.XXXXXX")',
        '    awk ' + shlex.join(['-v', 'early=' + storage['early_unlock_option'],
                                '-v', 'triggers=' + ','.join(storage['early_unlock_options'])]) + ' -v boot_names="$boot_crypto_names" ' + shlex.quote(program) + ' "$crypto_table" > "$pending" || { rm -f -- "$pending"; exit 1; }',
        '    chmod 0644 "$pending"',
        '    mv -f -- "$pending" "$crypto_table"',
        'fi',
    ]


def validate_crypto_content(storage):
    """Check real archive content, not merely a directory listing entry."""
    return [
        # Secondary noauto/data-only volumes need not be in the initramfs.
        '    if awk -v early=' + shlex.quote(storage['early_unlock_option']) + r''' '
            $0 !~ /^[[:space:]]*#/ && NF >= 4 {
                n=split($4, options, ",")
                for (i=1; i<=n; i++) if (options[i] == early) found=1
            }
            END { exit !found }
        ' ''' + rooted(storage['crypttab']) + '; then',
        '    (',
        # Use the target's /tmp so unmkinitramfs can execute in its chroot.
        '        scratch=$(mktemp -d "$target/tmp/deb-dev-initrd.XXXXXX")',
        '        trap \'rm -rf -- "$scratch"\' 0',
        '        guest_scratch="/${scratch#"$target"/}"',
        '        chroot "$target" ' + shlex.join(storage['initramfs_extract']) + ' "$guest_image" "$guest_scratch"',
        '        embedded="$scratch"/' + shlex.quote(storage['crypttab_initramfs_path']),
        '        [ -f "$embedded" ] || embedded="$scratch/main"/' + shlex.quote(storage['crypttab_initramfs_path']),
        # A file can exist in an initrd while containing no unlock entries.
        '        awk \'!/^[[:space:]]*#/ && NF >= 3 { found=1 } END { exit !found }\' "$embedded" || { echo "Installed initramfs has an empty crypto table" >&2; exit 1; }',
        '        awk -v early=' + shlex.quote(storage['early_unlock_option']) + r''' '
            FNR == NR {
                if ($0 !~ /^[[:space:]]*#/ && NF >= 3) actual[$1]=$2
                next
            }
            $0 !~ /^[[:space:]]*#/ && NF >= 4 {
                n=split($4, options, ",")
                for (i=1; i<=n; i++) if (options[i] == early && actual[$1] != $2) {
                    print "Missing encrypted target in initramfs: " $1 > "/dev/stderr"
                    failed=1
                }
            }
            END { exit failed }
        ' "$embedded" ''' + rooted(storage['crypttab']),
        '    )',
        '    fi',
    ]
