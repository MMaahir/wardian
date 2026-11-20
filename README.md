# Wardian

Wardian runs AI coding agents (Claude Code and Codex) in disposable Lima VMs on a Mac, so you can let them work unattended from [T3 Code](https://github.com/pingdotgg/t3code) without worrying about what else on your machine they can reach.

## Why

An agent running in full-access mode on your Mac can read your other projects, your home folder, your SSH keys, your cloud credentials and any API keys in your environment. It can reach services on your Mac and your local network, and it can leave files that your Mac runs later, such as git hooks or an `.envrc`. Permission prompts stop that, but they also stop unattended work.

Wardian puts each project in its own VM (the sandbox). The agent gets the project's code, its own login, and the internet, so it can install packages, run the tests and commit. It doesn't get your Mac.

## What it protects

- **Your other projects.** The sandbox has no host mounts, and holds only the one repo you push into it. It can't reach services on your Mac or your LAN.
- **Your sensitive files.** Nothing from your home folder is shared with the VM. Code comes back only by git, as refs that don't touch your working tree. Wardian lists changed files that Mac tools run on their own, such as dotfiles, `package.json` and `Makefile`, so you see them before you merge.
- **Your credentials.** No SSH agent forwarding, no host API keys and no cloud or forge tokens enter the sandbox. The only credential inside is the chosen agent's own login.

## What it doesn't protect

Wardian is not fool-proof containment. It aims to keep agents away from things they don't need, not to stop a determined attacker.

- The agent can send the project's code and its own login anywhere on the internet.
- The agent can run anything it downloads, inside the VM.
- T3's `html_render` pages run in the Mac app and can make your Mac send requests to its own localhost and LAN while the thread is open. Only T3 can fix this.
- A bug in the guest kernel, Lima or the hypervisor could reach your Mac.

The full list of accepted risks is in the spec, [issue #1](https://github.com/MMaahir/wardian/issues/1).

## How it works

This is the design in issue #1. [Status](#status) says which parts are on `main` today.

```text
Mac                                    Sandbox (Lima VM)
───                                    ─────────────────
T3 Code ──── SSH as `agent` ─────────▶ T3 server, Claude Code or Codex
your repo ── wardian push / fetch ───▶ /home/agent/project
                                         │
                                         ├─▶ public internet: allowed
                                         └─▶ Mac, LAN, metadata: dropped by nftables
```

- The VM comes from Wardian's own Lima template: pinned Ubuntu image, no mounts, no port forwarding, no access to the Mac's loopback.
- Agents run as `agent`, an unprivileged user. Their only root access is `sudo wardian-apt <packages>`, which installs new Ubuntu packages and nothing else.
- nftables drops traffic to private, link-local and metadata addresses for every user, so even root inside the VM can't reach your Mac or LAN without breaking the firewall first.
- Every control has a threat ID and a test that fails when the control is removed. CI proves that on every change.

[ADR 0001](docs/adr/0001-open-egress-instead-of-an-allowlist.md) explains why agents get the open internet rather than an allowlist.

## Status

Wardian is in development. The design changed on 2026-11-20 to give agents more room (see the ADR), and the code on `main` is catching up:

| Works on `main` | Coming |
| --- | --- |
| `create`, `test` and `destroy` | Open internet and `wardian-apt` ([#26](https://github.com/MMaahir/wardian/issues/26)). Until then, agents reach only their own APIs, through a proxy |
| Git in and out, with a manual setup | `wardian push` and `wardian fetch` ([#28](https://github.com/MMaahir/wardian/issues/28)) |
| Claude token from the Keychain | An `agent` SSH key and a T3 host block, and one agent per sandbox ([#27](https://github.com/MMaahir/wardian/issues/27)). T3 can't connect to an automated sandbox until this lands |

## Requirements

- A Mac with Apple silicon and Lima 2.3 (`brew install lima`)
- [uv](https://docs.astral.sh/uv/), which installs Python 3.13 and Ansible into `.venv`
- git 2.42 or later, for `wardian fetch`
- For Claude: a Claude token in your login Keychain under the service name `wardian-claude-token`. Make one with `claude setup-token`, then store it with:

  ```sh
  security add-generic-password -U -a "$USER" -s wardian-claude-token -w
  ```

  With `-w` last and no value, `security` prompts for the token, so it never lands in your shell history.
- For Codex: a ChatGPT account. Codex logs in with a device code inside each sandbox.
- No connectors linked on either account, and no Codex Cloud environments or tasks.

A sandbox uses 4 CPUs, 4 GiB of memory and 20 GiB of disk. One runs at a time.

## Usage

Run the CLI from a clone of this repo:

```sh
uv run wardian create            # build, check, boot and provision the sandbox
uv run wardian test              # run every control test and report each threat ID
uv run wardian destroy --repo ~/Developer/myproject   # refuse if work isn't fetched, then delete
```

Each command takes an optional sandbox name (default `wardian`), so you can keep a stopped sandbox per project and start the one you need with `limactl start <name>`.

A typical session:

1. `wardian create` a sandbox for the project.
2. Push your project's current commit into it. Until #28, this is the manual git setup in [docs/cli.md](docs/cli.md#git).
3. Connect T3 Code to the sandbox and give the agent a task. Agents work on `agent/<task>` branches.
4. Fetch the agent's branches back to your Mac, read the diff and the list of auto-run files, and merge the commit you read by its SHA.
5. `wardian destroy` the sandbox when the work is merged.

Run the agent's code and tests in the sandbox, not on your Mac, until you've reviewed it.

[docs/cli.md](docs/cli.md) covers each command in detail, the control tests, the firewall rules and the CI jobs.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The unit tests need no VM. `WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_git.py` runs the git tests against a real sandbox and destroys it at the end.

CI runs on CircleCI: the playbook is applied twice inside an Incus container on an Arm VM, the control tests run there, and a mutation job removes each control and checks its test fails.

## Docs

- [Issue #1](https://github.com/MMaahir/wardian/issues/1): the spec, threat model and accepted risks
- [docs/cli.md](docs/cli.md): the CLI, control tests, firewall and CI
- [docs/adr/](docs/adr/): design decisions
- [spike/](spike/): the hand-built sandboxes the automation is based on

## License

Apache 2.0. See [LICENSE](LICENSE).
