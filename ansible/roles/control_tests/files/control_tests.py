"""Wardian control tests.

Each test checks one control from the outside, the way an attacker would try
it, and declares a threat ID, a description and the environment it runs in:
ci (the Incus container), lima (a Lima sandbox) or both.

The playbook installs this file root-owned. Root runs it as agent with an
empty environment, so nothing agent can write changes the result:

    runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin \
        /usr/bin/python3 -I control_tests.py --env lima

It prints one JSON object per test and exits 1 if any test fails. Standard
library only. Network probes use sockets or `curl -q`.
"""

import argparse
import json
import os
import pwd
import subprocess
import sys

TESTS = []


def control(threat_id, description, env):
    if env not in ("ci", "lima", "both"):
        raise ValueError(f"{threat_id}: unknown environment {env!r}")

    def register(func):
        TESTS.append((threat_id, description, env, func))
        return func

    return register


@control("T01", "agent can't use sudo", "both")
def agent_cannot_sudo():
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "true"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        return False, "sudo -n true succeeded"
    return True, f"sudo -n true exited {result.returncode}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=("ci", "lima"))
    args = parser.parse_args()

    user = pwd.getpwuid(os.geteuid()).pw_name
    if user != "agent":
        print(f"control_tests.py: run this as agent, not {user}", file=sys.stderr)
        return 2

    failed = False
    for threat_id, description, env, func in TESTS:
        if env not in (args.env, "both"):
            result, detail = "skip", f"runs in {env} only"
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
