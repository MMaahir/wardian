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
import json
import os
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
    # masked. Each must fail: dig exits 0 for any answer, NXDOMAIN included.
    probes = (
        ("dig", "-r", "+time=3", "+tries=1", BLOCKED_NAME),
        ("dig", "-r", "+tcp", "+time=3", "+tries=1", BLOCKED_NAME),
        ("getent", "hosts", BLOCKED_NAME),
        ("resolvectl", "query", BLOCKED_NAME),
    )
    resolved = []
    details = []
    for probe in probes:
        result = as_agent(*probe)
        details.append(f"{probe[0]} exited {result.returncode}")
        if result.returncode == 0:
            resolved.append(" ".join(probe))
    if resolved:
        return False, "resolved a name: " + "; ".join(resolved)
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
    if exit_code != 0 and after > before:
        return (
            True,
            f"{PUBLIC_ADDRESS}: curl exited {exit_code}, agent_dropped {before} -> {after}",
        )
    return False, (
        f"{PUBLIC_ADDRESS}: curl exited {exit_code}, HTTP {code}, "
        f"agent_dropped {before} -> {after} {stderr}"
    )


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
