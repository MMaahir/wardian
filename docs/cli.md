# The wardian CLI

`wardian` creates, tests and destroys a sandbox: a Lima VM built from `lima/wardian.yaml` and provisioned by `ansible/playbook.yml`. So far the playbook:

- creates the unprivileged `agent` user
- creates `/home/agent/project`, the git repo the agents work in
- stops and masks the background apt jobs, and removes snapd, packagekit and unattended-upgrades
- writes a static `/etc/resolv.conf` and masks systemd-resolved
- upgrades every installed package, before the firewall starts
- installs the nftables ruleset, with a drop-in that powers the guest off if the ruleset fails to load
- installs Claude Code, Codex and ripgrep, with root-owned managed settings and instructions for both CLIs, and holds Claude Code at its version
- installs `wardian-apt`, its sudoers line, and a snapshot of the firewall it checks
- creates `/etc/wardian` for the Claude token, the script that installs the token, and the login profile script that exports it
- sets `WARDIAN_SANDBOX=1` in `/etc/environment` and `/etc/profile.d/wardian-sandbox.sh`
- installs the control tests

> **Being re-scoped.** Issue #1 changed on 2026-11-20 (see [ADR 0001](adr/0001-open-egress-instead-of-an-allowlist.md)). #26 replaced Squid and the allowlist with open egress and `wardian-apt`. `agent` gets its own SSH key, a T3 host block and one login per sandbox (#27). `wardian push` and `wardian fetch` replace the manual git setup (#28). This page describes the code on `main`, and each of those tickets updates it as it lands. Sections marked "Replaced by" describe behaviour that is going away.

## Requirements

- macOS on Apple silicon with Lima 2.3 (`brew install lima`)
- uv. It installs Python 3.13, ruff, pytest and ansible-core into `.venv` from `uv.lock`.
- A Claude token in your login Keychain, under the service name `wardian-claude-token`. Make one with `claude setup-token`, then store it with `security add-generic-password -U -a "$USER" -s wardian-claude-token -w`. With `-w` last and no value, `security` prompts for the token, so it never lands in your shell history.

Run the CLI with `uv run` from the repo, so `ansible-playbook` from `.venv` is on `PATH`. The CLI itself uses only the Python standard library.

## Commands

Each command takes an optional sandbox name, which defaults to `wardian`. A name is 1 to 31 characters: a lowercase letter, then lowercase letters, digits or `-`. Only one sandbox runs at a time, because the template fixes the SSH port at 60022. You can keep stopped sandboxes, one per project, and start the one you need with `limactl start <name>`.

```sh
uv run wardian create    # build, check, boot and provision
uv run wardian test      # run the control tests and report each threat ID
uv run wardian destroy --repo ~/Developer/myproject   # check for unfetched work, then delete
```

`create`:

1. Refuses if `$LIMA_HOME/_config/default.yaml`, `override.yaml` or `base.yaml` exists (`$LIMA_HOME` defaults to `~/.lima`). Lima merges those files into every instance.
2. Reads the Claude token from the Keychain with `security find-generic-password -s wardian-claude-token -w`, and refuses unless it starts with `sk-ant-oat`. The token stays in the CLI's memory and is never written to a file on the Mac. A missing token stops `create` before it builds a VM.
3. Runs `limactl create --tty=false` without starting the VM, then checks Lima's merged config from `limactl list --json`. It refuses if `mounts`, `networks` or `provision` is set, if `networkOpts.hostLoopback`, `propagateProxyEnv`, `ssh.forwardAgent` or `ssh.overVsock` is anything but false, if `portForwards` is anything but the template's catch-all ignore rule, or if Lima's admin user would be named `agent`. A refused VM never boots, and `create` deletes it.
4. Starts the VM, reads its ed25519 host key with `limactl shell`, and writes `~/.ssh/known_hosts.d/<name>`. The entry is keyed by the host alias `<name>`, not by `127.0.0.1:60022`. Lima itself pins the key the guest presents on first boot, so the trust starts there. From this file on, every Wardian SSH connection checks the key strictly.
5. Runs the playbook over SSH as Lima's admin user, with `wardian_resolver` set to Lima's resolver, 192.168.5.2. SSH uses `StrictHostKeyChecking=yes` against that file, no reused connection, and `-F /dev/null`, so your `~/.ssh/config` doesn't apply. `ANSIBLE_CONFIG` points at `ansible/ansible.cfg`, so an `ansible.cfg` in the directory you run from is never read.
6. Pipes the token over the same SSH connection settings into `sudo -n /usr/local/sbin/wardian-install-claude-token`. The script writes it to a temporary file in `/etc/wardian`, sets it to `root:agent 0640`, and renames it to `/etc/wardian/claude-token`, so a failed copy never replaces a good token.

