"""The Mac's view of a real Lima sandbox: the guest can't reach the Mac.

These tests need a running sandbox made by `wardian create`, named in
WARDIAN_LIMA_SANDBOX. They change nothing that outlasts them, so run them
before tests/test_lima_git.py, which destroys the sandbox:

    WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_isolation.py

Each probe that must fail has a positive check that the target answers when
nothing blocks it, so a broken probe can't pass.
"""

import contextlib
import os
import shlex
import socket
import subprocess
import threading
import time

import pytest

from wardian import cli

NAME = os.environ.get("WARDIAN_LIMA_SANDBOX", "")
pytestmark = [
    pytest.mark.lima,
    pytest.mark.skipif(
        not NAME, reason="set WARDIAN_LIMA_SANDBOX to a sandbox made by wardian create"
    ),
]

BANNER = b"wardian-mac-listener\n"
# Run in the guest: connect to argv[1]:argv[2] and print what comes back. A
# failed connect prints "blocked: ..." and exits 0, so a probe that didn't run
# (broken SSH, no python3) can't pass as blocked.
CONNECT = (
    "import socket, sys\n"
    "try:\n"
    "    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)\n"
    "except OSError as error:\n"
    "    sys.exit(print(f'blocked: {error!r}'))\n"
    "sys.stdout.write(s.recv(100).decode())\n"
)

# Run as agent in the guest: listen on every address on port argv[1], answer
# "agent", and exit when ssh closes stdin.
AGENT_LISTENER = """
import os, socket, sys, threading
server = socket.create_server(("0.0.0.0", int(sys.argv[1])))
def serve():
    while True:
        conn, _ = server.accept()
        conn.sendall(b"agent\\n")
        conn.close()
threading.Thread(target=serve, daemon=True).start()
print("ready", flush=True)
sys.stdin.read()
os._exit(0)
"""


@pytest.fixture(scope="module")
def instance():
    found = cli.find_instance(NAME)
    assert found is not None and found["status"] == "Running"
    return found


