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
AGENTS = ("claude", "codex")
# T3 forwards to this port in the guest. sshd lets agent forward to it only.
T3_PORT = 3773
# The claude_token role installs this script. It reads the token on stdin.
INSTALL_SCRIPT = "/usr/local/sbin/wardian-install-claude-token"
# The login Keychain item that holds the token from `claude setup-token`.
KEYCHAIN_SERVICE = "wardian-claude-token"
STORE_TOKEN_HINT = (
    "Store the token from `claude setup-token` with: security "
    f'add-generic-password -U -a "$USER" -s {KEYCHAIN_SERVICE} -w'
)
CLAUDE_TOKEN = re.compile(r"sk-ant-oat[A-Za-z0-9_-]+")
# create writes this file into the Lima instance directory. destroy deletes
# only instances that have it, and limactl delete removes it with the rest.
OWNER_MARKER = "wardian-sandbox"
# The repo the project_repo role creates. agent owns it, and runs every git
# command in it, so no other user runs git under config agent can write.
GUEST_REPO = "/home/agent/project"
# sudo keeps the admin's home as the working directory, and agent can't enter it.
AS_AGENT = "cd / && sudo -n -H -u agent"
OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
DISCARD_HINT = (
    "To discard it anyway, see 'Discarding a sandbox by hand' in docs/cli.md."
)

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


def capture(cmd: list[str], stdin_data: bytes | None = None) -> str:
    """Run cmd and return its stdout, uncleaned. Callers clean what they print.

    stdin_data, if given, goes to cmd's stdin through a pipe.
    """
    stdin = {"stdin": subprocess.DEVNULL} if stdin_data is None else {}
    try:
        result = subprocess.run(
            cmd, input=stdin_data, capture_output=True, check=False, **stdin
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


def wardian_home() -> Path:
    return Path.home() / ".wardian"


def agent_key(name: str) -> Path:
    """The private half of the sandbox's key for agent."""
    return wardian_home() / name / "agent_ed25519"


def host_config() -> Path:
    return wardian_home() / "ssh_config"


def make_agent_key(name: str) -> str:
    """Generate a new key for agent and return its public half."""
    key = agent_key(name)
    wardian_home().mkdir(mode=0o700, exist_ok=True)
    key.parent.mkdir(mode=0o700, exist_ok=True)
    # create runs this only after limactl created a new instance, so any old
    # key belonged to a sandbox that no longer exists.
    for path in (key, key.with_suffix(".pub")):
        path.unlink(missing_ok=True)
    capture(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"agent@{name}",
         "-f", str(key)]
    )  # fmt: skip
    key.chmod(0o600)
    return key.with_suffix(".pub").read_text().strip()


def host_block_markers(name: str) -> tuple[str, str]:
    return f"# BEGIN wardian-{name}", f"# END wardian-{name}"


def without_host_block(text: str, name: str) -> str:
    begin, end = host_block_markers(name)
    lines, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == begin:
            skipping = True
        elif skipping and line.rstrip("\n") == end:
            skipping = False
        elif not skipping:
            lines.append(line)
    return "".join(lines)


def quoted(path: Path) -> str:
    """A path as one ssh_config argument."""
    if '"' in str(path):
        raise WardianError(f"can't write a path with a double quote: {path}")
    return f'"{path}"'


def write_host_block(name: str, address: str, port: int) -> Path:
    """Write the wardian-<name> host block T3 connects through, as agent."""
    options = ssh_options(name)
    # The admin's options, minus the ones that only suit the CLI: T3 reads
    # this file through the user's ~/.ssh/config, and may prompt.
    settings = [
        tuple(value.split("=", 1))
        for flag, value in zip(options[::2], options[1::2], strict=True)
        if flag == "-o" and not value.startswith("BatchMode=")
    ]
    lines = [
        f"Host wardian-{name}",
        f"    HostName {address}",
        f"    Port {port}",
        "    User agent",
        f"    IdentityFile {quoted(agent_key(name))}",
    ]
    for key, value in settings:
        if key == "UserKnownHostsFile":
            value = quoted(Path(value))
        lines.append(f"    {key} {value}")
    begin, end = host_block_markers(name)
    path = host_config()
    old = path.read_text() if path.exists() else ""
    kept = without_host_block(old, name)
    if kept and not kept.endswith("\n"):
        kept += "\n"
    block = "\n".join([begin, *lines, end]) + "\n"
    wardian_home().mkdir(mode=0o700, exist_ok=True)
    path.write_text(kept + block)
    path.chmod(0o600)
    return path