Codex logs in per sandbox. As `agent`, run `codex login --device-auth` and approve the code in a browser.

`test` repeats the `_config` check, then connects the same way and runs the control tests as root:

```sh
sudo -n /usr/bin/python3 -I /usr/local/lib/wardian/control_tests.py --env lima
```

Root sets up T06's and T12's fixtures, reads the firewall's counters, and runs the T06 probes that must fail for root too. Every other probe runs as `agent` with an empty environment, through `runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin`.

It prints `PASS`, `FAIL` or `SKIP` for each threat ID and exits 1 if any test fails or none passes.

`destroy` runs `limactl delete --force` and removes the known-hosts file. It refuses to delete a Lima instance that `create` didn't make: `create` writes a `wardian-sandbox` file into the instance directory right after `limactl create`. It also refuses, naming each reason, while the sandbox's repo holds work the Mac doesn't have:

- uncommitted changes to tracked files, including a submodule with changes
- untracked files (files the repo's `.gitignore` ignores don't count)
- a stash
- any tag
- a branch, or a detached `HEAD`, whose commit no ref in the Mac repo contains

Stop the agent sessions first: anything an agent writes after the check is lost. The Mac repo is `--repo`, or the current directory without it. `destroy` reads the sandbox's repo over SSH, so the sandbox must be running. It refuses a stopped one. Start it with `limactl start <name>`, or see [Discarding a sandbox by hand](#discarding-a-sandbox-by-hand).

The CLI strips control characters from everything limactl, ansible-playbook, ssh and the guest print.

## Git

> Replaced by #28. `wardian push` and `wardian fetch` will do this setup and the fetch for you, connect only to the sandbox whatever your git config says, and print each branch's tip commit and the changed files Mac tools run on their own.

The sandbox holds one repo, `/home/agent/project`, owned by `agent`. Code moves in and out only by git over SSH. Git connects as Lima's admin user, and `sudo` runs `git-receive-pack` and `git-upload-pack` as `agent`. The admin user never runs git in the repo: `agent` can write its config and hooks, and a hook would run as whoever runs git, and the admin user has sudo.

Set up once on the Mac. Add this to `~/.ssh/config`, above any `Host *` block, since ssh keeps the first value it reads for each option:

```
Host wardian
    HostName 127.0.0.1
    Port 60022
    User lima
    IdentityFile ~/.lima/_config/user
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    HostKeyAlias wardian
    UserKnownHostsFile ~/.ssh/known_hosts.d/wardian
    GlobalKnownHostsFile /dev/null
    ForwardAgent no
    ControlMaster no
    ControlPath none
```

These are the settings `wardian` itself uses. For a sandbox with another name, change `wardian` in `Host`, `HostKeyAlias` and the known-hosts file name. If you set `$LIMA_HOME`, the key is in `$LIMA_HOME/_config/user`.

Then, in the Mac repo:

```sh
git remote add --no-tags sandbox wardian:/home/agent/project
git config remote.sandbox.fetch '+refs/heads/agent/*:refs/remotes/sandbox/agent/*'
git config remote.sandbox.receivepack 'cd / && sudo -n -H -u agent git-receive-pack'
git config remote.sandbox.uploadpack 'cd / && sudo -n -H -u agent git-upload-pack'
```

`cd /` is there because `sudo` keeps the admin's home as the working directory, and `agent` can't enter it.

Push the base commit with `git push sandbox main`. The repo's `HEAD` is `main`, and `receive.denyCurrentBranch` is `updateInstead`, so the push updates the checked-out files. If `agent` has uncommitted changes there, git refuses the push with `Working directory has unstaged changes`, and nothing changes. A push to a branch that isn't checked out moves only the branch.

The agents commit on `agent/<task>` branches. Fetch them into `sandbox/agent/*`:

```sh
( set -o pipefail
  git -c fetch.fsckObjects=true fetch --no-tags --no-recurse-submodules \
    sandbox '+refs/heads/agent/*:refs/remotes/sandbox/agent/*' 2>&1 \
    | LC_ALL=C tr -cd '\011\012\040-\176' )
```

