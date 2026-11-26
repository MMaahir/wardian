# Threat model

This is the threat model for Wardian v1, as specified in [issue #1](https://github.com/MMaahir/wardian/issues/1). It says what Wardian protects, from whom, which control stops each threat, and which test proves the control is there. [docs/cli.md](cli.md) has the details of each control and test.

## Protected assets

All three are on the Mac:

1. **Other projects.** Other repos on disk, services they run on the Mac or the LAN, and other repos reachable through the developer's AI accounts.
2. **Sensitive host files.** The home folder, `~/.ssh`, `~/.aws`, browser profiles, dotfiles, and files Mac tools run on their own: git hooks, `.envrc`, editor tasks and agent config.
3. **Host credentials.** Environment API keys, the SSH agent, cloud and forge tokens, and the Keychain.

## Adversary

An over-eager or prompt-injected agent that runs any code it likes in the guest as `agent`, including code from dependencies it installs.

## Non-goals

Wardian doesn't try to prevent exfiltration or DNS tunnels, allowlist egress, keep hostname-level or forensic logs, or defend against guest kernel or hypervisor escapes. Anything put in the guest on purpose, such as the pushed git history and the agent's own login, can leave.

## Design rules

- Every arrow points away from the sandbox. Nothing, such as CI or a webhook, pushes into a sandbox, and the Mac pulls everything it needs.
- No forge or cloud credentials ever enter a sandbox.
- One running sandbox at a time, because the SSH port is fixed. Stopped sandboxes may be kept, one per project.
- Root never runs, sources or writes through files the agent can write. The playbook runs only on a fresh VM, and a used sandbox is changed by destroy and recreate. `wardian-apt` is the one root path the agent can start, and it takes only package names.
- The assistant writes and maintains the docs. Code and configuration are agreed per ticket: the developer writes them, or hands the ticket to the assistant end to end.

The spec also asks that every control's test checks it from outside, the way an attacker would try it, and that every negative probe has a positive check, a fixture that answers or a counter that must rise, so the test fails when the control is removed. The tables below say where a control has no dedicated test.

## Where the tests run

- **Control tests** (threat IDs `T01` to `T16`) run inside a provisioned guest, from `ansible/roles/control_tests/files/control_tests.py`. Each ID is tagged `ci` (the Incus container in CircleCI), `lima` (a real sandbox, through `wardian test`) or `both`. A mandatory ID that is skipped or missing fails the run.
- **Mac-side tests** (`tests/test_lima_*.py`, pytest marker `lima`) look at a real sandbox from the Mac.
- **CLI tests** (`tests/test_cli.py`, `tests/test_cli_git.py`) run without a VM, in CI and on the Mac.
- **CI jobs** in `.circleci/config.yml`: the `incus` job also restarts the container with a broken ruleset and checks it stops, and the `mutation` job removes controls one at a time and requires their tests to fail.

## Threats in the guest

| Threat | Asset | Control | Test | Runs in |
|---|---|---|---|---|
| The agent becomes root and changes the firewall. | all | `agent` is in no group but its own and has no subordinate IDs. Its only sudo grant is `wardian-apt`. | T01 | both |
| The agent, or root, connects to the Mac, the LAN, the gateway beyond DNS and DHCP, or the metadata address. | 1 | nftables' output chain has a drop policy. After accepting established and related traffic, loopback, DNS to the resolver and DHCP renewals from `systemd-network`, it drops new connections to private, link-local, CGNAT, metadata and reserved IPv4 and all non-loopback IPv6, for every user, and accepts the rest. Drops are logged with a rate limit and counted. | T06; the `mutation` job removes each drop rule and requires T06 to fail | both |
| The agent replaces an agent CLI or its managed config, turning account connectors on to reach other repos. | 1 | Root owns both CLIs, their managed settings and instructions, the mode file and the token. | T07 | both |
| A host API key or cloud credential is in the agent's environment. | 3 | No host variables are copied in. Only the chosen agent's login is present. | T09 | both |
| Another local user reads the Claude token. | the agent's login | The token file is `root:agent 0640`, and a Codex sandbox has none. | T10 | both |
| Open egress stops working, so agents can't install what a project needs. | none (capability) | No proxy. The agent reaches public hosts directly. | T11 | both |
| The agent uses `wardian-apt` to run options, paths, `.deb` files or other repos as root. | all | `wardian-apt` accepts only package names, installs only new packages from the configured archives, and checks the firewall afterwards. | T12 | both |
| `wardian-apt` upgrades an installed package. | all | The simulated transaction must add packages only, or it refuses. | T13 | ci |
| A package's maintainer script changes the firewall during an install. | 1 | After the install, `wardian-apt` compares `inet wardian` with the provisioning snapshot and `/etc/nftables.conf` with its hash. On a mismatch, the VM ends up stopped. If both poweroff attempts fail, the worker says so and says to delete the sandbox. | the `mutation` job's `wardian-apt powers off after the firewall changes` step, which kills the caller first and checks the worker still runs the firewall check and stops the container | ci |
| The agent uses SSH forwarding to reach the Mac or the LAN. | 1 | sshd gives `agent` one local forward, to `127.0.0.1:3773`, and no remote, agent, socket or X11 forwarding or tunnels. | T14; `tests/test_lima_agent_ssh.py` | both; lima |
| On a provisioned sandbox's boot, an SSH session runs before the firewall has loaded, or with a ruleset that failed to load. | all | SSH requires and starts after `nftables.service`, which has `FailureAction=poweroff`. When the ruleset fails to load, the VM ends up stopped. The first boot starts with only Lima's admin login, which `create` uses to run the playbook. The playbook adds the `agent` login and then installs the firewall and this ordering. `create` writes T3's host block only after provisioning. | T15; the `incus` job's `A ruleset that fails to load powers the container off` step | both; ci |
| The agent reaches SSH through a listener that isn't ordered after the firewall. | all | `sshd-vsock.socket` and `sshd-unix-local.socket` are masked and stopped. | T16 | both |

"The VM ends up stopped" is the claim for both poweroffs. It doesn't mean no packet ever leaves: a maintainer script runs before `wardian-apt` checks the firewall, and at boot the check covers a ruleset that fails to load, not a masked unit or a ruleset that loads but is wrong. The control tests and the mutation job cover those.

## Threats from the guest to the Mac

These tests run from the Mac against a real sandbox (`lima`), in `tests/test_lima_isolation.py` unless noted. `tests/test_cli.py` runs in CI and on the Mac.

| Threat | Asset | Control | Test |
|---|---|---|---|
| The agent reads or writes Mac files through a shared folder. | 2 | The template has no mounts, and `create` refuses a merged config with any. | `test_no_host_mounts` |
| Lima forwards a service the agent starts to the Mac, so Mac tools talk to agent code. | 2 | Lima's automatic port forwarding is off, with a catch-all ignore rule. Separately, an `agent` SSH login may open local forwards only to `127.0.0.1:3773`, T3's port (T14). | `test_a_listener_agent_starts_is_unreachable_from_the_mac` |
| The agent reaches services on the Mac's loopback through the gateway. | 1 | `hostLoopback: false`, and nftables drops `192.168.5.2` beyond DNS. | `test_guest_cant_reach_the_macs_loopback_through_the_gateway` |
| `hostLoopback: false` fails while nftables still hides it. | 1 | Lima's `hostLoopback: false`, tested on its own with a temporary root-only accept rule. | `test_host_loopback_off_holds_on_its_own`; the positive control `tests/test_lima_hostloopback.py`, rerun when the Lima version changes |
| The agent reaches services on the Mac's LAN address. | 1 | nftables drops private IPv4. | `test_guest_cant_reach_the_macs_lan_address` (skipped, not passed, if the macOS application firewall prompts) |
| The agent reaches the metadata address. | 1 | nftables drops link-local IPv4. | `test_guest_cant_reach_the_metadata_address` |
| A Lima config file on the Mac adds a mount or another unsafe setting. | 2, 3 | `create` refuses if `_config/default.yaml`, `override.yaml` or `base.yaml` exists, and checks Lima's merged config before the VM boots. | `test_create_refuses_a_lima_override`; `tests/test_cli.py` (`test_refuses_lima_global_config`, `test_bad_config_is_refused`) |

## Threats through the CLI

These controls run on the Mac, in the `wardian` CLI. `tests/test_cli.py` and `tests/test_cli_git.py` need no VM and run in CI's `python` job and on the Mac. `tests/test_lima_*.py` need a real sandbox and run on the Mac only.

| Threat | Asset | Control | Test |
|---|---|---|---|
| The SSH agent or the Mac's keys reach the guest. | 3 | The template turns agent forwarding off and doesn't load `~/.ssh/*.pub`. T3's host block and the CLI set `IdentitiesOnly yes` and `ForwardAgent no`, with a per-sandbox key. | `test_host_block_uses_agent_and_the_cli_ssh_options`, `test_agent_key_is_new_and_private` (`tests/test_cli.py`); `test_agent_forwarding_is_refused` (`tests/test_lima_agent_ssh.py`) |
| The Claude token lands in a Mac file or the shell history. | 3 | `create` reads it from the Keychain and pipes it into the guest. | `tests/test_cli.py` token tests |
| The Mac connects to something other than the sandbox it built. | 3 | `create` reads the guest's host key through `limactl` on first boot and writes a per-sandbox known-hosts file. From then on, the CLI's connections and T3's, through the `wardian-<name>` host once `Include ~/.wardian/ssh_config` is at the top of `~/.ssh/config`, as `create` says, check it strictly. An earlier matching block could otherwise override those options. | configuration checks: `test_git_ssh_command_ignores_the_users_ssh_config` (`tests/test_cli_git.py`) and `test_host_block_uses_agent_and_the_cli_ssh_options` (`tests/test_cli.py`) check the SSH options |
| Git config sends `push` or `fetch` somewhere else. | 1 | Both use an exact URL and refuse a matching `insteadOf`, `pushInsteadOf` or `remote.<URL>.*` key in any scope. | `test_refuses_config_that_redirects_the_sandbox`, with its mutation test (`tests/test_cli_git.py`) |
| Git in the guest runs as Lima's admin, under config `agent` can write. | 1, 3 | Git connects as Lima's admin, which runs only `cd / && sudo -n -H -u agent git-receive-pack` or `git-upload-pack`, so only `agent` runs git in its repo. `push` passes `push.negotiate=false`, which would otherwise start `git-upload-pack` as the admin. `destroy` reads the guest repo as `agent` too. | `test_push_never_runs_upload_pack_as_the_admin` (`tests/test_cli_git.py`); the `tests/test_lima_git.py` push and fetch tests |
| A crafted sandbox name or object ID reaches a path, an SSH host or a git argument. | 2 | Sandbox names must match `^[a-z][a-z0-9-]{0,30}$`, and the object IDs the CLI reads from git and passes on, the pushed `HEAD`, the fetched branch tips, and the guest's branch tips and detached `HEAD` that `destroy` checks, must be 40 or 64 hex characters. | `test_valid_names`, `test_invalid_names`, `test_refuses_an_object_name_that_isnt_hex` (`tests/test_cli.py`) |
| An Ansible config from the user's environment or working directory changes how `create` provisions. | all | `create` runs `ansible-playbook` with `ANSIBLE_CONFIG` set to this repo's `ansible/ansible.cfg`. | no dedicated test |
| Fetching runs code on the Mac or contacts another host. | 2 | Every git call passes `core.hooksPath=/dev/null`. `fetch` passes `fetch.bundleURI=` and `fetch.fsckObjects=true`, and takes no tags or submodules. Its branch refs go only to `refs/wardian/<name>/agent/*`, never the worktree (git also writes `FETCH_HEAD`). The changed-file list comes from `git diff --no-ext-diff --no-textconv --no-renames --no-relative`, so no diff driver runs and `diff.relative` can't hide paths. | `test_no_hook_runs`, `test_fetch_ignores_a_configured_bundle_uri`, each with a mutation test, and `test_fetch_from_a_subdirectory_lists_root_files` (`tests/test_cli_git.py`); `test_fetch_takes_agent_branches_and_no_tags` (`tests/test_lima_git.py`). `fetch.fsckObjects` and the diff-driver flags have no dedicated test. |
| The agent's branch adds a file a Mac tool runs on its own. | 2 | `fetch` prints each tip SHA and lists changed dotfiles and build files. It warns and doesn't block. | `test_fetch_lists_tips_and_auto_run_files` (`tests/test_cli_git.py`) |
| Guest output puts escape sequences in the Mac terminal. | 2 | The CLI strips control characters from guest output and from fetched paths. | `test_clean_strips_control_characters` (`tests/test_cli.py`); `test_fetch_strips_control_characters_from_paths` (`tests/test_cli_git.py`) |
| `push` overwrites the agent's work. | the agent's work | `push` doesn't force, and the guest refuses it over uncommitted changes. | `test_push_is_refused_over_uncommitted_work` (`tests/test_cli_git.py`); `test_push_is_refused_over_uncommitted_changes` (`tests/test_lima_git.py`) |
| `destroy` deletes work the Mac doesn't have, or a VM Wardian didn't make. | the agent's work | `destroy` refuses a stopped sandbox, an instance without the `wardian-sandbox` marker, and uncommitted, untracked, stashed, tagged or unfetched work. | `tests/test_cli.py` destroy tests; `test_destroy_refuses_unfetched_work` (`tests/test_lima_git.py`) |

## Accepted risks

These are risks Wardian accepts on purpose, from issue #1:

- Egress is open. The agent can send the project and its own login anywhere on the internet, including over DNS.
- The agent can run anything it downloads inside the VM, and install its own agent CLI in its home.
- The agent can read its own login and tamper with the T3 server in the VM. What the desktop app does for a replaced T3 server is untested.
- T3's `html_render` pages run in the Mac app and can make the Mac send blind requests to its own localhost, the LAN and the internet while the thread is open. Only T3 can fix this. A report goes to the T3 maintainers, and `create` prints a notice about it.
- `codex cloud` works from the guest. The account must have no connectors, and for Codex no Cloud environments or tasks. `create` prints a notice about it.
- A Mac with a public IPv4 address, or a router port forward reached by hairpin NAT, is reachable, because the firewall drops private addresses, not public ones.
- An archive package's root daemon or setuid binary may have a local privilege bug.
- The agent may reach Lima's vsock endpoints. nftables doesn't filter `AF_VSOCK`.
- Guest root, through a kernel or sudo bug, defeats the in-guest controls and can reach the Mac's LAN address. A hypervisor or Lima host-agent bug can reach the Mac.
- Anything the agent leaves running lasts until `destroy`.
- `git fetch` remote output may carry escape sequences beyond what the filter covers.
- Git's transfer protocol isn't designed to hide other objects in the Mac repo from the other side (`git-fetch` documentation, "Security"). Push from a repo that holds only this project.

## Blocked attempts

The firewall logs drops with the prefix `wardian-drop: ` and the UID, at a limited rate, and counts every drop. To read the log:

```sh
limactl shell <name> sudo journalctl -k --grep wardian-drop
```
