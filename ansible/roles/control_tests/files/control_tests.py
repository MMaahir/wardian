"""Wardian control tests.

Each threat ID in THREAT_IDS has a description, the environment it must run
in (ci for the Incus container, lima for a Lima sandbox, or both) and one test.
The test checks the control from the outside, the way an attacker would try it.

The playbook installs this file root-owned, and root runs it:

    /usr/bin/python3 -I control_tests.py --env lima

Root sets up the fixtures, reads the firewall's counters, and runs the probes
that must fail for root too. Every other probe runs as agent with an empty
environment, so nothing agent can write changes the result:

    runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin <probe>

It prints one JSON object per threat ID and exits 1 if any test fails. A
threat ID that applies to the environment but has no test counts as a failure,
so deleting a test fails the run. --only runs the named IDs and skips the
rest; the mutation job uses it. Standard library only. Network probes use
`curl -q`, which reads no config file.
"""

import argparse
import grp
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Every threat ID is mandatory in its environment.
THREAT_IDS = {
    "T01": ("both", "agent's only sudo grant is wardian-apt"),
    "T06": ("both", "nftables drops private IPv4 and IPv6 for agent and root"),
    "T07": ("both", "agent can't write the agent CLIs, their config or the token"),
    "T09": ("both", "agent's environment has no proxy, cloud or AI API variables"),
    "T10": ("both", "other users can't read the Claude token"),
    "T11": ("both", "agent downloads from a public package registry directly"),
    "T12": ("both", "wardian-apt installs new packages and refuses the rest"),
    "T13": ("ci", "wardian-apt refuses to upgrade an installed package"),
}
for _threat_id, (_env, _) in THREAT_IDS.items():
    if _env not in ("ci", "lima", "both"):
        raise ValueError(f"{_threat_id}: unknown environment {_env!r}")

TESTS = {}

PATH = "PATH=/usr/bin:/bin"
AS_AGENT = [
    "/usr/sbin/runuser", "-u", "agent", "--",
    "/usr/bin/env", "-i", "HOME=/home/agent", PATH,
]  # fmt: skip
# dpkg needs the sbin directories.
AS_ROOT = ["/usr/bin/env", "-i", "HOME=/root", "PATH=/usr/sbin:/usr/bin:/sbin:/bin"]
WRAPPER = "/usr/local/sbin/wardian-apt"
# T06's fixture: a listener in its own network namespace, behind a veth pair,
# so traffic to it leaves through the output chain like traffic to the Mac.
FIXTURE_NS = "wardian-t06"
FIXTURE_LINK = "wardian-t06"
FIXTURE_PORT = 8080
FIXTURE_HOST4, FIXTURE_PEER4 = "172.31.255.1", "172.31.255.2"
FIXTURE_HOST6, FIXTURE_PEER6 = "fd77:6172:6469::1", "fd77:6172:6469::2"
FIXTURE_NAME = "wardian-fixture.test"
FIXTURE_SERVER = f"""
import socket
server = socket.create_server(
    ("::", {FIXTURE_PORT}), family=socket.AF_INET6, dualstack_ipv6=True
)
print("ready", flush=True)
while True:
    conn, _ = server.accept()
    conn.sendall(b"HTTP/1.0 200 OK\\r\\nContent-Length: 0\\r\\n\\r\\n")
    conn.close()
"""
# T11: a package index any project might download from.
REGISTRY_URL = "https://pypi.org/simple/pip/"
TOKEN = "/etc/wardian/claude-token"  # noqa: S105 - a path, not a token
# What agent must not change: the agent CLIs, their managed config and
# instructions, the token, wardian-apt and its firewall snapshot, and the
# files that set agent's environment. Each tree is checked to every depth,
# each path on its own.
PROTECTED_TREES = (
    "/opt/codex",
    "/etc/claude-code",
    "/etc/codex",
    "/etc/wardian",
    "/var/lib/wardian",
)
PROTECTED_PATHS = (
    "/usr/bin",
    "/usr/bin/claude",
    "/usr/local/bin",
    "/usr/local/bin/codex",
    "/etc/claude-code/managed-settings.json",
    "/etc/claude-code/CLAUDE.md",
    "/etc/codex/managed_config.toml",
    "/etc/codex/requirements.toml",
    "/usr/local/sbin",
    "/usr/local/sbin/wardian-install-claude-token",
    WRAPPER,
    "/etc/sudoers.d",
    "/etc/profile.d",
    "/etc/profile.d/wardian-claude-token.sh",
    "/etc/profile.d/wardian-sandbox.sh",
    "/etc/environment",
    TOKEN,
)
# AWS, Google (including Gemini), Cloud SDK, Anthropic and OpenAI API variables.
# CLAUDE_CODE_OAUTH_TOKEN is allowed: it is the sandbox's own Claude token.
FORBIDDEN_ENV_PREFIXES = (
    "AWS_",
    "GOOGLE_",
    "GEMINI_",
    "GCLOUD_",
    "CLOUDSDK_",
    "ANTHROPIC_",
    "OPENAI_",
)
# Matched without case, since curl and others read both spellings.
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy")