- The refspec names `agent/*` only, so no other branch comes back.
- `--no-tags` fetches no tags, and `--no-recurse-submodules` fetches no submodules.
- `fetch.fsckObjects` checks every object received and rejects a malformed one. It's set on the command line because it beats `transfer.fsckObjects` and any `fetch.fsckObjects` in your own config.
- `tr` keeps only tab, newline and printable ASCII, because the spike showed escape sequences in git's `remote:` lines reach the terminal. `pipefail` keeps git's exit status.

Read the diff, for example `git diff main sandbox/agent/<task>`, and merge the commit you read by its SHA, not by the branch name. Run the agent's code in the sandbox, not on the Mac.

## Discarding a sandbox by hand

`destroy` can't check a sandbox it can't reach, such as one the firewall-failure poweroff stopped, and it refuses. To delete one anyway:

```sh
limactl delete --force wardian
rm -f ~/.ssh/known_hosts.d/wardian
```

**Any work in the sandbox that you haven't fetched is lost.** If the VM still boots, `limactl start wardian` and a fetch first.

## Control tests

`ansible/roles/control_tests/files/control_tests.py` is one standard-library Python file. Its `THREAT_IDS` table gives each threat ID a description and where it must run: `ci`, `lima` or `both`. A threat ID that applies to the environment but has no test fails the run, so deleting a test can't make the suite pass. The playbook installs the file as `root:root 0644` under `/usr/local/lib/wardian`.

| Threat ID | Runs in | Checks |
|---|---|---|
| T01 | both | `sudo -n -l` as `agent` succeeds and lists exactly one grant, `(root) NOPASSWD: /usr/local/sbin/wardian-apt`. Any other grant fails the test. |
| T06 | both | Neither `agent` nor root can reach the fixture, and the drop counters go up. The fixture is a listener on port 8080 in its own network namespace, behind a veth pair, at `172.31.255.2` and `fd77:6172:6469::2`. The probes are curl to each address, and to `wardian-fixture.test`, a name that resolves to `172.31.255.2` through an `/etc/hosts` mounted in a private mount namespace. Before the probes, the test checks the fixture answers inside its own namespace. Each probe must fail and raise `private4_dropped` or `ipv6_dropped`. |
| T07 | both | `find -writable` as `agent` finds nothing in `/opt/codex`, `/etc/claude-code`, `/etc/codex`, `/etc/wardian` or `/var/lib/wardian`, and none of `/usr/bin/claude`, `/usr/local/bin/codex`, the managed config and instruction files, the token, the install script, `wardian-apt`, its worker in `/usr/local/libexec`, `/etc/sudoers.d`, the two login profile scripts, `/etc/environment` or their directories is writable. Every path must exist. `agent` can't stat files in `/etc/sudoers.d`, which is `0750` on Ubuntu, so T01 checks the grant instead. |
| T09 | both | `agent`'s login environment has no proxy variable, in either case, and no variable starting with `AWS_`, `GOOGLE_`, `GEMINI_`, `GCLOUD_`, `CLOUDSDK_`, `ANTHROPIC_` or `OPENAI_`. One probe is `su --login` with `env` as the shell, which runs PAM and so reads `/etc/environment`. The other sources `/etc/profile` without `agent`'s dotfiles. Each must find `WARDIAN_SANDBOX=1`, so a probe that read nothing fails, and the second must also find `CLAUDE_CODE_OAUTH_TOKEN`. #27 makes the token check follow the agent mode. |
| T10 | both | `/etc/wardian/claude-token` is `root:agent 0640`, `agent` is the only member of the `agent` group, and `cat` of the token as `nobody` gets `Permission denied`. #27 makes the test follow the agent mode. |
| T11 | both | As `agent`, with no proxy variables, curl downloads `https://pypi.org/simple/pip/` directly and gets HTTP 200. |
| T12 | both | As `agent`, `sudo -n wardian-apt` exits 2 for options (`-y`, `--reinstall`, `-o...`), paths, `.deb` files, names ending in `+` or `-`, a stray `--` and no names. `wardian-apt z.h`, a valid name that apt-get would otherwise treat as a regular expression matching `zsh`, fails with `Unable to locate package`. `wardian-apt jq` exits 0 and leaves `jq` installed. If `jq` is already installed, the test leaves it alone, because once the archive has a newer `jq`, `wardian-apt` refuses the upgrade. The test never removes a package. |
| T13 | ci | With a fake `jq` 0~wardian installed through `dpkg`, `wardian-apt jq` would upgrade it, so it exits 3 and says to recreate the sandbox. The test then purges `jq`, which is why it runs only in CI. |

