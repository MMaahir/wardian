#!/usr/bin/python3 -I
"""wardian-apt: install new system packages from the configured archives.

agent's only sudo grant is this script:

    sudo wardian-apt <package>...

It takes package names only: no options, paths, .deb files or repos. It
installs a package only if the transaction adds packages and changes none
that are installed. Afterwards, whether the install worked or not, it checks
the firewall against the snapshot provisioning took, and powers the sandbox
off if it changed.

Root owns this file. Standard library only. python3 -I ignores PYTHON*
variables and the user's site directory.
"""

import fcntl
import hashlib
import os
import re
import subprocess
import sys

NAME = re.compile(r"[a-z0-9][a-z0-9.+-]*[a-z0-9]")
# Every apt-get call gets this environment and nothing else.
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LC_ALL": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
}
# Pattern-Only: a name is a package name, never a regular expression that
# apt-get would match against the archive when no package has that name.
APT_GET = [
    "/usr/bin/apt-get",
    "-o", "DPkg::Lock::Timeout=120",
    "-o", "APT::Cmd::Pattern-Only=true",
]  # fmt: skip
# /run is root's, so agent can't create or replace the lock file.
LOCK = "/run/wardian-apt.lock"
# The wardian_apt role writes both after provisioning.
RULESET_SNAPSHOT = "/var/lib/wardian/inet-wardian.nft"
CONFIG_HASH = "/var/lib/wardian/nftables.conf.sha256"
NFT_CONFIG = "/etc/nftables.conf"
# In `apt-get -s` output, an Inst line with the installed version in brackets
# upgrades, downgrades or reinstalls a package. Remv and Purg remove one.
CHANGES_INSTALLED = re.compile(r"Inst \S+ \[|Remv |Purg ")
RECREATE_HINT = (
    "Fetch the agent's work and recreate the sandbox to change installed packages."
)


def say(message):
    """Print to stderr. agent controls stderr, so a write that fails is ignored."""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def refused_names(names):
    return [name for name in names if not NAME.fullmatch(name)]


def changes_to_installed(simulation):
    """The lines of `apt-get -s install` output that change an installed package."""
    return [line for line in simulation.splitlines() if CHANGES_INSTALLED.match(line)]


def apt_get(*args, capture=False):
    return subprocess.run(
        [*APT_GET, *args],
        stdin=subprocess.DEVNULL,
        capture_output=capture,
        check=False,
        text=True,
        env=ENV,
    )


def firewall_unchanged():
    ruleset = subprocess.run(
        ["/usr/sbin/nft", "-s", "list", "table", "inet", "wardian"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        text=True,
        env=ENV,
    ).stdout
    with open(RULESET_SNAPSHOT) as f:
        snapshot = f.read()
    with open(NFT_CONFIG, "rb") as f:
        config_hash = hashlib.sha256(f.read()).hexdigest()
    with open(CONFIG_HASH) as f:
        expected_hash = f.read().strip()
    return (
        ruleset.rstrip("\n") == snapshot.rstrip("\n") and config_hash == expected_hash
    )


def check_firewall():
    """Power the sandbox off unless the firewall matches the snapshot.

    The poweroff comes before any output, so nothing agent does to stderr
    can stop it.
    """
    try:
        if firewall_unchanged():
            return True
        reason = "the firewall no longer matches its snapshot"
    except Exception as exc:  # noqa: BLE001 - any failure to check is a mismatch
        reason = f"can't check the firewall: {exc!r}"
    subprocess.run(
        ["/usr/bin/systemctl", "poweroff"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        env=ENV,
    )
    say(f"wardian-apt: {reason}. Powering off.")
    return False


def install(names):
    """Update, simulate, then install. Return the exit code."""
    if apt_get("update").returncode != 0:
        say("wardian-apt: apt-get update failed")
        return 1
    simulation = apt_get(
        "-s", "install", "--no-install-recommends", "--", *names, capture=True
    )
    if simulation.returncode != 0:
        say((simulation.stdout + simulation.stderr).rstrip("\n"))
        return 1
    changes = changes_to_installed(simulation.stdout)
    if changes:
        say(
            "wardian-apt: refusing: this would change installed packages:\n  "
            + "\n  ".join(changes)
            + f"\n{RECREATE_HINT}"
        )
        return 3
    return apt_get(
        "install", "-y", "--no-remove", "--no-install-recommends",
        "-o", "Dpkg::Options::=--force-confold",
        "--", *names,
    ).returncode  # fmt: skip


def main(argv):
    if os.geteuid() != 0:
        say("wardian-apt: run it with sudo")
        return 2
    if not argv:
        say("usage: sudo wardian-apt <package>...")
        return 2
    bad = refused_names(argv)
    if bad:
        say(
            "wardian-apt: refusing: not a package name: "
            + ", ".join(repr(name) for name in bad)
            + ". Pass package names only, with no options, paths or .deb files."
        )
        return 2
    os.chdir("/")
    os.umask(0o022)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        code = install(argv)
    finally:
        firewall_ok = check_firewall()
    return code if firewall_ok else 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
