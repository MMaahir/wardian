"""The wardian command: create, test and destroy a sandbox.

A sandbox is a Lima VM built from lima/wardian.yaml and provisioned with
ansible/playbook.yml. The CLI uses only the standard library. It runs limactl,
ansible-playbook and ssh with argument lists, never through a shell, and strips
control characters from everything they print.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "lima" / "wardian.yaml"
PLAYBOOK = REPO / "ansible" / "playbook.yml"
ANSIBLE_CONFIG = REPO / "ansible" / "ansible.cfg"

# Where the control_tests role installs the script in the guest.
CONTROL_TESTS = "/usr/local/lib/wardian/control_tests.py"
HOST_KEY = "/etc/ssh/ssh_host_ed25519_key.pub"
# The guest's DNS server: Lima's host agent answers on the default gateway.
# See "Resolver" in spike/phase-1-notes.md.
LIMA_RESOLVER = "192.168.5.2"
# create writes this file into the Lima instance directory. destroy deletes
# only instances that have it, and limactl delete removes it with the rest.
OWNER_MARKER = "wardian-sandbox"

NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
# C0 controls except tab and newline, DEL, and C1 controls.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Lima merges these files into every instance, with or without `base`, and
# list fields such as mounts and portForwards combine with the template's.
LIMA_GLOBAL_CONFIGS = ("default.yaml", "override.yaml", "base.yaml")

# The template's catch-all port-forward rule as it appears in Lima 2.3's merged
# config, which adds the default hostIP and hostPortRange.
CATCH_ALL_IGNORE = {
    "guestIPMustBeZero": False,
    "guestIP": "0.0.0.0",  # noqa: S104 - matches guest listeners, binds nothing
    "guestPortRange": [1, 65535],
    "hostIP": "127.0.0.1",
    "hostPortRange": [1, 65535],
    "proto": "any",
    "ignore": True,
}


class WardianError(Exception):
    """A step failed or a check refused. The message says which."""


def clean(text: str) -> str:
    return CONTROL_CHARS.sub("", text)


def sandbox_name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid sandbox name {clean(value)!r}: use 1 to 31 characters, "
            "a lowercase letter first, then lowercase letters, digits or '-'"
        )
    return value


def run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    """Run cmd and print its output line by line, cleaned."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        raise WardianError(f"can't run {cmd[0]}: {exc}") from exc
    with proc:
        for line in proc.stdout:  # never None: stdout is a pipe
            print(clean(line.decode("utf-8", "replace")), end="", flush=True)
    if proc.returncode != 0:
        raise WardianError(f"{shlex.join(cmd[:2])} exited {proc.returncode}")