def as_agent(command: str, **kwargs) -> subprocess.CompletedProcess:
    """Run command as agent in the guest, through the wardian-<name> host."""
    return subprocess.run(
        ["ssh", "-F", str(cli.host_config()), f"wardian-{NAME}", command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        **kwargs,
    )


def as_root(instance, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*cli.admin_ssh(NAME, instance), f"sudo -n sh -c {shlex.quote(command)}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def connect_from_guest(host: str, port: int, root_instance=None):
    command = shlex.join(["python3", "-I", "-c", CONNECT, host, str(port)])
    if root_instance is not None:
        return as_root(root_instance, command)
    return as_agent(command)


def assert_blocked(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("blocked: "), result.stdout


def counted_packets(listing: str, handle: str) -> int:
    """The packet count of the rule with this handle in `nft --handle list`."""
    line = next(line for line in listing.splitlines() if f"# handle {handle}" in line)
    return int(line.split("counter packets ")[1].split()[0])


@contextlib.contextmanager
def mac_listener(address: str):
    """A short-lived listener on the Mac that answers BANNER. Yields its port."""
    server = socket.create_server((address, 0))
    server.settimeout(0.5)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.sendall(BANNER)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join()
        server.close()


def reach_from_mac(address: str, port: int) -> bytes:
    with socket.create_connection((address, port), timeout=5) as conn:
        return conn.recv(100)


def mac_lan_address() -> str | None:
    route = subprocess.run(
        ["route", "-n", "get", "default"], capture_output=True, text=True, check=False
    )
    for line in route.stdout.splitlines():
        key, _, value = line.strip().partition(": ")
        if key == "interface":
            found = subprocess.run(
                ["ipconfig", "getifaddr", value.strip()],
                capture_output=True,
                text=True,
                check=False,
            )
            return found.stdout.strip() or None
    return None


def app_firewall_on() -> bool:
    state = subprocess.run(
        ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "enabled" in state.stdout


def test_no_host_mounts(instance):
    result = as_root(instance, "findmnt --raw --noheadings --output FSTYPE,TARGET")
    assert result.returncode == 0, result.stderr
    shared = [
        line
        for line in result.stdout.splitlines()
        if line.split()[0] in ("virtiofs", "9p", "fuse.sshfs", "sshfs")
    ]
    assert shared == []


def test_a_listener_agent_starts_is_unreachable_from_the_mac(instance):
    port = 47911
    server = subprocess.Popen(
        ["ssh", "-F", str(cli.host_config()), f"wardian-{NAME}",
         shlex.join(["python3", "-I", "-c", AGENT_LISTENER, str(port)])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        assert server.stdout.readline() == "ready\n", server.stderr.read()
        # Positive: the listener answers inside the guest.
        inside = connect_from_guest("127.0.0.1", port)
        assert inside.stdout == "agent\n", inside.stderr
        # Lima would forward it to the Mac's 127.0.0.1 without the ignore rule.
        # Lima's host agent sets up forwards when the guest agent reports a
        # new listener, a few seconds later, so keep trying for a while.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
            time.sleep(0.5)
    finally:
        # The listener exits when ssh closes its stdin.
        server.stdin.close()
        server.wait(timeout=30)


def test_guest_cant_reach_the_macs_loopback_through_the_gateway():
    with mac_listener("127.0.0.1") as port:
        assert reach_from_mac("127.0.0.1", port) == BANNER
        result = connect_from_guest(cli.LIMA_RESOLVER, port)
    assert_blocked(result)


def test_guest_cant_reach_the_macs_lan_address():
    address = mac_lan_address()
    if address is None:
        pytest.skip("the Mac has no default route with an IPv4 address")
    if app_firewall_on():
        pytest.skip("the macOS application firewall may prompt for the listener")
    with mac_listener("0.0.0.0") as port:  # noqa: S104 - the LAN address must answer
        assert reach_from_mac(address, port) == BANNER
        result = connect_from_guest(address, port)
    assert_blocked(result)


def test_guest_cant_reach_the_metadata_address(instance):
    # Nothing need listen there, so a counter-only rule at the top of the drop
    # chain shows the firewall dropped this probe, not that the address was
    # empty. Other drops don't match it.
    rule = "meta skuid agent ip daddr 169.254.169.254 tcp dport 80 counter"
    insert = as_root(
        instance, f"nft --echo --handle insert rule inet wardian drop_logged {rule}"
    )
    assert insert.returncode == 0, insert.stderr
    handle = insert.stdout.split("# handle ")[-1].split()[0]
    try:
        result = connect_from_guest("169.254.169.254", 80)
        listed = as_root(instance, "nft --handle list chain inet wardian drop_logged")
    finally:
        removed = as_root(
            instance, f"nft delete rule inet wardian drop_logged handle {handle}"
        )
    assert removed.returncode == 0, removed.stderr
    assert_blocked(result)
    assert counted_packets(listed.stdout, handle) > 0


def test_host_loopback_off_holds_on_its_own(instance):
    # Lets root through nftables to the gateway on the listener's port, so
    # only Lima's hostLoopback: false can stop root's probe. The counter
    # proves the probe went through the rule.
    with mac_listener("127.0.0.1") as port:
        assert reach_from_mac("127.0.0.1", port) == BANNER
        rule = f"meta skuid 0 ip daddr {cli.LIMA_RESOLVER} tcp dport {port}"
        insert = as_root(
            instance,
            f"nft --echo --handle insert rule inet wardian output {rule} counter accept",
        )
        assert insert.returncode == 0, insert.stderr
        handle = insert.stdout.split("# handle ")[-1].split()[0]
        try:
            result = connect_from_guest(cli.LIMA_RESOLVER, port, root_instance=instance)
            listed = as_root(instance, "nft --handle list chain inet wardian output")
        finally:
            removed = as_root(
                instance, f"nft delete rule inet wardian output handle {handle}"
            )
    assert removed.returncode == 0, removed.stderr
    assert_blocked(result)
    assert counted_packets(listed.stdout, handle) > 0


def test_create_refuses_a_lima_override(tmp_path, monkeypatch, capsys):
    # A temporary LIMA_HOME, so other Lima commands never see the override.
    monkeypatch.setenv("LIMA_HOME", str(tmp_path / "lima"))
    override = cli.lima_home() / "_config" / "override.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(f"mounts:\n- location: {tmp_path}\n")
    assert cli.main(["create", "--agent", "codex", "wardian-override"]) == 1
    assert "override.yaml" in capsys.readouterr().err
    assert cli.find_instance("wardian-override") is None
