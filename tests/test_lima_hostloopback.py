"""The hostLoopback positive control: a throwaway VM with hostLoopback: true.

tests/test_lima_isolation.py shows that a sandbox's root can't reach a
listener on the Mac's 127.0.0.1 through 192.168.5.2, even past nftables. This
shows the same probe connects when only hostLoopback changes, so that test
fails for the right reason. Rerun it whenever the Lima version changes.

It builds a VM from lima/wardian.yaml with hostLoopback: true, never
provisions it, reaches it only through `limactl shell`, and deletes it. It
runs only when no Lima instance is running, because the template fixes the
SSH port:

    WARDIAN_HOSTLOOPBACK_CONTROL=1 uv run pytest tests/test_lima_hostloopback.py
"""

import os
import subprocess

import pytest
from test_lima_isolation import BANNER, CONNECT, mac_listener, reach_from_mac

from wardian import cli

CONTROL = "wardian-loopback-control"
pytestmark = [
    pytest.mark.lima,
    pytest.mark.skipif(
        os.environ.get("WARDIAN_HOSTLOOPBACK_CONTROL") != "1",
        reason="set WARDIAN_HOSTLOOPBACK_CONTROL=1 to build the control VM",
    ),
]


def test_host_loopback_true_reaches_the_macs_loopback(tmp_path):
    running = subprocess.run(
        ["limactl", "list", "--format", "{{.Name}} {{.Status}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert " Running" not in running, f"stop every instance first:\n{running}"
    assert cli.find_instance(CONTROL) is None

    text = cli.TEMPLATE.read_text()
    assert text.count("  hostLoopback: false\n") == 1
    template = tmp_path / "loopback-control.yaml"
    template.write_text(
        text.replace("  hostLoopback: false\n", "  hostLoopback: true\n")
    )

    try:
        cli.run(
            ["limactl", "create", "--tty=false", f"--name={CONTROL}", str(template)]
        )
        config = cli.find_instance(CONTROL)["config"]
        assert config["networkOpts"]["hostLoopback"] is True
        cli.run(["limactl", "start", "--tty=false", CONTROL])
        with mac_listener("127.0.0.1") as port:
            assert reach_from_mac("127.0.0.1", port) == BANNER
            result = subprocess.run(
                ["limactl", "shell", "--workdir", "/", CONTROL,
                 "python3", "-I", "-c", CONNECT, cli.LIMA_RESOLVER, str(port)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )  # fmt: skip
    finally:
        subprocess.run(["limactl", "delete", "--force", CONTROL], check=False)
    assert result.stdout == BANNER.decode(), result.stderr