T02 (no DNS), T03, T04, T05 (Squid) and T08 (no update host) were retired by #26.

`--only <ID>`, repeatable, runs only those threat IDs and reports the rest as skipped. The mutation job uses it. `wardian test` never passes it.

## Egress

Every user in the guest reaches public IPv4 addresses directly, with DNS. No user reaches a private, link-local, CGNAT, metadata, multicast or reserved IPv4 address, or any IPv6 address other than loopback, so nothing in the guest reaches the Mac or the LAN.

The nftables ruleset in `/etc/nftables.conf` replaces only `table inet wardian`, never Lima's `ip nat` table. The output chain's policy is drop. In order:

1. Established and related traffic.
2. Traffic out of `lo`, for every user.
3. DNS, TCP and UDP port 53, to the resolver.
4. DHCP renewals from `systemd-network` to the resolver.
5. All other IPv6 is dropped.
6. Any IPv4 destination in the `private4` set is dropped: RFC 1918, loopback, link-local (including 169.254.169.254), CGNAT, multicast and reserved ranges. That covers the resolver's other ports and the Mac's LAN address.
7. Everything else is accepted.

Drops go through one rate-limited log rule with the prefix `wardian-drop: ` and the UID, then a counter and drop rule with no limit. The named counters `ipv6_dropped` and `private4_dropped` show which rule dropped a packet, and `dropped` counts them all: `sudo nft list counters table inet wardian`.

The only resolver is `wardian_resolver`, written to a static `/etc/resolv.conf`. systemd-resolved and its sockets are masked.

`/etc/environment` holds no proxy variables. It and `/etc/profile.d/wardian-sandbox.sh` set `WARDIAN_SANDBOX=1`. Lima keeps its own lines in `/etc/environment`.

## Packages

`agent` installs system packages with `sudo wardian-apt <package>...`. Its only sudoers line is `agent ALL=(root) NOPASSWD: /usr/local/sbin/wardian-apt`, in `/etc/sudoers.d/wardian-apt`, with sudo's default `env_reset`.

`/usr/local/sbin/wardian-apt` is a root-owned Python front end. It accepts only names matching `^[a-z0-9][a-z0-9.+-]*[a-z0-9]$`: no options, paths, `.deb` files or repos. A name that ends in `+` is refused too, so `g++` comes in through `build-essential`. It then starts `/usr/local/libexec/wardian-apt-worker` with `systemd-run` as a transient service, `wardian-apt-<random>`, waits for it, prints its log and exits with its code. The log and the exit code go to root-only files in `/run/wardian-apt`, and the caller sees the output only at the end.

The worker does the work outside `sudo` and the caller's session. Once it has started, nothing the caller does reaches it: signals sent through `sudo` (Ubuntu 26.04's sudo-rs turns `SIGALRM` into `SIGKILL` for the command), a closed terminal, or output nobody reads. The worker:

1. Runs from `/` with `umask 022`, takes an exclusive `flock` on `/run/wardian-apt.lock`, and gives every `apt-get` call a fixed environment (`PATH`, `HOME=/root`, `LC_ALL=C.UTF-8`, `DEBIAN_FRONTEND=noninteractive`), `-o DPkg::Lock::Timeout=120` and `-o APT::Cmd::Pattern-Only=true`. Without Pattern-Only, apt-get treats a name no package has, such as `a.*b`, as a regular expression and installs every package it matches.
2. Runs `apt-get update`, then `apt-get -s install --no-install-recommends -- <names>`. If the simulated transaction would remove, upgrade, downgrade or reinstall an installed package, it exits 3 and says to fetch the work and recreate the sandbox.
3. Runs `apt-get install -y --no-remove --no-install-recommends -o Dpkg::Options::=--force-confold -- <names>`.
4. Whatever happened after it took the lock, checks that `nft -s list table inet wardian` matches `/var/lib/wardian/inet-wardian.nft` and that `/etc/nftables.conf` matches the SHA-256 in `/var/lib/wardian/nftables.conf.sha256`. On a mismatch, or if it can't check, it runs `systemctl poweroff` and exits 4.