def control(threat_id):
    if threat_id not in THREAT_IDS:
        raise ValueError(f"{threat_id}: not listed in THREAT_IDS")

    def register(func):
        TESTS[threat_id] = func
        return func

    return register


def run(argv, timeout=60, check=False):
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        # sudo keeps the admin's working directory, which agent can't enter.
        cwd="/",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} exited {result.returncode}: {result.stderr.strip()}"
        )
    return result


def as_agent(*argv, timeout=60):
    return run([*AS_AGENT, *argv], timeout=timeout)


def as_user(user, *argv, timeout=30):
    return run(
        ["/usr/sbin/runuser", "-u", user, "--", "/usr/bin/env", "-i", PATH, *argv],
        timeout=timeout,
    )


def curl_argv(url, *options):
    """curl that prints the HTTP status and fails fast on a dropped connection."""
    return [
        "curl", "-q", "--silent", "--show-error", "--globoff",
        "--output", "/dev/null", "--connect-timeout", "3", "--max-time", "20",
        "--write-out", "%{http_code}", *options, url,
    ]  # fmt: skip


def counter(name):
    result = run(
        ["/usr/sbin/nft", "--json", "list", "counter", "inet", "wardian", name],
        timeout=30,
        check=True,
    )
    for item in json.loads(result.stdout)["nftables"]:
        if "counter" in item:
            return item["counter"]["packets"]
    raise ValueError(f"nft printed no counter named {name}")


@control("T01")
def agent_sudo_is_only_wardian_apt():
    # `sudo -n true` would miss a grant that leaves out `true`. `sudo -l`
    # lists every grant, and the one line after the header must be exactly
    # the wrapper, as root, with no password.
    result = as_agent("sudo", "-n", "-l", timeout=30)
    output = (result.stdout + result.stderr).strip()
    _, header, grants = result.stdout.partition("may run the following commands on")
    lines = [line.strip() for line in grants.splitlines()[1:] if line.strip()]
    expected = [f"(root) NOPASSWD: {WRAPPER}"]
    if result.returncode == 0 and header and lines == expected:
        return True, f"agent may run only: {lines[0]}"
    return False, f"sudo -n -l exited {result.returncode}: {output}"


