#!/usr/bin/python3 -I
"""wardian-apt: install new system packages from the configured archives.

agent's only sudo grant is this script:

    sudo wardian-apt <package>...

It takes package names only: no options, paths, .deb files or repos. It
starts /usr/local/libexec/wardian-apt-worker as its own transient systemd
service, waits for it, then prints the worker's log and exits with its code.
The worker does the install and the firewall check. Nothing the caller does
to this process or to sudo reaches the worker once it has started.

Root owns this file. Standard library only. python3 -I ignores PYTHON*
variables and the user's site directory.
"""

import os
import re
import secrets
import subprocess
import sys
import time

NAME = re.compile(r"[a-z0-9][a-z0-9.+-]*[a-z0-9]")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"}
WORKER = "/usr/local/libexec/wardian-apt-worker"
# /run is root's, and this directory is root's alone, so agent can't read or
# replace a log or status file.
RUN_DIR = "/run/wardian-apt"


def say(message):
    """Print to stderr. agent controls stderr, so a write that fails is ignored."""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def refused_names(names):
    return [name for name in names if not NAME.fullmatch(name)]


def read_status(path):
    try:
        with open(path) as f:
            return int(f.read())
    except FileNotFoundError:
        return None


def wait_for(unit, status_file):
    """Wait for the worker. Return its exit code, or 1 if it left none."""
    while True:
        code = read_status(status_file)
        if code is not None:
            return code
        active = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            check=False,
            env=ENV,
        )
        if active.returncode != 0:
            # It may have written the file just before it stopped.
            code = read_status(status_file)
            return 1 if code is None else code
        time.sleep(0.5)


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
    os.umask(0o077)
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    unit = f"wardian-apt-{secrets.token_hex(8)}"
    log = f"{RUN_DIR}/{unit}.log"
    status_file = f"{RUN_DIR}/{unit}.status"
    # Without --wait, systemd-run returns once the service has started, and
    # the service runs on whatever happens to this process.
    started = subprocess.run(
        [
            "/usr/bin/systemd-run", "--quiet", "--collect", f"--unit={unit}",
            f"--property=StandardOutput=truncate:{log}",
            "--property=StandardError=inherit",
            "--", "/usr/bin/python3", "-I", WORKER, status_file, *argv,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        env=ENV,
    )  # fmt: skip
    if started.returncode != 0:
        say("wardian-apt: can't start the worker")
        return 1
    code = wait_for(unit, status_file)
    try:
        with open(log, errors="replace") as f:
            say(f.read().rstrip("\n"))
    except OSError:
        pass
    for path in (log, status_file):
        try:
            os.unlink(path)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