def capture(cmd: list[str]) -> str:
    """Run cmd and return its stdout, uncleaned. Callers clean what they print."""
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as exc:
        raise WardianError(f"can't run {cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        stderr = clean(result.stderr.decode("utf-8", "replace")).strip()
        raise WardianError(
            f"{shlex.join(cmd[:2])} exited {result.returncode}: {stderr}"
        )
    return result.stdout.decode("utf-8", "replace")


def lima_home() -> Path:
    return Path(os.environ.get("LIMA_HOME") or Path.home() / ".lima")


def check_lima_global_config() -> None:
    found = [
        path
        for path in (lima_home() / "_config" / name for name in LIMA_GLOBAL_CONFIGS)
        if os.path.lexists(path)
    ]
    if found:
        raise WardianError(
            "refusing: Lima merges these files into every instance. Move them "
            "away first: " + ", ".join(str(path) for path in found)
        )


def config_problems(config: dict) -> list[str]:
    """Check the security settings in Lima's merged config for an instance."""
    problems = [
        f"{key} is not empty"
        for key in ("mounts", "networks", "provision")
        if config.get(key)
    ]
    ssh = config.get("ssh") or {}
    must_be_false = {
        "networkOpts.hostLoopback": (config.get("networkOpts") or {}).get(
            "hostLoopback"
        ),
        "propagateProxyEnv": config.get("propagateProxyEnv"),
        "ssh.forwardAgent": ssh.get("forwardAgent"),
        "ssh.overVsock": ssh.get("overVsock"),
    }
    problems += [
        f"{key} is {value!r}, not false"
        for key, value in must_be_false.items()
        if value is not False
    ]
    if config.get("portForwards") != [CATCH_ALL_IGNORE]:
        problems.append("portForwards is not exactly the catch-all ignore rule")
    if (config.get("user") or {}).get("name") == "agent":
        problems.append("Lima's admin user would be named agent")
    return problems


def find_instance(name: str) -> dict | None:
    for line in capture(["limactl", "list", "--json"]).splitlines():
        if line.strip():
            instance = json.loads(line)
            if instance.get("name") == name:
                return instance
    return None


def known_hosts_file(name: str) -> Path:
    return Path.home() / ".ssh" / "known_hosts.d" / name


def write_known_hosts(name: str, host_key: str) -> Path:
    fields = host_key.split()
    if (
        len(fields) < 2
        or fields[0] != "ssh-ed25519"
        or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[1])
    ):
        raise WardianError(f"unexpected host key from {name}: {clean(host_key)!r}")
    path = known_hosts_file(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The entry is keyed by HostKeyAlias, so it never matches another
    # sandbox that later uses the same 127.0.0.1 port.
    path.write_text(f"{name} ssh-ed25519 {fields[1]}\n")
    return path


def ssh_options(name: str) -> list[str]:
    """Options for SSH as Lima's admin user, with strict host-key checking."""
    return [
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ForwardAgent=no",
        # Never use an existing master connection, which skips the key check.
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"HostKeyAlias={name}",
        "-o", f"UserKnownHostsFile={known_hosts_file(name)}",
        "-o", "GlobalKnownHostsFile=/dev/null",
    ]  # fmt: skip


def connection(instance: dict) -> tuple[str, int, str, str]:
    """Return the address, port, admin user and identity file for SSH."""
    return (
        instance["sshAddress"],
        instance["sshLocalPort"],
        instance["config"]["user"]["name"],
        os.path.expanduser(instance["IdentityFile"]),
    )


def create(name: str) -> None:
    check_lima_global_config()
    run(["limactl", "create", "--tty=false", f"--name={name}", str(TEMPLATE)])

    instance = find_instance(name)
    if instance is None:
        raise WardianError(f"limactl created no instance named {name}")
    (Path(instance["dir"]) / OWNER_MARKER).touch()
    problems = config_problems(instance.get("config") or {})
    if problems:
        raise WardianError(
            f"refusing to start {name}: "
            + "; ".join(problems)
            + f". Run `wardian destroy {name}` to remove it."
        )

    run(["limactl", "start", "--tty=false", name])
    host_key = capture(["limactl", "shell", "--workdir", "/", name, "cat", HOST_KEY])
    print(f"Wrote {write_known_hosts(name, host_key)}")

    address, port, user, identity = connection(instance)
    extra_vars = {
        "ansible_host": address,
        "ansible_port": port,
        "ansible_user": user,
        "ansible_ssh_private_key_file": identity,
        "wardian_resolver": LIMA_RESOLVER,
        # Extra vars beat ANSIBLE_* settings from the environment, such as
        # ANSIBLE_HOST_KEY_CHECKING=false. ssh_args come first on the ssh
        # command line, and ssh keeps the first value of each option. They
        # replace Ansible's ControlPersist default, so no connection is reused.
        "ansible_connection": "ssh",
        "ansible_host_key_checking": True,
        "ansible_ssh_args": shlex.join(ssh_options(name)),
        # Ansible appends these after ssh_args, and -S there would still
        # override ControlPath=none. Blank them so the environment can't.
        "ansible_ssh_common_args": "",
        "ansible_ssh_extra_args": "",
        "ansible_scp_extra_args": "",
        "ansible_sftp_extra_args": "",
    }
    run(
        [
            "ansible-playbook",
            "--inventory",
            f"{name},",
            "--extra-vars",
            json.dumps(extra_vars),
            str(PLAYBOOK),
        ],
        # Use this repo's config, never an ansible.cfg in the working directory.
        env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE_CONFIG)},
    )
    print(f"Sandbox {name} is ready.")


def test(name: str) -> None:
    check_lima_global_config()
    instance = find_instance(name)
    if instance is None or instance.get("status") != "Running":
        raise WardianError(f"sandbox {name} is not running")

    address, port, user, identity = connection(instance)
    # Root runs the root-owned script, which reads the firewall's counters and
    # runs every probe as agent with an empty environment, so nothing in
    # agent's home or environment can change the result.
    cmd = [
        "ssh", *ssh_options(name),
        "-o", f"IdentityFile={identity}",
        "-p", str(port),
        f"{user}@{address}",
        "sudo", "-n", "/usr/bin/python3", "-I", CONTROL_TESTS, "--env", "lima",
    ]  # fmt: skip
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as exc:
        raise WardianError(f"can't run ssh: {exc}") from exc

    failed = result.returncode != 0
    passed = 0
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
            status = record["result"]
            threat_id = record["id"]
            description = record["description"]
            detail = record.get("detail", "")
        except (ValueError, KeyError, TypeError, AttributeError):
            print(f"unexpected output: {clean(line)}")
            failed = True
            continue
        print(clean(f"{str(status).upper():4}  {threat_id}  {description}: {detail}"))
        passed += status == "pass"
        failed |= status not in ("pass", "skip")
    stderr = clean(result.stderr.decode("utf-8", "replace")).strip()
    if stderr:
        print(stderr, file=sys.stderr)
    if failed or passed == 0:
        raise WardianError(f"control tests failed on {name}")
    print(f"All control tests passed on {name}.")


def destroy(name: str) -> None:
    instance = find_instance(name)
    if instance is not None:
        if not (Path(instance["dir"]) / OWNER_MARKER).exists():
            raise WardianError(
                f"refusing: Lima instance {name} wasn't made by wardian create"
            )
        run(["limactl", "delete", "--force", name])
    path = known_hosts_file(name)
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    print(f"Sandbox {name} is gone.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wardian", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(required=True, metavar="command")
    for func, help_text in (
        (create, "build a sandbox from the template and provision it"),
        (test, "run the control tests in a sandbox"),
        (destroy, "delete a sandbox and its known-hosts file"),
    ):
        command = commands.add_parser(func.__name__, help=help_text)
        command.add_argument("name", nargs="?", default="wardian", type=sandbox_name)
        command.set_defaults(func=func)
    args = parser.parse_args(argv)
    try:
        args.func(args.name)
    except WardianError as exc:
        print(f"wardian: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
