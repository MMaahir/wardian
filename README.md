# Wardian

Wardian runs AI coding agents (Claude Code and Codex) in disposable Lima VMs on a Mac, so you can let them work unattended from [T3 Code](https://github.com/pingdotgg/t3code) without worrying about what else on your machine they can reach.

## Why

An agent running in full-access mode on your Mac can read your other projects, your home folder, your SSH keys, your cloud credentials and any API keys in your environment. It can reach services on your Mac and your local network, and it can leave files that your Mac runs later, such as git hooks or an `.envrc`. Permission prompts stop that, but they also stop unattended work.

Wardian puts each project in its own VM (the sandbox). The agent gets the project's code, its own login, and the internet, so it can install packages, run the tests and commit. It doesn't get your Mac.

## What it protects

This section and the next describe the design in issue #1. [Status](#status) says which parts are on `main` today.

- **Your other projects.** The sandbox has no host mounts, and holds only the one repo you push into it. The firewall blocks it from the private addresses of your Mac and your LAN, so it can't reach services listening on them.
- **Your sensitive files.** Nothing from your home folder is shared with the VM. Code comes back only by git, as refs that don't touch your working tree. Wardian lists changed files that Mac tools run on their own, such as dotfiles, `package.json` and `Makefile`, so you see them before you merge.
- **Your credentials.** No SSH agent forwarding, no host API keys and no cloud or forge tokens enter the sandbox. The only credential inside is the chosen agent's own login.

## What it doesn't protect

Wardian is not fool-proof containment. It aims to keep agents away from things they don't need, not to stop a determined attacker.

- The agent can send the project's code and its own login anywhere on the internet.
- The agent can run anything it downloads, inside the VM.
- T3's `html_render` pages run in the Mac app and can make your Mac send requests to its own localhost and LAN while the thread is open. Only T3 can fix this.
- A Mac with a public IPv4 address, or a service your router forwards to it, can be reached, because the firewall blocks private, link-local and metadata IPv4 addresses and all non-loopback IPv6, not public IPv4 addresses.
- A bug in the guest kernel, Lima or the hypervisor could reach your Mac.

The full list of accepted risks is in the spec, [issue #1](https://github.com/MMaahir/wardian/issues/1).

## How it works

This also describes the design in issue #1.

```text
Mac                                    Sandbox (Lima VM)
───                                    ─────────────────
T3 Code ──── SSH as `agent` ─────────▶ T3 server, Claude Code or Codex
your repo ── wardian push / fetch ───▶ /home/agent/project
                                         │
                                         ├─▶ public IPv4: allowed
                                         ├─▶ DNS and DHCP to Lima's gateway: allowed
                                         └─▶ other private, link-local and metadata
                                             IPv4, and non-loopback IPv6: dropped
```

- The VM comes from Wardian's own Lima template: pinned Ubuntu image, no mounts, no port forwarding, no access to the Mac's loopback.
- Agents run as `agent`, an unprivileged user. Their only root access is `sudo wardian-apt <packages>`, which can only install new system packages.
- nftables drops traffic to private, link-local and metadata addresses for every user, apart from DNS and DHCP to Lima's gateway, so even root inside the VM can't reach those addresses without breaking the firewall first.
- Every control has a threat ID and a test that checks it from outside. CI runs the tests on every change, and a mutation job removes controls one at a time to check their tests fail.

[ADR 0001](docs/adr/0001-open-egress-instead-of-an-allowlist.md) explains why agents get the open internet rather than an allowlist.

## Status

Wardian is in development. The design changed on 2026-11-20 to give agents more room (see the ADR), and the code on `main` is catching up:

| Works on `main` | Coming |
| --- | --- |
| `create`, `test` and `destroy` | |
| Open internet with private-range drops, and `sudo wardian-apt` | |
| `wardian push` and `wardian fetch`, with the list of changed files Mac tools run on their own | |
| One agent per sandbox (`--agent claude\|codex`), an `agent` SSH key and a T3 host block | |

## Requirements

- A Mac with Apple silicon and Lima 2.3 (`brew install lima`)
- [uv](https://docs.astral.sh/uv/), which installs Python 3.13 and Ansible into `.venv`
- git 2.42 or later, for `wardian push` and `wardian fetch`
- For a Claude sandbox, a Claude token in your login Keychain under the service name `wardian-claude-token`. `create --agent claude` refuses without it. Make one with `claude setup-token`, then store it with:

  ```sh
  security add-generic-password -U -a "$USER" -s wardian-claude-token -w
  ```

  With `-w` last and no value, `security` prompts for the token, so it never lands in your shell history.
- For Codex, a ChatGPT account. Codex logs in with a device code inside each sandbox.
- No connectors linked on either account, and no Codex Cloud environments or tasks.

A sandbox uses 4 CPUs, 4 GiB of memory and 20 GiB of disk. One runs at a time.

## Usage

Run the CLI from a clone of this repo:

```sh
uv run wardian create --agent claude   # build, check, boot and provision the sandbox
uv run wardian test              # run every control test and report each threat ID
uv run wardian push --repo ~/Developer/myproject    # send HEAD to the sandbox
uv run wardian fetch --repo ~/Developer/myproject   # bring back the agent's branches
uv run wardian destroy --repo ~/Developer/myproject   # refuse if work isn't fetched, then delete
```

Each command takes an optional sandbox name (default `wardian`), so you can keep a stopped sandbox per project and start the one you need with `limactl start <name>`.

A typical session:

1. `wardian create --agent claude` (or `--agent codex`) a sandbox for the project. The first time, add the `Include ~/.wardian/ssh_config` line it prints to the top of `~/.ssh/config`. Read the two notices it prints.
2. `uv run wardian push --repo /path/to/project` sends the project's current commit to the sandbox's `main`. Push from a repo that holds only this project: git's protocol can reveal information about other objects in the repo you push from.
3. In T3 Code, add a remote environment with the host `wardian-<name>`, which connects as `agent`, and give the agent a task. For Codex, log in first with `ssh -t wardian-<name> codex login --device-auth`. Agents work on `agent/<task>` branches.
4. `uv run wardian fetch --repo /path/to/project` brings the agent's branches back as `refs/wardian/<name>/agent/*`, without touching your worktree. It prints each branch's tip SHA and the changed files Mac tools run on their own, such as `.envrc` and `package.json`. Read the diff and merge that SHA.
5. When the work is merged, `uv run wardian destroy --repo /path/to/project`. `--repo` must be the project, not this clone, or the unfetched-work check reads the wrong repo and refuses.

Run the agent's code and tests in the sandbox, not on your Mac, until you've reviewed it.

To see connections the firewall blocked, run `limactl shell <name> sudo journalctl -k --grep wardian-drop`.

[docs/cli.md](docs/cli.md) covers each command in detail, the control tests, the firewall rules, `wardian-apt` and the CI jobs.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The unit tests need no VM. `WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_git.py` runs the git tests against a fresh, running sandbox made by `create`, and destroys it at the end. A sandbox that already holds a project can fail them.

CI runs on CircleCI: the playbook is applied twice inside an Incus container on an Arm VM, the control tests run there, and a mutation job removes controls one at a time and checks their tests fail.

## Docs

- [Issue #1](https://github.com/MMaahir/wardian/issues/1): the spec, threat model and accepted risks
- [docs/cli.md](docs/cli.md): the CLI, control tests, firewall and CI
- [docs/adr/](docs/adr/): design decisions
- [spike/](spike/): the hand-built sandboxes the automation is based on

## License

Apache 2.0. See [LICENSE](LICENSE).