@contextmanager
def fixture():
    """Start T06's listener behind a veth pair. Yield a hosts file for its name."""
    ip = "/usr/sbin/ip"

    def cleanup():
        # Deleting the namespace deletes the veth pair with it.
        run([ip, "netns", "delete", FIXTURE_NS])
        run([ip, "link", "delete", FIXTURE_LINK])

    cleanup()
    server = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            peer = f"{FIXTURE_LINK}p"
            for argv in (
                [ip, "netns", "add", FIXTURE_NS],
                [ip, "link", "add", FIXTURE_LINK, "type", "veth", "peer", "name", peer],
                [ip, "link", "set", peer, "netns", FIXTURE_NS],
                [ip, "addr", "add", f"{FIXTURE_HOST4}/30", "dev", FIXTURE_LINK],
                [ip, "-6", "addr", "add", f"{FIXTURE_HOST6}/64", "dev", FIXTURE_LINK, "nodad"],
                [ip, "link", "set", FIXTURE_LINK, "up"],
                [ip, "-n", FIXTURE_NS, "addr", "add", f"{FIXTURE_PEER4}/30", "dev", peer],
                [ip, "-n", FIXTURE_NS, "-6", "addr", "add", f"{FIXTURE_PEER6}/64", "dev", peer, "nodad"],
                [ip, "-n", FIXTURE_NS, "link", "set", peer, "up"],
                # The self-check below connects to the namespace's own address.
                [ip, "-n", FIXTURE_NS, "link", "set", "lo", "up"],
            ):  # fmt: skip
                run(argv, check=True)
            server = subprocess.Popen(
                [ip, "netns", "exec", FIXTURE_NS, "/usr/bin/python3", "-I", "-c", FIXTURE_SERVER],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd="/",
            )  # fmt: skip
            if server.stdout.readline().strip() != "ready":
                raise RuntimeError("the fixture listener didn't start")
            # The listener answers inside its namespace, where no ruleset runs.
            for address in (FIXTURE_PEER4, f"[{FIXTURE_PEER6}]"):
                url = f"http://{address}:{FIXTURE_PORT}/"
                result = run(
                    [ip, "netns", "exec", FIXTURE_NS, *AS_ROOT, *curl_argv(url)]
                )
                if result.returncode != 0 or result.stdout != "200":
                    raise RuntimeError(
                        f"the fixture doesn't answer at {url}: {result.stderr}"
                    )
            hosts = Path(tmp) / "hosts"
            hosts.write_text(f"{FIXTURE_PEER4} {FIXTURE_NAME}\n")
            hosts.chmod(0o644)
            yield hosts
        finally:
            if server is not None:
                server.kill()
                server.wait()
            cleanup()


def with_hosts(hosts, argv):
    """Run argv in a private mount namespace whose /etc/hosts is the hosts file."""
    return [
        "/usr/bin/unshare", "--mount", "--propagation", "private", "--",
        "/bin/sh", "-ec", '/usr/bin/mount --bind "$1" /etc/hosts; shift; exec "$@"',
        "sh", str(hosts), *argv,
    ]  # fmt: skip


@control("T06")
def nftables_drops_private_addresses():
    failures = []
    details = []
    with fixture() as hosts:
        targets = (
            (f"http://{FIXTURE_PEER4}:{FIXTURE_PORT}/", "private4_dropped", False),
            (f"http://[{FIXTURE_PEER6}]:{FIXTURE_PORT}/", "ipv6_dropped", False),
            (f"http://{FIXTURE_NAME}:{FIXTURE_PORT}/", "private4_dropped", True),
        )
        for user, prefix in (("agent", AS_AGENT), ("root", AS_ROOT)):
            for url, counter_name, by_name in targets:
                argv = [*prefix, *curl_argv(url)]
                if by_name:
                    argv = with_hosts(hosts, argv)
                before = counter(counter_name)
                result = run(argv)
                after = counter(counter_name)
                if result.returncode == 0:
                    failures.append(f"{user} reached {url}")
                elif after <= before:
                    failures.append(
                        f"{user} to {url}: curl exited {result.returncode} but "
                        f"{counter_name} stayed at {before}: {result.stderr.strip()}"
                    )
                else:
                    details.append(f"{user} to {url}: dropped")
    if failures:
        return False, "; ".join(failures)
    return True, "; ".join(details)


@control("T07")
def agent_cannot_write_clis_or_token():
    # exists follows the codex symlink, so a dangling link counts as missing.
    missing = [p for p in (*PROTECTED_TREES, *PROTECTED_PATHS) if not os.path.exists(p)]
    if missing:
        return False, "missing: " + ", ".join(missing)
    # -writable asks access(2), so the probe writes nothing. find follows the
    # codex symlink for this test.
    failures = []
    for probe in (
        ("find", *PROTECTED_TREES, "-writable"),
        ("find", *PROTECTED_PATHS, "-maxdepth", "0", "-writable"),
    ):
        result = as_agent(*probe)
        if result.returncode != 0 or result.stdout.strip():
            failures.append(
                f"find exited {result.returncode}, writable: "
                f"{' '.join(result.stdout.split())} {result.stderr.strip()}"
            )
    if failures:
        return False, "; ".join(failures)
    count = len(PROTECTED_TREES) + len(PROTECTED_PATHS)
    return True, f"none of {count} paths is writable by agent"


