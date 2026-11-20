"""Wardian control tests.

Each threat ID in THREAT_IDS has a description, the environment it must run
in (ci for the Incus container, lima for a Lima sandbox, or both) and one test.
The test checks the control from the outside, the way an attacker would try it.

The playbook installs this file root-owned, and root runs it:

    /usr/bin/python3 -I control_tests.py --env lima

Root only reads the firewall's counters. Every probe runs as agent with an
empty environment, so nothing agent can write changes the result:

    runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin <probe>

It prints one JSON object per threat ID and exits 1 if any test fails. A
threat ID that applies to the environment but has no test counts as a failure,
so deleting a test fails the run. Standard library only. Network probes use
`curl -q`, which reads no config file, and `dig -r`, which doesn't read
~/.digrc.
"""

import argparse
import grp
import json
import os
import pwd
import stat
import subprocess
import sys

# Every threat ID is mandatory in its environment.
THREAT_IDS = {
    "T01": ("both", "agent can't use sudo"),
    "T02": ("both", "agent can't resolve names through any path"),
    "T03": ("both", "Squid denies a name that isn't on the allowlist"),
    "T04": ("both", "Squid lets agent reach an allowlisted name"),
    "T05": ("both", "Squid denies private addresses"),
    "T06": ("both", "nftables drops and counts agent's direct connections"),
    "T07": ("both", "agent can't write the agent CLIs, their config or the token"),
    "T08": ("both", "Squid denies the agent CLIs' update hosts"),
    "T09": ("both", "agent's environment has no cloud or AI API variables"),
    "T10": ("both", "Squid's user and logship can't read the Claude token"),
}
for _threat_id, (_env, _) in THREAT_IDS.items():
    if _env not in ("ci", "lima", "both"):
        raise ValueError(f"{_threat_id}: unknown environment {_env!r}")

TESTS = {}

