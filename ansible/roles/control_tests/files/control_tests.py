"""Wardian control tests.

Each threat ID in THREAT_IDS has a description, the environment it must run
in (ci for the Incus container, lima for a Lima sandbox, or both) and one test.
The test checks the control from the outside, the way an attacker would try it.

The playbook installs this file root-owned. Root runs it as agent with an
empty environment, so nothing agent can write changes the result:

    runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin \
        /usr/bin/python3 -I control_tests.py --env lima

It prints one JSON object per threat ID and exits 1 if any test fails. A
threat ID that applies to the environment but has no test counts as a failure,
so deleting a test fails the run. Standard library only. Network probes use
sockets or `curl -q`.
"""

import argparse
import json
import os
import pwd
import subprocess
import sys

# Every threat ID is mandatory in its environment.
THREAT_IDS = {
    "T01": ("both", "agent can't use sudo"),
}
for _threat_id, (_env, _) in THREAT_IDS.items():
    if _env not in ("ci", "lima", "both"):
        raise ValueError(f"{_threat_id}: unknown environment {_env!r}")

TESTS = {}


def control(threat_id):
    if threat_id not in THREAT_IDS:
        raise ValueError(f"{threat_id}: not listed in THREAT_IDS")

    def register(func):
        TESTS[threat_id] = func
        return func

    return register


@control("T01")
def agent_cannot_sudo():
    # `sudo -n true` would pass with a grant that leaves out `true`, such as
    # NOPASSWD: /usr/bin/apt. `sudo -l` lists every grant, and only a user with
    # none gets "may not run sudo".
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "-l"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 and "may not run sudo" in output:
        return True, output
    return False, f"sudo -n -l exited {result.returncode}: {output}"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=("ci", "lima"))
    args = parser.parse_args(argv)

    user = pwd.getpwuid(os.geteuid()).pw_name
    if user != "agent":
        print(f"control_tests.py: run this as agent, not {user}", file=sys.stderr)
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