def variables(output):
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


@control("T09")
def agent_environment_has_no_api_variables():
    # A login goes through PAM, which reads /etc/environment, then the login
    # shell reads /etc/profile. su --login runs PAM like an SSH login and then
    # runs env as the "shell". bash reads /etc/profile but not agent's own
    # dotfiles. Each probe must find WARDIAN_SANDBOX=1, which both files set,
    # so a probe that read nothing can't pass.
    pam = run(["/usr/bin/su", "--login", "--shell", "/usr/bin/env", "agent"])
    profile = as_agent(
        "bash", "--noprofile", "--norc", "-c", ". /etc/profile && exec env"
    )
    failures = []
    names = set()
    for label, result, expected in (
        ("su --login", pam, ("WARDIAN_SANDBOX",)),
        ("/etc/profile", profile, ("WARDIAN_SANDBOX", "CLAUDE_CODE_OAUTH_TOKEN")),
    ):
        found = variables(result.stdout)
        names |= found.keys()
        if found.get("WARDIAN_SANDBOX") != "1":
            failures.append(f"{label} has no WARDIAN_SANDBOX=1")
        missing = [name for name in expected if name not in found]
        if result.returncode != 0 or missing:
            failures.append(
                f"{label} exited {result.returncode} without {', '.join(missing)}: "
                f"{result.stderr.strip()}"
            )
    forbidden = sorted(
        n
        for n in names
        if n.startswith(FORBIDDEN_ENV_PREFIXES) or n.lower() in PROXY_VARIABLES
    )
    if forbidden:
        failures.append("forbidden variables: " + ", ".join(forbidden))
    if failures:
        return False, "; ".join(failures)
    return True, f"{len(names)} variables, none forbidden"


@control("T10")
def others_cannot_read_token():
    info = os.stat(TOKEN)
    group = grp.getgrgid(info.st_gid)
    failures = []
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != 0 or group.gr_name != "agent" or mode != 0o640:
        failures.append(
            f"{TOKEN} is {pwd.getpwuid(info.st_uid).pw_name}:{group.gr_name} {mode:04o}, "
            "not root:agent 0640"
        )
    # Anyone in the agent group could read it. Only agent may be.
    members = set(group.gr_mem) | {
        user.pw_name for user in pwd.getpwall() if user.pw_gid == info.st_gid
    }
    if members != {"agent"}:
        failures.append("agent group members: " + ", ".join(sorted(members)))
    result = as_user("nobody", "cat", TOKEN)
    if result.returncode == 0 or "Permission denied" not in result.stderr:
        failures.append(
            f"cat as nobody exited {result.returncode}: {result.stderr.strip()}"
        )
    if failures:
        return False, "; ".join(failures)
    return True, f"{TOKEN} is root:agent 0640; nobody: Permission denied"


@control("T11")
def agent_reaches_package_registry():
    # env -i: no proxy variables, so curl connects directly.
    result = as_agent(*curl_argv(REGISTRY_URL))
    if result.returncode == 0 and result.stdout == "200":
        return True, f"{REGISTRY_URL}: HTTP 200"
    return False, (
        f"{REGISTRY_URL}: curl exited {result.returncode}, HTTP {result.stdout} "
        f"{result.stderr.strip()}"
    )


def dpkg_status(package):
    result = run(["/usr/bin/dpkg-query", "-W", "-f", "${Status} ${Version}", package])
    return result.stdout if result.returncode == 0 else "not installed"


def wardian_apt(*args):
    return as_agent("sudo", "-n", WRAPPER, *args, timeout=600)