AS_AGENT = [
    "/usr/sbin/runuser", "-u", "agent", "--",
    "/usr/bin/env", "-i", "HOME=/home/agent", "PATH=/usr/bin:/bin",
]  # fmt: skip
PROXY = "http://127.0.0.1:3128"
BLOCKED_NAME = "example.com"
ALLOWED_URL = "https://api.anthropic.com/"
# Lima's resolver and host, private ranges, the metadata address, loopback,
# and an IPv4-mapped IPv6 address.
PRIVATE_ADDRESSES = (
    "192.168.5.2",
    "10.0.0.1",
    "172.16.0.1",
    "100.64.0.1",
    "169.254.169.254",
    "127.0.0.1",
    "[::1]",
    "[fd00::1]",
    "[fe80::1]",
    "[::ffff:192.168.5.2]",
)
PUBLIC_ADDRESS = "1.1.1.1"
TOKEN = "/etc/wardian/claude-token"  # noqa: S105 - a path, not a token
# What agent must not change: the agent CLIs, their managed config, the token,
# and the files that put the token and the proxy into its environment. Each
# tree is checked to every depth, each path on its own.
PROTECTED_TREES = ("/opt/codex", "/etc/claude-code", "/etc/codex", "/etc/wardian")
PROTECTED_PATHS = (
    "/usr/bin",
    "/usr/bin/claude",
    "/usr/local/bin",
    "/usr/local/bin/codex",
    "/etc/claude-code/managed-settings.json",
    "/etc/codex/managed_config.toml",
    "/usr/local/sbin",
    "/usr/local/sbin/wardian-install-claude-token",
    "/etc/profile.d",
    "/etc/profile.d/wardian-claude-token.sh",
    "/etc/environment",
    TOKEN,
)
# Where Claude Code (native installer, npm) and Codex (GitHub releases) fetch
# updates. The apt repo is on downloads.claude.ai too.
UPDATE_HOSTS = (
    "downloads.claude.ai",
    "storage.googleapis.com",
    "registry.npmjs.org",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
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


def control(threat_id):
    if threat_id not in THREAT_IDS:
        raise ValueError(f"{threat_id}: not listed in THREAT_IDS")

    def register(func):
        TESTS[threat_id] = func
        return func

    return register


def as_agent(*argv, timeout=60):
    return subprocess.run(
        [*AS_AGENT, *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        # sudo keeps the admin's working directory, which agent can't enter.
        cwd="/",
    )


def as_user(user, *argv, timeout=30):
    return subprocess.run(
        [
            "/usr/sbin/runuser",
            "-u",
            user,
            "--",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            *argv,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        # sudo keeps the admin's working directory, which agent can't enter.
        cwd="/",
    )


def curl(url, *options):
    """Fetch url as agent. Return curl's exit code, CONNECT status and HTTP status."""
    result = as_agent(
        "curl", "-q", "--silent", "--show-error", "--output", "/dev/null",
        "--connect-timeout", "5", "--max-time", "20",
        "--write-out", "%{http_connect} %{http_code}",
        *options, url,
    )  # fmt: skip
    connect, _, code = result.stdout.partition(" ")
    return result.returncode, connect, code, result.stderr.strip()


def counter(name):
    result = subprocess.run(
        ["/usr/sbin/nft", "--json", "list", "counter", "inet", "wardian", name],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    for item in json.loads(result.stdout)["nftables"]:
        if "counter" in item:
            return item["counter"]["packets"]
    raise ValueError(f"nft printed no counter named {name}")


@control("T01")
def agent_cannot_sudo():
    # `sudo -n true` would pass with a grant that leaves out `true`, such as
    # NOPASSWD: /usr/bin/apt. `sudo -l` lists every grant, and only a user with
    # none gets "may not run sudo".
    result = as_agent("sudo", "-n", "-l", timeout=30)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 and "may not run sudo" in output:
        return True, output
    return False, f"sudo -n -l exited {result.returncode}: {output}"


@control("T02")
def agent_cannot_resolve():
    # dig asks the resolver in /etc/resolv.conf over UDP and TCP. getent goes
    # through glibc's resolver. resolvectl asks systemd-resolved, which is
    # masked. Each must fail with the exit code for a lookup that got no
    # answer, so a probe that can't run (env exits 127) doesn't pass: dig 9
    # (no reply from a server), getent 2 (not found), resolvectl 1.
    probes = (
        (9, "dig", "-r", "+time=3", "+tries=1", BLOCKED_NAME),
        (9, "dig", "-r", "+tcp", "+time=3", "+tries=1", BLOCKED_NAME),
        (2, "getent", "hosts", BLOCKED_NAME),
        (1, "resolvectl", "query", BLOCKED_NAME),
    )
    failures = []
    details = []
    for expected, *probe in probes:
        result = as_agent(*probe)
        details.append(f"{probe[0]} exited {result.returncode}")
        if result.returncode != expected:
            failures.append(
                f"{' '.join(probe)} exited {result.returncode}, not {expected}: "
                f"{result.stderr.strip()}"
            )
    if failures:
        return False, "; ".join(failures)
    return True, ", ".join(details)


@control("T03")
def squid_denies_blocked_name():
    exit_code, connect, code, stderr = curl(
        f"https://{BLOCKED_NAME}/", "--proxy", PROXY
    )
    if exit_code != 0 and connect == "403":
        return True, f"{BLOCKED_NAME}: CONNECT got 403"
    return (
        False,
        f"{BLOCKED_NAME}: curl exited {exit_code}, CONNECT {connect}, HTTP {code} {stderr}",
    )


@control("T04")
def squid_allows_allowlisted_name():
    # Any HTTP reply means the tunnel opened and TLS completed.
    exit_code, connect, code, stderr = curl(ALLOWED_URL, "--proxy", PROXY)
    if exit_code == 0 and connect == "200" and code != "000":
        return True, f"{ALLOWED_URL}: CONNECT 200, HTTP {code}"
    return (
        False,
        f"{ALLOWED_URL}: curl exited {exit_code}, CONNECT {connect}, HTTP {code} {stderr}",
    )


@control("T05")
def squid_denies_private_addresses():
    failures = []
    for address in PRIVATE_ADDRESSES:
        exit_code, connect, _, stderr = curl(f"https://{address}/", "--proxy", PROXY)
        if exit_code == 0 or connect != "403":
            failures.append(
                f"{address}: curl exited {exit_code}, CONNECT {connect} {stderr}"
            )
    if failures:
        return False, "; ".join(failures)
    return True, f"CONNECT got 403 for {len(PRIVATE_ADDRESSES)} private addresses"


@control("T06")
def nftables_drops_direct_connection():
    before = counter("agent_dropped")
    exit_code, _, code, stderr = curl(
        f"https://{PUBLIC_ADDRESS}/", "--connect-timeout", "3"
    )
    after = counter("agent_dropped")
    # 28 is curl's timeout. A connection that opened and then failed TLS
    # exits with another code.
    if exit_code == 28 and after > before:
        return (
            True,
            f"{PUBLIC_ADDRESS}: curl exited {exit_code}, agent_dropped {before} -> {after}",
        )
    return False, (
        f"{PUBLIC_ADDRESS}: curl exited {exit_code}, HTTP {code}, "
        f"agent_dropped {before} -> {after} {stderr}"
    )


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


@control("T08")
def squid_denies_update_hosts():
    failures = []
    for host in UPDATE_HOSTS:
        exit_code, connect, _, stderr = curl(f"https://{host}/", "--proxy", PROXY)
        if exit_code == 0 or connect != "403":
            failures.append(
                f"{host}: curl exited {exit_code}, CONNECT {connect} {stderr}"
            )
    if failures:
        return False, "; ".join(failures)
    return True, f"CONNECT got 403 for {len(UPDATE_HOSTS)} update hosts"


def variable_names(output):
    return {line.split("=", 1)[0] for line in output.splitlines() if "=" in line}


@control("T09")
def agent_environment_has_no_api_variables():
    # A login goes through PAM, which reads /etc/environment, then the login
    # shell reads /etc/profile. su --login runs PAM like an SSH login and then
    # runs env as the "shell". bash reads /etc/profile but not agent's own
    # dotfiles. Each probe has a variable it must find, so a probe that read
    # nothing can't pass.
    pam = subprocess.run(
        ["/usr/bin/su", "--login", "--shell", "/usr/bin/env", "agent"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    profile = as_agent(
        "bash", "--noprofile", "--norc", "-c", ". /etc/profile && exec env"
    )
    failures = []
    names = set()
    for label, result, expected in (
        ("su --login", pam, "HTTPS_PROXY"),
        ("/etc/profile", profile, "CLAUDE_CODE_OAUTH_TOKEN"),
    ):
        found = variable_names(result.stdout)
        names |= found
        if result.returncode != 0 or expected not in found:
            failures.append(
                f"{label} exited {result.returncode} without {expected}: "
                f"{result.stderr.strip()}"
            )
    forbidden = sorted(n for n in names if n.startswith(FORBIDDEN_ENV_PREFIXES))
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
    details = []
    for user in ("proxy", "logship"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            if user == "proxy":
                failures.append("no proxy user")
            details.append(f"no {user} user yet")
            continue
        result = as_user(user, "cat", TOKEN)
        if result.returncode == 0 or "Permission denied" not in result.stderr:
            failures.append(
                f"cat as {user} exited {result.returncode}: {result.stderr.strip()}"
            )
        else:
            details.append(f"{user}: Permission denied")
    if failures:
        return False, "; ".join(failures)
    return True, f"{TOKEN} is root:agent 0640; " + ", ".join(details)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=("ci", "lima"))
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("control_tests.py: run this as root", file=sys.stderr)
        return 2

    failed = False
    for threat_id, (env, description) in THREAT_IDS.items():
        func = TESTS.get(threat_id)
        if env not in (args.env, "both"):
            result, detail = "skip", f"runs in {env} only"
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
