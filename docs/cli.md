# The wardian CLI

`wardian` creates, tests and destroys a sandbox: a Lima VM built from `lima/wardian.yaml` and provisioned by `ansible/playbook.yml`. So far the playbook:

- creates the unprivileged `agent` user
- removes snapd and packagekit
- writes a static `/etc/resolv.conf` and masks systemd-resolved
- installs Squid with the allowlist
- installs the nftables ruleset, with a drop-in that powers the guest off if the ruleset fails to load
- installs the control tests

The agent CLIs and git transport come in later tickets.

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
4. Runs the playbook over SSH as Lima's admin user, with `wardian_resolver` set to Lima's resolver, 192.168.5.2. SSH uses `StrictHostKeyChecking=yes` against that file, no reused connection, and `-F /dev/null`, so your `~/.ssh/config` doesn't apply. `ANSIBLE_CONFIG` points at `ansible/ansible.cfg`, so an `ansible.cfg` in the directory you run from is never read.

`test` repeats the `_config` check, then connects the same way and runs the control tests as root:

```sh
sudo -n /usr/bin/python3 -I /usr/local/lib/wardian/control_tests.py --env lima
```

Root only reads the firewall's counters. The script runs every probe as `agent` with an empty environment, through `runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin`.

It prints `PASS`, `FAIL` or `SKIP` for each threat ID and exits 1 if any test fails or none passes.

`destroy` runs `limactl delete --force` and removes the known-hosts file. It refuses to delete a Lima instance that `create` didn't make: `create` writes a `wardian-sandbox` file into the instance directory right after `limactl create`. It doesn't yet check for unfetched work.

The CLI strips control characters from everything limactl, ansible-playbook, ssh and the guest print.

## Control tests

`ansible/roles/control_tests/files/control_tests.py` is one standard-library Python file. Its `THREAT_IDS` table gives each threat ID a description and where it must run: `ci`, `lima` or `both`. A threat ID that applies to the environment but has no test fails the run, so deleting a test can't make the suite pass. The playbook installs the file as `root:root 0644` under `/usr/local/lib/wardian`.

| Threat ID | Runs in | Checks |
|---|---|---|
| T01 | both | `agent` can't use sudo: `sudo -n -l` fails and says `agent` may not run sudo. Any grant, even for one command, fails the test. |
| T02 | both | `agent` can't resolve a name: `dig` over UDP and TCP, `getent hosts` and `resolvectl query` all fail. |
| T03 | both | Squid answers 403 to a CONNECT for `example.com`, which isn't on the allowlist. |
| T04 | both | A CONNECT through Squid to `api.anthropic.com` gets 200, and an HTTP reply comes back through the tunnel. |
| T05 | both | Squid answers 403 to a CONNECT for private, loopback, link-local and metadata addresses, IPv4 and IPv6. |
| T06 | both | A direct connection from `agent` to `1.1.1.1:443` fails, and the `agent_dropped` counter goes up. |

## Egress

`agent` reaches the internet only through Squid on `127.0.0.1:3128`, which allows CONNECT to port 443 for `api.anthropic.com`, `chatgpt.com` and `auth.openai.com`, the list from the spike's phase 2. Squid denies other names before it looks them up, and denies allowlisted names that resolve to a private address.

The nftables ruleset in `/etc/nftables.conf` drops outbound traffic for every user unless a rule accepts it. In order:

1. Established and related traffic.
2. `agent` to Squid's port and to non-DNS loopback ports.
3. Squid's user to the resolver on port 53, and to public addresses on port 443. Private ranges are dropped.
4. `systemd-network` to the resolver for DHCP renewals.
5. Root and `_apt` to the resolver on port 53, and to public addresses on ports 80 and 443. Private ranges are dropped.
6. Everything else goes through one rate-limited log rule with the prefix `wardian-drop:`, then a counter and drop rule with no limit.

The named counters `agent_dropped`, `proxy_private_dropped` and `dropped` show which rule dropped a packet: `sudo nft list counters table inet wardian`.

The only resolver is `wardian_resolver`, written to a static `/etc/resolv.conf`. systemd-resolved and its sockets are masked.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The unit tests run without a VM.

## CI

CircleCI runs `.circleci/config.yml` on Arm machine VMs. The `incus` job starts an Ubuntu 26.04 Incus system container, so the firewall rules never touch the CI host, and then:

1. applies the playbook twice over SSH, with `wardian_resolver` set to the Incus bridge, and fails unless the second run reports `changed=0`
2. restarts the container and checks that nftables loaded
3. runs the control tests with `--env ci`, through the same `python3 -I` command as `wardian test`
4. appends a line that isn't nft syntax to `/etc/nftables.conf`, restarts the container, and fails unless it stops within 120 seconds

The `mutation` job runs alongside it in its own container. It applies the playbook once, removes one control at a time, runs the control tests, and fails unless the tests for that control fail:

1. `nft flush ruleset` removes the firewall. T02 (DNS) and T06 (direct egress) must fail. The job flushes the live ruleset rather than breaking `/etc/nftables.conf`, because a ruleset that fails to load powers the container off before the tests run. It then reloads `nftables.service` to put the ruleset back.
2. Deleting the `allowlist` ACL and its `http_access deny` line from `/etc/squid/squid.conf`, then restarting Squid, removes the allowlist. T03 (a name off the allowlist) must fail.
