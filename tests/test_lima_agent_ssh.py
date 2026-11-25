"""SSH as agent into a real Lima sandbox, through the wardian-<name> host.

These tests need a running sandbox made by `wardian create`, named in
WARDIAN_LIMA_SANDBOX. They read the host block from ~/.wardian/ssh_config with
`ssh -F`, so they don't need the Include line in ~/.ssh/config. They change
nothing in the sandbox, so run them before tests/test_lima_git.py, which
destroys it:

    WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_agent_ssh.py
"""

import os
import shlex
import socket
import subprocess
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

BANNER = b"wardian-listener\n"
# Run as agent in the guest: answer BANNER on T3's port and the next one, and
# exit when ssh closes stdin.
LISTENER = f"""
import os, socket, sys, threading
servers = [socket.create_server(("127.0.0.1", p)) for p in ({cli.T3_PORT}, {cli.T3_PORT + 1})]
def serve(server):
    while True:
        conn, _ = server.accept()
        conn.sendall({BANNER!r})
        conn.close()
for server in servers:
    threading.Thread(target=serve, args=(server,), daemon=True).start()
print("ready", flush=True)
sys.stdin.read()
os._exit(0)
"""


def ssh(*args: str) -> list[str]:
    return ["ssh", "-F", str(cli.host_config()), *args, f"wardian-{NAME}"]


@pytest.fixture(scope="module")
def listener():
    proc = subprocess.Popen(
        # ssh hands the command to agent's shell, so quote it.
        [*ssh(), shlex.join(["python3", "-c", LISTENER])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout.readline() == b"ready\n", proc.stderr.read()
        yield
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def through_local_forward(target: str) -> tuple[bytes, str]:
    """Open -L to target, connect once, and return what came back and ssh's stderr."""
    port = free_port()
    proc = subprocess.Popen(
        [*ssh("-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{port}:{target}")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                conn = socket.create_connection(("127.0.0.1", port), timeout=10)
                break
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, "the local forward never listened"
                time.sleep(0.2)
        with conn:
            data = b""
            while chunk := conn.recv(1024):
                data += chunk
        # ssh logs a refused channel just after it closes the local socket.
        time.sleep(1)
    finally:
        proc.terminate()
        _, stderr = proc.communicate(timeout=30)
    return data, stderr.decode("utf-8", "replace")


def test_logs_in_as_agent():
    result = subprocess.run(
        [*ssh(), "id", "-un"], capture_output=True, text=True, check=True
    )
    assert result.stdout == "agent\n"


def test_local_forward_to_t3_port_reaches_the_listener(listener):
    data, stderr = through_local_forward(f"127.0.0.1:{cli.T3_PORT}")
    assert data == BANNER, stderr


def test_stdio_forward_to_t3_port_reaches_the_listener(listener):
    result = subprocess.run(
        [*ssh("-W", f"127.0.0.1:{cli.T3_PORT}")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.stdout == BANNER, result.stderr


@pytest.mark.parametrize(
    "target",
    [f"127.0.0.1:{cli.T3_PORT + 1}", f"{cli.LIMA_RESOLVER}:53", "1.1.1.1:443"],
)
def test_other_local_forwards_are_prohibited(listener, target):
    data, stderr = through_local_forward(target)
    assert data == b""
    assert "administratively prohibited" in stderr


def test_remote_forward_is_refused():
    result = subprocess.run(
        [*ssh("-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:0:127.0.0.1:9"), "true"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )  # fmt: skip
    assert result.returncode == 255
    assert "remote port forwarding failed" in result.stderr


def test_agent_forwarding_is_refused():
    # A short path: Unix socket paths are limited to 104 bytes on macOS.
    sock = f"/tmp/wardian-test-agent-{os.getpid()}.sock"  # noqa: S108
    agent = subprocess.run(
        ["ssh-agent", "-a", sock, "-s"], capture_output=True, text=True, check=True
    )
    pid = agent.stdout.split("SSH_AGENT_PID=")[1].split(";")[0]
    try:
        result = subprocess.run(
            [*ssh("-o", "ForwardAgent=yes"), 'echo "sock=$SSH_AUTH_SOCK"'],
            env={**os.environ, "SSH_AUTH_SOCK": sock},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    finally:
        subprocess.run(["kill", pid], check=False)
    assert result.stdout == "sock=\n"