def remove_host_block(name: str) -> None:
    path = host_config()
    if path.exists():
        text = path.read_text()
        kept = without_host_block(text, name)
        if kept != text:
            path.write_text(kept)
            print(f"Removed the wardian-{name} host from {path}")


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


def read_claude_token() -> bytes:
    """Read the Claude token from the login Keychain, never from a file."""
    try:
        output = capture(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]
        )
    except WardianError as exc:
        raise WardianError(f"{exc}. {STORE_TOKEN_HINT}") from exc
    token = output.removesuffix("\n")
    if not CLAUDE_TOKEN.fullmatch(token):
        # Never print the item: it may be a different secret.
        raise WardianError(
            f"the Keychain item {KEYCHAIN_SERVICE} isn't a Claude token "
            f"(sk-ant-oat...). {STORE_TOKEN_HINT}"
        )
    return token.encode()


def connection(instance: dict) -> tuple[str, int, str, str]:
    """Return the address, port, admin user and identity file for SSH."""
    return (
        instance["sshAddress"],
        instance["sshLocalPort"],
        instance["config"]["user"]["name"],
        os.path.expanduser(instance["IdentityFile"]),
    )


def admin_ssh(name: str, instance: dict) -> list[str]:
    """The ssh command line, up to the remote command, as Lima's admin user."""
    address, port, user, identity = connection(instance)
    return [
        "ssh", *ssh_options(name),
        "-o", f"IdentityFile={identity}",
        "-p", str(port),
        f"{user}@{address}",
    ]  # fmt: skip


NOTICES = (
    (
        "Notice: html_render pages from the agent run in the T3 app on your "
        "Mac. While the thread is open they can make your Mac send requests to "
        "its own localhost and LAN. Wardian can't stop this; only T3 can."
    ),
    (
        "Notice: the agent's account must have no connectors linked. For "
        "Codex, the ChatGPT account must also have no Codex Cloud environments "
        "or tasks. Wardian can't check either."
    ),
)


def create(name: str, agent: str) -> None:
    check_lima_global_config()
    # Read first, so a missing token stops create before it builds a VM.
    token = read_claude_token() if agent == "claude" else None
    run(["limactl", "create", "--tty=false", f"--name={name}", str(TEMPLATE)])

    instance = find_instance(name)
    if instance is None:
        raise WardianError(f"limactl created no instance named {name}")
    (Path(instance["dir"]) / OWNER_MARKER).touch()
    problems = config_problems(instance.get("config") or {})
    if problems:
        # It never booted, so it holds no work.
        run(["limactl", "delete", "--force", name])
        raise WardianError(
            f"refused to start {name}, and deleted it: " + "; ".join(problems)
        )

    agent_public_key = make_agent_key(name)
    print(f"Wrote {agent_key(name)}")
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
        "wardian_agent_mode": agent,
        "wardian_agent_key": agent_public_key,
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
    if token is not None:
        # Over ssh's stdin, so the token is never in a file or argument on the Mac.
        capture([*admin_ssh(name, instance), "sudo", "-n", INSTALL_SCRIPT], token)
        print(f"Installed the Claude token in {name}.")
    print(f"Wrote the wardian-{name} host to {write_host_block(name, address, port)}")
    print("For T3 and ssh to find it, add this line at the top of ~/.ssh/config:")
    print("    Include ~/.wardian/ssh_config")
    if token is None:
        print(
            f"Then log Codex in with `ssh -t wardian-{name} codex login "
            "--device-auth` and approve the code in a browser."
        )
    print()
    for notice in NOTICES:
        print(notice)
    print()
    print(f"Sandbox {name} is ready for {agent}.")


