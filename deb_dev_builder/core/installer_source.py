"""Distribution-specific d-i origins, default answers and offline init packages."""
import re
import fnmatch


def control_fields(paragraph):
    return dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))


def select_udeb_records(index, release, policy):
    pattern = re.compile(policy['kernel_udeb_pattern'])
    versions, records = set(), []
    for paragraph in index.strip().split('\n\n'):
        fields = control_fields(paragraph)
        if not fields:
            continue
        kernel = pattern.fullmatch(fields['Package'])
        if kernel:
            versions.add(kernel.group('release'))
        if kernel and release and kernel.group('release') != release:
            continue
        if not any(fnmatch.fnmatchcase(fields['Package'], value) for value in policy['exclude_udebs']):
            records.append((paragraph, fields))
    if release and release not in versions:
        raise ValueError(f'Installer kernel {release} has no matching module udebs; available kernel releases: {", ".join(sorted(versions))}')
    present = {fields['Package'] for _, fields in records}
    if not set(policy['live_modules']) <= present:
        raise ValueError('Debian Installer index is missing required live modules')
    return records


def daily_installer(config, policy):
    """Keep moving Debian installer images paired with their native udebs."""
    if config.get("installer_image_mirror") or policy["image_mirror"] or config.get("installer_mirror"):
        return None
    profile = policy.get("daily_images", {}).get(config.get("base_distro"), {})
    if config.get("suite") in profile.get("suites", []):
        return profile
    return None


def installer_udeb_suite(config, policy):
    profile = daily_installer(config, policy)
    return profile["udeb_suite"] if profile else config["suite"]


def installer_mirror(config, policy, *, images=False):
    if images:
        mirror = (config.get("installer_image_mirror") or policy["image_mirror"]
                  or config.get("installer_mirror") or config.get("mirror") or policy["mirror"])
    else:
        mirror = (config.get("installer_mirror") or policy["archive_mirror"]
                  or config.get("mirror") or policy["mirror"])
    return str(mirror).rstrip("/")


def init_preseed(config, policy):
    question = policy["init_question"]
    if not question:
        return ""
    init = config.get("init_system", "sysvinit")
    if init not in policy["init_packages"]:
        raise ValueError(f"The native installer does not support init system {init!r}")
    answer = f"d-i {question} select {init}\n"
    if policy["init_ask"]:
        answer += f"d-i {question} seen false\n"
    return answer


def default_preseed(content, config, policy):
    """Adapt automatic bundled profiles; explicitly supplied preseeds stay intact."""
    if config.get("preseed"):
        return content
    answers = [line.format(suite=config["suite"]) for line in policy["default_preseed"]]
    daily = daily_installer(config, policy)
    if daily:
        answers.extend([f'd-i mirror/suite string {config["suite"]}',
                        f'd-i mirror/udeb/suite string {daily["udeb_suite"]}'])
    init = init_preseed(config, policy)
    if init:
        answers.extend(init.splitlines())
    for answer in answers:
        question = answer.split()[1]
        pattern = re.compile(r"^\s*d-i\s+" + re.escape(question) + r"\s+.*$", re.MULTILINE)
        content = pattern.sub("", content)
    return content.rstrip() + ("\n" + "\n".join(answers) if answers else "") + "\n"


def offline_package_sets(config, policy):
    arch = config.get("dpkg_arch", config.get("architecture"))
    groups = list(policy["offline_package_sets"].get(arch, []))
    if groups and policy["init_question"]:
        init_preseed(config, policy)  # Validate before resolving the package set.
        if policy["init_ask"]:
            # Resolve alternatives separately: init implementations can conflict.
            groups.extend(policy["init_packages"].values())
        else:
            groups.append(policy["init_packages"][config.get("init_system", "sysvinit")])
    return groups