The sources are the ones provisioning configured: Ubuntu's archive and Anthropic's signed Claude Code repo. Background apt jobs, unattended-upgrades, snapd and packagekit are off, and provisioning runs a full upgrade before the firewall starts, because an nftables package upgrade can restart the service, and its `ExecStop` flushes every table, Lima's included. Claude Code is installed after that and held. So a new package rarely needs a newer version of one already installed. When it does, `wardian-apt` refuses.

## Agent CLIs

Root owns both CLIs and their config, and the managed config turns their updates off. `agent` can still install its own copy in its home, so "updates off" is a provisioning choice, not a control.

- Claude Code 2.1.311-1 comes from Anthropic's signed apt repo, stable channel, and is held with `dpkg --set-selections`. The repo's signing key, fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, is kept in the repo at `ansible/roles/agent_clis/files/claude-code.asc`.
- Codex 0.169.0 is the full `codex-package-aarch64-unknown-linux-musl.tar.gz`, checked against GitHub's published SHA-256 and unpacked into `/opt/codex/0.169.0`. `/usr/local/bin/codex` links to `bin/codex` there. The single `codex` binary isn't enough: Codex in T3 needs `codex-code-mode-host`.
- ripgrep comes from apt.
- `/etc/claude-code/managed-settings.json` turns off updates, nonessential traffic and claude.ai connectors. `/etc/codex/managed_config.toml` turns off the update check and ChatGPT apps. Both CLIs apply these files over `agent`'s own settings.
- The same managed instructions go to both agents. They say to install system packages with `sudo wardian-apt`, use user-space installers (uv, pip, npm, mise, rustup) for the rest, commit only on `agent/<task>` branches, and that Docker is unavailable. Claude Code reads them from `/etc/claude-code/CLAUDE.md`. Codex 0.169.0 reads no system-wide `AGENTS.md`, so they go in `additional_developer_instructions` in `/etc/codex/requirements.toml`, which Codex sends as a managed developer message. The text is `ansible/roles/agent_clis/files/wardian-instructions.md`.
- `/etc/profile.d/wardian-claude-token.sh` exports `CLAUDE_CODE_OAUTH_TOKEN` from `/etc/wardian/claude-token` for users who can read it, which means root and the `agent` group.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The unit tests run without a VM. `tests/test_lima_git.py` needs a fresh, running sandbox made by `create`, because its first test pushes an unrelated history to `main`. It pushes into the sandbox's repo, checks each case `destroy` refuses, fetches, and then destroys the sandbox:

```sh
WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_git.py
```

Without `WARDIAN_LIMA_SANDBOX`, those tests are skipped.

## CI

CircleCI runs `.circleci/config.yml` on Arm machine VMs. The `incus` job starts an Ubuntu 26.04 Incus system container, so the firewall rules never touch the CI host, and then:

1. applies the playbook twice over SSH, with `wardian_resolver` set to the Incus bridge, and fails unless the second run reports `changed=0`
2. restarts the container and checks that nftables loaded
3. installs a fake Claude token through `wardian-install-claude-token`, then runs the control tests with `--env ci`, through the same `python3 -I` command as `wardian test`
4. appends a line that isn't nft syntax to `/etc/nftables.conf`, restarts the container, and fails unless it stops within 120 seconds

The `mutation` job runs alongside it in its own container. It applies the playbook once, removes one control at a time from the live ruleset, runs `control_tests.py --env ci --only T06`, and fails unless T06 fails because `agent` reached the fixture:

1. It deletes the private-range drop rule. `agent` must reach `172.31.255.2`. Then it reloads `nftables.service` to put the rule back.
2. It deletes the IPv6 drop rule. `agent` must reach `fd77:6172:6469::2`. Then it reloads again.
3. It inserts an accept rule into `inet wardian` and holds `/run/wardian-apt.lock`, so the worker waits before it installs anything. As `agent`, it starts `sudo wardian-apt jq`, waits for the worker's unit, and sends `SIGALRM` to `sudo`. Root then kills any front end left and checks the worker is still running. Only then does it release the lock, and it fails unless the container stops within 120 seconds.

It deletes rules from the live ruleset rather than breaking `/etc/nftables.conf`, because a ruleset that fails to load powers the container off before the tests run.