def test(name: str) -> None:
    check_lima_global_config()
    instance = find_instance(name)
    if instance is None or instance.get("status") != "Running":
        raise WardianError(f"sandbox {name} is not running")

    # Root runs the root-owned script, which reads the firewall's counters and
    # runs every probe as agent with an empty environment, so nothing in
    # agent's home or environment can change the result.
    cmd = [
        *admin_ssh(name, instance),
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


def on_mac(repo: Path, oid: str) -> bool:
    """Whether a ref in the Mac repo contains the commit oid."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "--count=1", "--contains", oid],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise WardianError(f"can't run git: {exc}") from exc
    # git exits non-zero for a commit the repo doesn't have.
    return result.returncode == 0 and bool(result.stdout.strip())


def unfetched_work(name: str, instance: dict, repo: Path) -> list[str]:
    """List the work in the sandbox's repo that deleting it would lose."""
    ssh = admin_ssh(name, instance)

    def guest_git(*args: str) -> list[str]:
        # ssh hands the command to the admin's shell, so quote the arguments.
        command = f"{AS_AGENT} git -C {GUEST_REPO} {shlex.join(args)}"
        return capture([*ssh, command]).splitlines()

    problems = []
    changed = untracked = 0
    head = ""
    tips = {}
    # The flags beat any status or .gitmodules setting that would hide changes.
    status = guest_git(
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    for line in status:
        if line.startswith("# branch.oid "):
            head = line.removeprefix("# branch.oid ")
        elif line == "# branch.head (detached)":
            # Commits on a detached HEAD are on no branch.
            tips["detached HEAD"] = head
        elif line.startswith(("1 ", "2 ", "u ")):
            changed += 1
        elif line.startswith("? "):
            untracked += 1
    if changed:
        problems.append(f"uncommitted changes in {changed} files")
    if untracked:
        problems.append(f"{untracked} untracked files")

    refs = guest_git(
        "for-each-ref",
        "--format=%(objectname) %(refname)",
        "refs/heads",
        "refs/tags",
        "refs/stash",
    )
    for line in refs:
        oid, _, ref = line.partition(" ")
        if ref == "refs/stash":
            problems.append("a stash")
        elif ref.startswith("refs/tags/"):
            problems.append(f"tag {ref.removeprefix('refs/tags/')}")
        else:
            tips[f"branch {ref.removeprefix('refs/heads/')}"] = oid

    try:
        if tips:
            capture(["git", "-C", str(repo), "rev-parse", "--git-dir"])
    except WardianError as exc:
        # Keep the reasons already found, so they aren't hidden by this one.
        problems.append(f"can't read the Mac repo: {exc}")
        tips = {}
    for label, oid in tips.items():
        if not OID.fullmatch(oid):
            raise WardianError(f"unexpected object name for {label}: {oid!r}")
        if not on_mac(repo, oid):
            problems.append(f"{label} at {oid[:12]} isn't in {repo}")
    return [clean(problem) for problem in problems]


def destroy(name: str, repo: Path) -> None:
    instance = find_instance(name)
    if instance is not None:
        if not (Path(instance["dir"]) / OWNER_MARKER).exists():
            raise WardianError(
                f"refusing: Lima instance {name} wasn't made by wardian create"
            )
        status = instance.get("status")
        if status != "Running":
            raise WardianError(
                clean(
                    f"refusing: {name} is {status}, so wardian can't check it "
                    f"for unfetched work. Start it with `limactl start {name}`. "
                    + DISCARD_HINT
                )
            )
        try:
            problems = unfetched_work(name, instance, repo)
        except WardianError as exc:
            raise WardianError(
                f"can't check {name} for unfetched work: {exc}. {DISCARD_HINT}"
            ) from exc
        if problems:
            raise WardianError(
                f"refusing: {name} has work that isn't fetched: " + "; ".join(problems)
            )
        run(["limactl", "delete", "--force", name])
    path = known_hosts_file(name)
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    key = agent_key(name)
    if key.parent.exists():
        for child in (key, key.with_suffix(".pub")):
            child.unlink(missing_ok=True)
        key.parent.rmdir()
        print(f"Removed {key.parent}")
    remove_host_block(name)
    print(f"Sandbox {name} is gone.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wardian", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(required=True, metavar="command")
    for func, help_text in (
        (create, "build a sandbox from the template and provision it"),
        (test, "run the control tests in a sandbox"),
        (destroy, "delete a fully fetched sandbox, its known-hosts file and key"),
    ):
        command = commands.add_parser(func.__name__, help=help_text)
        command.add_argument("name", nargs="?", default="wardian", type=sandbox_name)
        command.set_defaults(func=func)
        if func is create:
            command.add_argument(
                "--agent",
                required=True,
                choices=AGENTS,
                help="the one agent whose login the sandbox gets",
            )
        if func is destroy:
            command.add_argument(
                "--repo",
                type=Path,
                default=Path.cwd(),
                help="the Mac repo the sandbox's branches must be in "
                "(default: the current directory)",
            )
    kwargs = vars(parser.parse_args(argv))
    func = kwargs.pop("func")
    try:
        func(**kwargs)
    except WardianError as exc:
        print(f"wardian: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
