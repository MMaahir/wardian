# The wardian CLI

`wardian` creates, tests and destroys a sandbox: a Lima VM built from `lima/wardian.yaml` and provisioned by `ansible/playbook.yml`. So far the playbook creates the unprivileged `agent` user and installs the control tests. The network controls, agent CLIs and git transport come in later tickets.

## Requirements

- macOS on Apple silicon with Lima 2.3 (`brew install lima`)
- uv. It installs Python 3.13, ruff, pytest and ansible-core into `.venv` from `uv.lock`.

Run the CLI with `uv run` from the repo, so `ansible-playbook` from `.venv` is on `PATH`. The CLI itself uses only the Python standard library.

## Commands

Each command takes an optional sandbox name, which defaults to `wardian`. A name is 1 to 31 characters: a lowercase letter, then lowercase letters, digits or `-`. Wardian runs one sandbox at a time, because the template fixes the SSH port at 60022.

```sh
uv run wardian create    # build, check, boot and provision
uv run wardian test      # run the control tests and report each threat ID
uv run wardian destroy   # delete the VM and its known-hosts file
```

`create`:

1. Refuses if `$LIMA_HOME/_config/default.yaml`, `override.yaml` or `base.yaml` exists (`$LIMA_HOME` defaults to `~/.lima`). Lima merges those files into every instance.
2. Runs `limactl create --tty=false` without starting the VM, then checks Lima's merged config from `limactl list --json`. It refuses if `mounts`, `networks` or `provision` is set, if `networkOpts.hostLoopback`, `propagateProxyEnv`, `ssh.forwardAgent` or `ssh.overVsock` is anything but false, if `portForwards` is anything but the template's catch-all ignore rule, or if Lima's admin user would be named `agent`. A refused VM never boots. Remove it with `wardian destroy`.
3. Starts the VM, reads its ed25519 host key with `limactl shell`, and writes `~/.ssh/known_hosts.d/<name>`. The entry is keyed by the host alias `<name>`, not by `127.0.0.1:60022`. Lima itself pins the key the guest presents on first boot, so the trust starts there. From this file on, every Wardian SSH connection checks the key strictly.
4. Runs the playbook over SSH as Lima's admin user, with `StrictHostKeyChecking=yes` against that file, no reused connection, and `-F /dev/null`, so your `~/.ssh/config` doesn't apply. `ANSIBLE_CONFIG` points at `ansible/ansible.cfg`, so an `ansible.cfg` in the directory you run from is never read.

`test` repeats the `_config` check, then connects the same way and runs the control tests:

```sh
sudo -n runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin \
  /usr/bin/python3 -I /usr/local/lib/wardian/control_tests.py --env lima
```

It prints `PASS`, `FAIL` or `SKIP` for each threat ID and exits 1 if any test fails or none passes.

`destroy` runs `limactl delete --force` and removes the known-hosts file. It doesn't yet check for unfetched work.

The CLI strips control characters from everything limactl, ansible-playbook, ssh and the guest print.

## Control tests

`ansible/roles/control_tests/files/control_tests.py` is one standard-library Python file. Each test declares a threat ID, a description and where it runs: `ci`, `lima` or `both`. The playbook installs it as `root:root 0644` under `/usr/local/lib/wardian`.

| Threat ID | Runs in | Checks |
|---|---|---|
| T01 | both | `agent` can't use sudo: `sudo -n -l` fails and says `agent` may not run sudo. Any grant, even for one command, fails the test. |

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The unit tests run without a VM.
