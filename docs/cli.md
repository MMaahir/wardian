# The wardian CLI

`wardian` creates, tests, pushes to, fetches from and destroys a sandbox: a Lima VM built from `lima/wardian.yaml` and provisioned by `ansible/playbook.yml`. So far the playbook:

- creates the unprivileged `agent` user, with the sandbox's own SSH key as its only authorized key
- creates `/home/agent/project`, the git repo the agents work in
- stops and masks the background apt jobs, and removes snapd, packagekit and unattended-upgrades
- writes a static `/etc/resolv.conf` and masks systemd-resolved
- upgrades every installed package, before the firewall starts
- installs the nftables ruleset, with a drop-in that powers the guest off if the ruleset fails to load, and starts SSH only after it (see [Fails closed](#fails-closed))
- limits what an SSH login as `agent` gets to one local forward, to T3's port, and masks the vsock and Unix socket SSH listeners
- installs Claude Code, Codex and ripgrep, with root-owned managed settings and instructions for both CLIs, and holds Claude Code at its version
- installs `wardian-apt`, its sudoers line, and a snapshot of the firewall it checks
- records the agent mode in `/etc/wardian/agent-mode`, and creates `/etc/wardian` for the Claude token, the script that installs the token, and the login profile script that exports it
- sets `WARDIAN_SANDBOX=1` in `/etc/environment` and `/etc/profile.d/wardian-sandbox.sh`
- installs the control tests

> **Being re-scoped.** Issue #1 changed on 2026-11-20 (see [ADR 0001](adr/0001-open-egress-instead-of-an-allowlist.md)). #26 replaced Squid and the allowlist with open egress and `wardian-apt`. #27 gave `agent` its own SSH key, a T3 host block and one login per sandbox. #28 replaced the manual git setup with `wardian push` and `wardian fetch`. This page describes the code on `main`.

## Requirements

- macOS on Apple silicon with Lima 2.3 (`brew install lima`)
- uv. It installs Python 3.13, ruff, pytest and ansible-core into `.venv` from `uv.lock`.
- For a Claude sandbox, a Claude token in your login Keychain, under the service name `wardian-claude-token`. A Codex sandbox doesn't need it. Make one with `claude setup-token`, then store it with `security add-generic-password -U -a "$USER" -s wardian-claude-token -w`. With `-w` last and no value, `security` prompts for the token, so it never lands in your shell history.

Run the CLI with `uv run` from the repo, so `ansible-playbook` from `.venv` is on `PATH`. The CLI itself uses only the Python standard library.

## Commands

Each command takes an optional sandbox name, which defaults to `wardian`. A name is 1 to 31 characters: a lowercase letter, then lowercase letters, digits or `-`. Only one sandbox runs at a time, because the template fixes the SSH port at 60022. You can keep stopped sandboxes, one per project, and start the one you need with `limactl start <name>`.

```sh
uv run wardian create --agent claude   # build, check, boot and provision
uv run wardian test      # run the control tests and report each threat ID
uv run wardian push      # send the current repo's HEAD to the sandbox's main
uv run wardian fetch     # bring back the agent's branches and list files to read
uv run wardian destroy --repo ~/Developer/myproject   # check for unfetched work, then delete
```

`create --agent claude|codex` needs `--agent`. The sandbox gets the login of that agent only. Both CLIs are installed either way.

1. Refuses if `$LIMA_HOME/_config/default.yaml`, `override.yaml` or `base.yaml` exists (`$LIMA_HOME` defaults to `~/.lima`). Lima merges those files into every instance.
2. In Claude mode, reads the Claude token from the Keychain with `security find-generic-password -s wardian-claude-token -w`, and refuses unless it starts with `sk-ant-oat`. The token stays in the CLI's memory and is never written to a file on the Mac. A missing token stops `create` before it builds a VM. Codex mode skips this step.
3. Runs `limactl create --tty=false` without starting the VM, then checks Lima's merged config from `limactl list --json`. It refuses if `mounts`, `networks` or `provision` is set, if `networkOpts.hostLoopback`, `propagateProxyEnv`, `ssh.forwardAgent` or `ssh.overVsock` is anything but false, if `portForwards` is anything but the template's catch-all ignore rule, or if Lima's admin user would be named `agent`. A refused VM never boots, and `create` deletes it.
4. Generates a new ed25519 key for `agent` at `~/.wardian/<name>/agent_ed25519`, mode 0600, in a 0700 directory, replacing any key left there by an earlier sandbox of the same name.
5. Starts the VM, reads its ed25519 host key with `limactl shell`, and writes `~/.ssh/known_hosts.d/<name>`. The entry is keyed by the host alias `<name>`, not by `127.0.0.1:60022`. Lima itself pins the key the guest presents on first boot, so the trust starts there. From this file on, every Wardian SSH connection checks the key strictly.
6. Runs the playbook over SSH as Lima's admin user, with `wardian_resolver` set to Lima's resolver, 192.168.5.2, `wardian_agent_mode` set to the agent, and `wardian_agent_key` set to the public half of the agent key. SSH uses `StrictHostKeyChecking=yes` against that file, no reused connection, and `-F /dev/null`, so your `~/.ssh/config` doesn't apply. `ANSIBLE_CONFIG` points at `ansible/ansible.cfg`, so an `ansible.cfg` in the directory you run from is never read.
7. In Claude mode, pipes the token over the same SSH connection settings into `sudo -n /usr/local/sbin/wardian-install-claude-token`. The script writes it to a temporary file in `/etc/wardian`, sets it to `root:agent 0640`, and renames it to `/etc/wardian/claude-token`, so a failed copy never replaces a good token.
8. Writes the `wardian-<name>` host block to `~/.wardian/ssh_config` and prints the line that includes it. See [SSH as agent](#ssh-as-agent).
9. Prints two notices. `html_render` pages from the agent run in the T3 app on your Mac, and while the thread is open they can make your Mac send requests to its own localhost and LAN. Wardian can't stop that. And the agent's account must have no connectors linked, and for Codex no Codex Cloud environments or tasks. Wardian can't check either.

Codex logs in per sandbox, with device auth. After adding the `Include` line, run `ssh -t wardian-<name> codex login --device-auth` and approve the code in a browser.

`test` repeats the `_config` check, then connects the same way and runs the control tests as root:

```sh
sudo -n /usr/bin/python3 -I /usr/local/lib/wardian/control_tests.py --env lima
```

Root sets up T06's network fixture and, in CI, T13's fake `jq` package, reads the firewall's counters, runs the T06 probes that must fail for root too, and checks who owns T07's paths. T10 tries to read the token as `nobody` and must get `Permission denied`. Every other probe runs as `agent` with an empty environment, through `runuser -u agent -- env -i HOME=/home/agent PATH=/usr/bin:/bin`.

It prints `PASS`, `FAIL` or `SKIP` for each threat ID and exits 1 if any test fails or none passes.

`destroy` runs `limactl delete --force`, removes the known-hosts file and the agent key, and removes the sandbox's host block from `~/.wardian/ssh_config`. It refuses to delete a Lima instance that `create` didn't make: `create` writes a `wardian-sandbox` file into the instance directory right after `limactl create`. It also refuses, naming each reason, while the sandbox's repo holds work the Mac doesn't have:

- uncommitted changes to tracked files, including a submodule with changes
- untracked files (files the repo's `.gitignore` ignores don't count)
- a stash
- any tag
- a branch, or a detached `HEAD`, whose commit no ref in the Mac repo contains

Stop the agent sessions first: anything an agent writes after the check is lost. The Mac repo is `--repo`, or the current directory without it. `destroy` reads the sandbox's repo over SSH, so the sandbox must be running. It refuses a stopped one. Start it with `limactl start <name>`, or see [Discarding a sandbox by hand](#discarding-a-sandbox-by-hand).

The CLI strips control characters from everything limactl, ansible-playbook, ssh and the guest print.

## Git

The sandbox holds one repo, `/home/agent/project`, owned by `agent`. Code moves in and out only by git over SSH, with `wardian push` and `wardian fetch`. Run both from the Mac repo, or pass `--repo`. Push from a repo that holds only this project: git's transfer protocol isn't designed to hide other objects in the repo from the other side (see "Security" in `git help fetch`).

```sh
uv run wardian push --repo ~/Developer/myproject    # send HEAD to the sandbox's main
uv run wardian fetch --repo ~/Developer/myproject   # bring back agent/* branches
```

Both connect only to the sandbox, whatever your git config says:

- The URL is exactly `ssh://<admin>@127.0.0.1:60022/home/agent/project`, with no configured remote.
- `GIT_SSH_COMMAND` is the CLI's own ssh command: `-F /dev/null`, Lima's admin identity file, and the strict host-key options from [Commands](#commands). Your `~/.ssh/config` and the `wardian-<name>` host don't apply.
- Git connects as Lima's admin user, and runs `cd / && sudo -n -H -u agent git-receive-pack` or `git-upload-pack`, so only `agent` runs git in the repo. The admin user never does: `agent` can write the repo's config and hooks, and a hook runs as whoever runs git.
- Both refuse if any git config scope, read with `git config --get-regexp` in the Mac repo with `GIT_CONFIG` unset, has a `url.<base>.insteadOf` or `pushInsteadOf` value that is a prefix of the sandbox URL, or any `remote.<sandbox URL>.*` key. Each would send the URL somewhere else. Rewrites of other URLs, such as GitHub's, don't matter.
- Every git command the CLI runs on the Mac has `-c core.hooksPath=/dev/null`, so no hook in the Mac repo runs, not even for `update-ref`.
- They refuse a Mac git older than 2.42.

`push` runs `git -c push.negotiate=false push --no-recurse-submodules --no-follow-tags <URL> <HEAD's commit>:refs/heads/main`, with no force. `push.negotiate=false` matters because negotiation starts a fetch with git's default `git-upload-pack`, which would run git in the repo as the admin user. The guest repo's `HEAD` is `main` and `receive.denyCurrentBranch` is `updateInstead`, so the push updates the checked-out files. If `agent` has uncommitted changes there, git refuses with `Working directory has unstaged changes` and nothing changes. A push that isn't a fast-forward is refused too. After a push, `push` records the commit as `refs/wardian/<name>/base` in the Mac repo. If the guest has another branch checked out, it says that only `main` moved.

`fetch` runs:

```sh
git -c fetch.bundleURI= -c fetch.fsckObjects=true fetch --prune --no-tags --no-recurse-submodules \
  <URL> '+refs/heads/agent/*:refs/wardian/<name>/agent/*'
```

- The refspec names `agent/*` only, so no other branch comes back, and your worktree and branches don't change. `--prune` deletes the `refs/wardian/<name>/agent/*` ref of a branch the agent deleted, so `fetch` never reports an old tip as current.
- `fetch.bundleURI=` stops git downloading a bundle from a URI in your config. `fetch.fsckObjects` checks every object received and rejects a malformed one. On the command line, both beat your config files.
- `--no-tags` fetches no tags, and `--no-recurse-submodules` fetches no submodules.
- The CLI strips control characters from git's output, because the spike showed escape sequences in git's `remote:` lines reach the terminal.

Then, for each `agent/*` branch, `fetch` prints the branch and its tip SHA, and the changed files Mac tools may run on their own. It compares the tip with its merge-base with `refs/wardian/<name>/base`, or with the empty tree when there's no common commit, using `git diff --name-only -z --no-ext-diff --no-textconv --no-relative`, so no diff driver runs and a `diff.relative` setting can't hide files. A file is listed if any part of its path starts with `.` (`.envrc`, `.vscode/tasks.json`, `.github/workflows/...`), or its name is `package.json`, `pyproject.toml`, `setup.py`, `Makefile`, `justfile`, `Taskfile.yml`, `build.rs` or `Cargo.toml`. The list is a warning, not a block. Control characters, tabs and newlines are stripped from each path.

```text
agent/fix 3f1c2a9e0b7d4c5e6f8a9b0c1d2e3f4a5b6c7d8e
  Changed files Mac tools may run on their own. Read them first:
    .envrc
```

Read the diff, for example `git diff refs/wardian/<name>/base <SHA>`, and merge the SHA `fetch` printed, not the branch name, which the next fetch can move: `git merge <SHA>`. Run the agent's code in the sandbox, not on the Mac.

## SSH as agent

T3 connects to the sandbox as `agent`, through a host block `create` writes to `~/.wardian/ssh_config`:

```
# BEGIN wardian-wardian
Host wardian-wardian
    HostName 127.0.0.1
    Port 60022
    User agent
    IdentityFile "/Users/you/.wardian/wardian/agent_ed25519"
    IdentitiesOnly yes
    ForwardAgent no
    ControlMaster no
    ControlPath none
    StrictHostKeyChecking yes
    HostKeyAlias wardian
    UserKnownHostsFile "/Users/you/.ssh/known_hosts.d/wardian"
    GlobalKnownHostsFile /dev/null
# END wardian-wardian
```

The options after `IdentityFile` are the ones the CLI itself uses, without `BatchMode`. `create` replaces only the block between its own markers, so blocks for other sandboxes stay, and `destroy` removes it. Add this line once, at the top of `~/.ssh/config`, so ssh reads it before any `Host *` block (ssh keeps the first value it reads for each option):

```
Include ~/.wardian/ssh_config
```

Then in T3, add a remote environment with the host `wardian-<name>`.

The key is the sandbox's own: `create` makes a new one for each sandbox, and provisioning installs its public half as the only line of `/home/agent/.ssh/authorized_keys`. `agent` owns that file and can add keys to it, which gives it nothing, because sshd limits every `agent` login the same way. `/etc/ssh/sshd_config.d/50-wardian-agent.conf` holds:

```
Match User agent
	AllowTcpForwarding local
	PermitOpen 127.0.0.1:3773
	PermitListen none
	AllowAgentForwarding no
	AllowStreamLocalForwarding no
	X11Forwarding no
	PermitTunnel no
```

So an `agent` login gets a shell and one local forward, to T3's port in the guest, and nothing else: no remote forwards, no forwards to other ports or hosts, no agent or Unix socket forwarding, no X11 and no tunnels. sshd opens the forwarded connection as `agent`, so the firewall limits it too. Ubuntu's `sshd_config` includes `sshd_config.d` before its own settings, and sshd keeps the first value it reads, so this block wins for `agent`. Lima's admin user keeps sshd's defaults.

## Fails closed

No SSH session starts before the firewall has loaded, and a firewall that fails to load stops the VM.

- `nftables.service` is enabled, and its drop-in `/etc/systemd/system/nftables.service.d/50-wardian.conf` sets `FailureAction=poweroff`. The unit runs before `network-pre.target`, so at boot a ruleset that fails to load stops the guest before networking or SSH start. The claim covers a failed load only: a masked unit, or a ruleset that loads but is wrong, is caught by the control tests and the mutation job instead. The playbook checks the unit with `systemd-analyze verify nftables.service`.
- `ssh.socket` and `ssh.service` each have a drop-in, `50-wardian-nftables.conf`, with `Requires=nftables.service` and `After=nftables.service`. Ubuntu's SSH is socket-activated, so `ssh.socket` is what listens on port 22.
- The ruleset template is checked with `nft -c -f` before it's written. The playbook reloads `nftables.service` and never restarts it, because the packaged `ExecStop` runs `nft flush ruleset`, which would also remove Lima's `ip nat` table.
- Lima's SSH uses TCP (`ssh.overVsock: false`). systemd's SSH generator still makes `sshd-vsock.socket`, listening on vsock port 22, and `sshd-unix-local.socket`, which the spike found open to every local user. The playbook masks and stops both, so only `ssh.socket`, ordered after the firewall, listens for SSH.

## Discarding a sandbox by hand

`destroy` can't check a sandbox it can't reach, such as one the firewall-failure poweroff stopped, and it refuses. To delete one anyway:

```sh
limactl delete --force wardian
uv run wardian destroy wardian
```

With the instance gone, `destroy` only removes the known-hosts file, the agent key and the host block.

**Any work in the sandbox that you haven't fetched is lost.** After the boot-time poweroff, the VM can't boot until the ruleset loads, so the work can't be fetched. After a `wardian-apt` poweroff, don't start it to fetch: see [Packages](#packages).

## Control tests

`ansible/roles/control_tests/files/control_tests.py` is one standard-library Python file. Its `THREAT_IDS` table gives each threat ID a description and where it must run: `ci`, `lima` or `both`. A threat ID that applies to the environment but has no test fails the run, so deleting a test can't make the suite pass. The playbook installs the file as `root:root 0644` under `/usr/local/lib/wardian`.

| Threat ID | Runs in | Checks |
|---|---|---|
| T01 | both | `sudo -n -l` as `agent` succeeds and lists exactly one grant, `(root) NOPASSWD: /usr/local/sbin/wardian-apt`. Any other grant fails the test. |
| T06 | both | Neither `agent` nor root can reach the fixture, and the drop counters go up. The fixture is a listener on port 8080 in its own network namespace, behind a veth pair, at `172.31.255.2` and `fd77:6172:6469::2`. The probes are curl to each address, and to `wardian-fixture.test`, a name that resolves to `172.31.255.2` through an `/etc/hosts` mounted in a private mount namespace. Before the probes, the test checks the fixture answers inside its own namespace. Each probe must fail and raise `private4_dropped` or `ipv6_dropped`. |
| T07 | both | `find -writable` as `agent` finds nothing in `/opt/codex`, `/etc/claude-code`, `/etc/codex`, `/etc/wardian` or `/var/lib/wardian`, and none of `/usr/bin/claude`, `/usr/local/bin/codex`, the managed config and instruction files, the agent mode file, in Claude mode the token, the install script, `wardian-apt`, its worker in `/usr/local/libexec`, `/etc/sudoers.d`, the two login profile scripts, `/etc/environment`, the agent's sshd config or their directories is writable. Root must own every one of these paths, checked at the target for a symlink, and everything in the five directories, since an owner can `chmod` a read-only file. Every path must exist. `agent` can't stat files in `/etc/sudoers.d`, which is `0750` on Ubuntu, so T01 checks the grant instead. |
| T09 | both | `agent`'s login environment has no proxy variable, in either case, and no variable starting with `AWS_`, `GOOGLE_`, `GEMINI_`, `GCLOUD_`, `CLOUDSDK_`, `ANTHROPIC_` or `OPENAI_`. One probe is `su --login` with `env` as the shell, which runs PAM and so reads `/etc/environment`. The other sources `/etc/profile` without `agent`'s dotfiles. Each must find `WARDIAN_SANDBOX=1`, so a probe that read nothing fails. In Claude mode the second must also find `CLAUDE_CODE_OAUTH_TOKEN`. In Codex mode neither may have it. |
| T10 | both | In Claude mode, `/etc/wardian/claude-token` is `root:agent 0640`, `agent` is the only member of the `agent` group, and `cat` of the token as `nobody` gets `Permission denied`. In Codex mode, the token file doesn't exist. |
| T11 | both | As `agent`, with no proxy variables, curl downloads `https://pypi.org/simple/pip/` directly and gets HTTP 200. |
| T12 | both | As `agent`, `sudo -n wardian-apt` exits 2 for options (`-y`, `--reinstall`, `-o...`), paths, `.deb` files, names ending in `+` or `-`, a stray `--` and no names. `wardian-apt z.h`, a valid name that apt-get would otherwise treat as a regular expression matching `zsh`, fails with `Unable to locate package`. `wardian-apt jq` exits 0 and leaves `jq` installed. If `jq` is already installed, the test leaves it alone, because once the archive has a newer `jq`, `wardian-apt` refuses the upgrade. The test never removes a package. |
| T13 | ci | With a fake `jq` 0~wardian installed through `dpkg`, `wardian-apt jq` would upgrade it, so it exits 3 and says to recreate the sandbox. The test then purges `jq`, which is why it runs only in CI. |
| T14 | both | `sshd -t` passes. `sshd -T -C user=agent,...` shows `allowtcpforwarding local`, `permitopen 127.0.0.1:3773`, `permitlisten none`, and `no` for agent forwarding, Unix socket forwarding, X11 and tunnels. For the admin user (Lima's UID 1000 user in Lima, root in CI), `allowtcpforwarding` is still `yes`, so the `Match` block applies to `agent` only. |
| T15 | both | `systemctl is-enabled nftables.service` is `enabled`, its `FailureAction` is `poweroff`, and `ssh.socket` and `ssh.service` both list `nftables.service` under `Requires` and `After`. |
| T16 | both | `sshd-vsock.socket` and `sshd-unix-local.socket` are masked, and `systemctl list-sockets --all` shows no socket for SSH but `ssh.socket`. |

T07, T09 and T10 read the agent mode, `claude` or `codex`, from the root-owned `/etc/wardian/agent-mode` that provisioning writes. A missing or unknown mode fails them.

T02 (no DNS), T03, T04, T05 (Squid) and T08 (no update host) were retired by #26.

`--only <ID>`, repeatable, runs only those threat IDs and reports the rest as skipped. The mutation job uses it. `wardian test` never passes it.

## Egress

Every user in the guest reaches public IPv4 addresses directly, with DNS. Apart from loopback, DNS to the resolver and DHCP renewals, no user reaches an IPv4 address in the `private4` set listed below, or any IPv6 address, so nothing in the guest reaches the Mac or the LAN.

The nftables ruleset in `/etc/nftables.conf` replaces only `table inet wardian`, never Lima's `ip nat` table. The output chain's policy is drop. In order:

1. Established and related traffic.
2. Traffic out of `lo`, for every user.
3. DNS, TCP and UDP port 53, to the resolver.
4. DHCP renewals from `systemd-network` to the resolver.
5. All other IPv6 is dropped.
6. Any IPv4 destination in the `private4` set is dropped: `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10` (CGNAT), `127.0.0.0/8`, `169.254.0.0/16` (including 169.254.169.254), `172.16.0.0/12`, `192.0.0.0/24`, `192.168.0.0/16`, `198.18.0.0/15`, `224.0.0.0/4` (multicast) and `240.0.0.0/4`. That covers the resolver's other ports and the Mac's LAN address. The documentation ranges `192.0.2.0/24`, `198.51.100.0/24` and `203.0.113.0/24` aren't in the set.
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
4. Whatever happened after it took the lock, checks that `nft -s list table inet wardian` matches `/var/lib/wardian/inet-wardian.nft` and that `/etc/nftables.conf` matches the SHA-256 in `/var/lib/wardian/nftables.conf.sha256`. On a mismatch, or if it can't check, it runs `systemctl poweroff --force` and exits 4. A plain `systemctl poweroff` goes through logind, which refuses while anyone holds a block inhibitor, root included. `--force` asks systemd directly, skipping the orderly stop of services. If that fails, `--force --force` calls `reboot(2)` to power off. The poweroff can stop the worker before it writes its exit code, so the caller may see exit 1 or its SSH connection drop instead of exit 4. The log line `Powering off.` is the sign that the poweroff started. If both calls fail, the worker says so and the sandbox keeps running.

After a `wardian-apt` poweroff, or a failed one, don't start or use the sandbox again, not even to fetch. If `/etc/nftables.conf` changed and still loads, it boots with the changed firewall, and anything `agent` set to run at boot gets that firewall's egress. Delete it as in [Discarding a sandbox by hand](#discarding-a-sandbox-by-hand). Work you haven't fetched is lost.

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

The unit tests run without a VM. `tests/test_cli_git.py` runs `push` and `fetch` through git's real transport against a local repo, with a fake `ssh` that runs the remote command locally. It checks that a matching `insteadOf`, a matching `pushInsteadOf` and a `remote.<URL>.*` key, in the repo's or the global config, each make both commands refuse, that an unrelated GitHub `insteadOf` doesn't, that `fetch` succeeds while a `fetch.bundleURI` listener gets no request, and that no hook in the Mac repo runs. Each control has a mutation test that removes it and shows the push goes elsewhere, the listener gets a request, or a hook runs. `tests/test_lima_agent_ssh.py` checks SSH as `agent` through the `wardian-<name>` host, read from `~/.wardian/ssh_config` with `ssh -F`: `id -un` is `agent`, `-R` and agent forwarding are refused, `-L` and `-W` to `127.0.0.1:3773` reach a test listener, and `-L` to `127.0.0.1:3774`, `192.168.5.2:53` and `1.1.1.1:443` get `administratively prohibited`. It changes nothing in the sandbox.

`tests/test_lima_isolation.py` checks the Mac's view of the sandbox. Each probe that must fail has a check that its target answers when nothing blocks it, or that the firewall counted the drop. A guest probe passes as blocked only when it ran and printed `blocked: <error>`, so broken SSH or a missing `python3` fails the test:

- `findmnt` in the guest shows no `virtiofs`, `9p` or `sshfs` mount.
- A listener `agent` starts on every guest address answers inside the guest. For 15 seconds, the time Lima would take to set up a forward, no connection to that port on the Mac's `127.0.0.1` succeeds.
- A short-lived listener on the Mac's `127.0.0.1` answers on the Mac, but `agent` can't reach it through `192.168.5.2`. Neither can it reach a listener on the Mac's LAN IPv4 address. If the macOS application firewall is on, it may prompt for the LAN listener, so that probe is skipped, not passed.
- `agent` can't reach `169.254.169.254:80`. Nothing may listen there, so a temporary counter-only rule at the top of the `drop_logged` chain, matching only `agent`'s packets to that address and port, must count the probe. The test deletes the rule in `finally`.
- `hostLoopback: false` holds on its own. A temporary rule at the top of the output chain, `meta skuid 0 ip daddr 192.168.5.2 tcp dport <port> counter accept`, lets root past nftables to the Mac listener's port. Root's probe must still fail, with the rule's counter above zero. The test deletes the rule in `finally`.
- With a mount in `_config/override.yaml` under a temporary `$LIMA_HOME`, `create` refuses and makes no VM. The temporary `$LIMA_HOME` keeps other Lima commands from reading the override.

`tests/test_lima_hostloopback.py` is the positive control for the `hostLoopback` probe. It builds a throwaway VM from `lima/wardian.yaml` with only `hostLoopback: true`, never provisions it, and runs the same probe through `limactl shell`, which must connect. Then it deletes the VM. It runs only when no Lima instance is running, because the template fixes the SSH port, and refuses, as `create` does, when a Lima `_config` file exists. It passed on 2026-11-25 with Lima 2.3.0. **Rerun it whenever the Lima version changes:**

```sh
WARDIAN_HOSTLOOPBACK_CONTROL=1 uv run pytest tests/test_lima_hostloopback.py
```

`tests/test_lima_git.py` needs a fresh, running sandbox made by `create`, because its first test pushes an unrelated history to `main`. It pushes into the sandbox's repo, checks each case `destroy` refuses, fetches, and then destroys the sandbox:

```sh
WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_isolation.py tests/test_lima_agent_ssh.py tests/test_lima_git.py
```

The Lima test files carry the `lima` pytest marker, so `uv run pytest -m lima` selects them. Without `WARDIAN_LIMA_SANDBOX`, or `WARDIAN_HOSTLOOPBACK_CONTROL=1` for the control, they are skipped.

## CI

CircleCI runs `.circleci/config.yml` on Arm machine VMs. The `python` job runs `ruff check`, `ruff format --check` and `pytest`, including `tests/test_cli_git.py`. It first checks that the image's git is the version in `GIT_VERSION`, so the git the tests run against changes only when someone bumps the image tag and that value together.

The `incus` job starts an Ubuntu 26.04 Incus system container, so the firewall rules never touch the CI host, and then:

1. applies the playbook twice over SSH in Codex mode, with `wardian_resolver` set to the Incus bridge and an agent key made for the run, and fails unless the second run reports `changed=0`
2. restarts the container and checks that nftables loaded
3. runs the control tests with `--env ci`, through the same `python3 -I` command as `wardian test`
4. applies the playbook again in Claude mode, installs a fake Claude token through `wardian-install-claude-token`, and runs the control tests again
5. appends a line that isn't nft syntax to `/etc/nftables.conf`, restarts the container, and fails unless it stops within 120 seconds

The `mutation` job runs alongside it in its own container. It applies the playbook once, in Codex mode, removes one control at a time from the live ruleset, and checks each change:

1. It deletes the private-range drop rule and runs `control_tests.py --env ci --only T06`, which must fail because `agent` and root reached `172.31.255.2` and the fixture's name. Then it reloads `nftables.service` to put the rule back.
2. It deletes the IPv6 drop rule and runs T06 again, which must fail because `agent` and root reached `fd77:6172:6469::2`. Then it reloads again.
3. It inserts an accept rule into `inet wardian`, holds a block shutdown inhibitor, which makes logind refuse a plain poweroff even for root, and holds `/run/wardian-apt.lock`, so the worker waits before it installs anything. As `agent`, it starts `sudo wardian-apt jq`, waits for the worker's unit, and sends `SIGALRM` to `sudo`. Root then kills any front end left and checks the worker is still running. Only then does it release the lock, and it fails unless the container stops within 120 seconds.

It deletes rules from the live ruleset rather than breaking `/etc/nftables.conf`, because a ruleset that fails to load powers the container off before the tests run.