@control("T12")
def wardian_apt_installs_only_new_packages():
    failures = []
    # Refused before apt runs: exit 2.
    refused = (
        ("-y", "jq"),
        ("--reinstall", "jq"),
        ("-oAPT::Get::Assume-Yes=1", "jq"),
        ("/tmp/jq.deb",),  # noqa: S108 - an argument wardian-apt must refuse
        ("./jq.deb",),
        ("../jq",),
        ("jq+",),
        ("jq-",),
        ("jq", "--"),
        (),
    )
    for args in refused:
        result = wardian_apt(*args)
        if result.returncode != 2:
            failures.append(
                f"wardian-apt {' '.join(args)} exited {result.returncode}, not 2: "
                f"{result.stderr.strip()}"
            )
    # A valid name that no package has. As a regular expression it matches
    # zsh, and apt-get would install that unless Pattern-Only is set.
    result = wardian_apt("z.h")
    if result.returncode == 0 or "Unable to locate package z.h" not in result.stderr:
        failures.append(
            f"wardian-apt z.h exited {result.returncode}: {result.stderr.strip()[-500:]}"
        )
    # Installs jq, or does nothing if it is already installed. Nothing here
    # removes a package from a sandbox in use.
    result = wardian_apt("jq")
    status = dpkg_status("jq")
    if result.returncode != 0 or not status.startswith("install ok installed"):
        failures.append(
            f"wardian-apt jq exited {result.returncode}, jq is {status}: "
            f"{result.stderr.strip()[-500:]}"
        )
    if failures:
        return False, "; ".join(failures)
    return True, (
        f"refused {len(refused)} bad argument lists and a regular expression; "
        f"jq is {status}"
    )


@control("T13")
def wardian_apt_refuses_upgrades():
    # CI only: it replaces jq with a fake older version, then purges it.
    dpkg = [*AS_ROOT, "/usr/bin/dpkg"]
    with tempfile.TemporaryDirectory() as tmp:
        control_dir = Path(tmp, "pkg", "DEBIAN")
        control_dir.mkdir(parents=True)
        (control_dir / "control").write_text(
            "Package: jq\nVersion: 0~wardian\nArchitecture: all\n"
            "Maintainer: Wardian <wardian@wardian.invalid>\n"
            "Description: an old jq, so installing jq is an upgrade\n"
        )
        deb = Path(tmp, "jq.deb")
        build = [*AS_ROOT, "/usr/bin/dpkg-deb", "--build", str(control_dir.parent)]
        run([*build, str(deb)], check=True)
        run([*dpkg, "--purge", "jq"], check=True)
        run([*dpkg, "--install", str(deb)], check=True)
    try:
        result = wardian_apt("jq")
        status = dpkg_status("jq")
    finally:
        run([*dpkg, "--purge", "jq"], check=True)
    if result.returncode == 3 and "recreate the sandbox" in result.stderr:
        return True, f"an upgrade of jq was refused; jq stayed at {status}"
    return False, (
        f"an upgrade of jq exited {result.returncode}, not 3, jq is {status}: "
        f"{result.stderr.strip()[-500:]}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=("ci", "lima"))
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(THREAT_IDS),
        help="run only this threat ID; repeat for more",
    )
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("control_tests.py: run this as root", file=sys.stderr)
        return 2

    failed = False
    for threat_id, (env, description) in THREAT_IDS.items():
        func = TESTS.get(threat_id)
        if env not in (args.env, "both"):
            result, detail = "skip", f"runs in {env} only"
        elif args.only and threat_id not in args.only:
            result, detail = "skip", "not selected with --only"
        else:
            if func is None:
                ok, detail = False, "mandatory threat ID has no test"
            else:
                try:
                    ok, detail = func()
                except Exception as exc:  # noqa: BLE001 - an error is a failed test
                    ok, detail = False, f"error: {exc!r}"
            result = "pass" if ok else "fail"
            failed |= not ok
        record = {
            "id": threat_id,
            "description": description,
            "env": env,
            "result": result,
            "detail": detail,
        }
        print(json.dumps(record), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
