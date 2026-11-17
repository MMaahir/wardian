# Spike phase 1 notes: the discovery sandbox

Issue #2. These notes record what a hand-built Lima VM needed so that T3, Claude Code and Codex could finish a toy task inside it. Squid allowed every domain and logged it. nftables made Squid the only way out for the `agent` user and blocked private ranges. Phase 2 (issue #3) builds the allowlist from these results.

Run on 2026-11-16 and 2026-11-17. Host: macOS 26.6 on Apple silicon, Lima 2.3.0. The VM template is `spike/spike-phase1.yaml`.

## Summary for phase 2

- Allow these domains for the agents: `api.anthropic.com` (Claude), `chatgpt.com` and `auth.openai.com` (Codex). The rest of the Squid log is optional traffic or T3's installer. See [Domains](#every-domain-in-the-squid-log).
- Install T3's server while provisioning the VM. Otherwise T3 downloads it from GitHub on first connect, and GitHub would have to be on the allowlist.
- Install Codex from the full `codex-package` tarball, not the single `codex` binary. Codex in T3 fails without `codex-code-mode-host`.
- Mask `sshd-vsock.socket`. It listens on vsock port 22 even with `ssh.overVsock: false`.
- Filter or avoid the agent's shell when fetching code out of the VM. Bytes that the agent's `~/.bashrc` writes to stderr reach the Mac terminal unchanged.
- Allow `systemd-network` to send DHCP to 192.168.5.2, and decide what to do with chrony, fwupd, motd-news and the apt timers. See [Other dropped traffic](#other-dropped-traffic).

## Template and merged config

The template is hand-written with no `base`. It sets vz, one image pinned by digest, 4 CPU, 4 GiB memory, 20 GiB disk, no mounts, host loopback off, proxy propagation off, containerd off, agent forwarding off, `ssh.overVsock: false`, SSH on port 60022, and a catch-all port-forward ignore rule.

Without `base`, the template does not inherit `param.internal_netplanOptional: "true"` from Lima's `_images/ubuntu-26.04.yaml`. Leaving it out stalls first boot for 120 seconds on a 26.04 network bug. With it, boot took 18 seconds.

The merged config from `limactl list --json`, recorded once before first start, showing the keys that matter:

```json
{
 "vmType": "vz",
 "arch": "aarch64",
 "cpus": 4,
 "memory": "4GiB",
 "disk": "20GiB",
 "ssh": {
  "localPort": 60022,
  "loadDotSSHPubKeys": false,
  "forwardAgent": false,
  "forwardX11": false,
  "forwardX11Trusted": false,
  "overVsock": false
 },
 "containerd": { "system": false, "user": false },
 "propagateProxyEnv": false,
 "networkOpts": { "hostLoopback": false },
 "portForwards": [
  {
   "guestIPMustBeZero": false,
   "guestIP": "0.0.0.0",
   "guestPortRange": [1, 65535],
   "hostIP": "127.0.0.1",
   "hostPortRange": [1, 65535],
   "proto": "any",
   "ignore": true
  }
 ],
 "param": { "internal_netplanOptional": "true" },
 "images": [
  {
   "location": "https://cloud-images.ubuntu.com/releases/resolute/release-20261016/ubuntu-26.04-server-cloudimg-arm64.img",
   "arch": "aarch64",
   "digest": "sha256:8eb876c2ccc4b6ff89e4a65d511815923b285e04503ba465809bf587f763da24"
  }
 ],
 "hostResolver": { "ipv6": false },
 "guestInstallPrefix": "/usr/local",
 "nestedVirtualization": false,
 "video": { "display": "none" },
 "upgradePackages": false
}
```

Lima also fills in two values from the Mac that the template does not set:

- `timezone` copies the Mac's time zone.
- `user` copies the Mac's login name, full name and UID, with `passwordlessSudo: true`. This is the admin account that `limactl shell` uses. `agent` has no key for it.

Phase 2 can set both in the template if the Mac's name should not appear in the guest.

Checks inside the guest after boot: 4 CPUs, 3.8 GiB memory, 19 GB disk, no virtiofs, 9p or sshfs mounts, no proxy variables, no `SSH_AUTH_SOCK`, containerd inactive. Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic.

Port forwarding stayed off. Test listeners on guest 127.0.0.1, 0.0.0.0, ::1 and :: showed up in the guest agent's events, and the host agent forwarded none of them. Its log says `TCP (except for SSH) and UDP port forwarding is disabled`.

## The agent user and SSH

`agent` belongs only to the groups `agent` and `users`, and `sudo -n true` refuses it. `adduser` gave it subuid and subgid range 100000:65536. `usermod --del-subuids 100000-165535 --del-subgids 100000-165535 agent` removed both.

The Mac has a dedicated key, `~/.ssh/wardian-spike-agent`, and this host entry:

```
Host wardian-spike
  HostName 127.0.0.1
  Port 60022
  User agent
  IdentityFile ~/.ssh/wardian-spike-agent
  IdentitiesOnly yes
  ForwardAgent no
  ForwardX11 no
  HostKeyAlias wardian-spike
  UserKnownHostsFile ~/.ssh/known_hosts.d/wardian-spike
  StrictHostKeyChecking yes
```

`/etc/ssh/sshd_config.d/50-wardian-agent.conf` in the guest:

```
Match User agent
  AllowTcpForwarding local
  PermitListen none
  AllowAgentForwarding no
  AllowStreamLocalForwarding no
  X11Forwarding no
  PermitTunnel no
```

T3 connected with this block in place, so nothing had to be relaxed. T3 reaches its server through a TCP local forward (`-L <mac port>:127.0.0.1:3773`), which `AllowTcpForwarding local` permits. It does not use Unix socket forwarding.

## Network controls

### Resolver

Lima's resolver is 192.168.5.2. That address is also the default gateway and the Mac side of the vz NAT. On the Mac, the Lima host agent answers DNS for it on 127.0.0.1:54649 (TCP) and 127.0.0.1:52422 (UDP). This explains the `limactl` listener on 54649 seen during the spike. IPv6 in the guest is link-local only.

systemd-resolved is masked, along with `systemd-resolved-varlink.socket` and `systemd-resolved-monitor.socket`. `/etc/resolv.conf` is a static file:

```
nameserver 192.168.5.2
```

### nftables

`/etc/nftables.conf` defines its own table, `inet wardian`. It deletes and rebuilds only that table, never the whole ruleset with `flush ruleset`, because Lima keeps an `ip nat` table named LIMADNS that must survive. The output chain has policy `drop`:

- `agent` may reach Squid on loopback port 3128 and other loopback ports except 53. Anything else from `agent` is logged and dropped.
- `proxy` (Squid) may reach 192.168.5.2 on port 53, and any public address on port 443. Private IPv4 and IPv6 ranges, including `::ffff:0:0/96`, are logged and dropped.
- `root` and `_apt` may go anywhere.
- Everything else is logged with the prefix `wardian-drop:` and the UID, then dropped.

`systemctl cat nftables` on Ubuntu 26.04 (package nftables 1.1.6):

- `DefaultDependencies=no`, `Before=network-pre.target`, `Wants=network-pre.target`, `WantedBy=sysinit.target`.
- `ExecStart` and `ExecReload` load `/etc/nftables.conf`. `ExecStop` runs `nft flush ruleset`.
- The unit is disabled by default. The spike enabled it.

The rules load before the network comes up. `ExecStop` flushes every table, Lima's LIMADNS included, so stopping the service on a running VM leaves no firewall. Phase 2 should not stop or restart it on a live VM.

### Squid

Squid 7.7 listens on 127.0.0.1:3128 with no cache. It denies `manager`, denies a `to_private` destination ACL, allows `localhost`, and denies everything else. The access log is JSON sent to syslog, so it lands in the journal under `squid.service`:

```
logformat wardian_json {"time":"%{%Y-%m-%dT%H:%M:%S%z}tl","client":"%>a","method":"%rm","host":"%>rd","port":"%>rP","url":"%>ru","status":%>Hs,"squid_status":"%Ss","server_ip":"%<a","bytes_out":%>st,"bytes_in":%<st,"ms":%tr}
access_log syslog:daemon.info wardian_json
```

A drop-in sets `LogRateLimitIntervalSec=0` on `squid.service` so journald keeps every line.

`::ffff:0:0/96` must stay out of Squid's `dst` ACL. Squid stores IPv4 addresses internally as IPv4-mapped IPv6, so that range matches every IPv4 address. Squid warns that the other ranges are "covered by 0.0.0.0/0". It belongs in the nftables set, where it means only mapped addresses.

### Private-range tests

From the Mac, through an `ssh -L 13128:127.0.0.1:3128` tunnel as `agent`, every one of these returned 403 `ERR_ACCESS_DENIED` for both a plain GET and a CONNECT to port 443:

- 192.168.5.2, `host.lima.internal`, `localhost`, 127.0.0.1, [::1]
- the Mac's LAN address and the LAN gateway
- 169.254.169.254, [fd00:ec2::254], [::ffff:169.254.169.254], [::ffff:192.168.5.2]
- the Mac's link-local IPv6 address

The Mac had no global IPv6 address or IPv6 default route, so those were the only IPv6 targets.

Decimal and hex forms of 192.168.5.2 and 127.0.0.1 did not test Squid. curl rewrote them to dotted form before sending, so Squid never saw the encoded forms. Phase 2 should send raw requests if it wants that test.

Inside the guest, as `agent`: direct HTTPS fails because there is no DNS. HTTPS through Squid works. HTTP on port 80 through Squid times out, because nftables lets `proxy` out on 443 only. `getent`, `dig` and `resolvectl` fail. 192.168.5.2:22 times out. Host loopback is closed: a guest request to a Mac loopback service on 192.168.5.2:8765 failed.

## CLIs

Root installed both. `agent` cannot write to either binary or to their system config.

Claude Code:

- From Anthropic's signed apt repo, key fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, stable channel.
- Version 2.1.311-1, held with `apt-mark hold`.
- `/usr/bin/claude` sha256 `2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3`.
- `/etc/claude-code/managed-settings.json` sets `DISABLE_AUTOUPDATER=1`, `DISABLE_UPDATES=1`, `ENABLE_CLAUDEAI_MCP_SERVERS=false` and `disableClaudeAiConnectors: true`.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` was left unset so the log would show every domain.

Codex:

- Release `rust-v0.169.0`, `codex-cli 0.169.0`, statically linked musl build for aarch64.
- First install was the single binary from `codex-aarch64-unknown-linux-musl.tar.gz` (sha256 `97b97d87eea075c5cb7709c7f6156a451622a7667953ecda40e0276637196f3f`, matching GitHub's asset digest). `/usr/local/bin/codex` sha256 `3bb41c9a80187773e2a59278d4fc355b1a163c31bb5a79581e37d4fe428af047`.
- That was not enough. Codex in T3 failed with "`codex-code-mode-host` was not found" and could not run any command. `codex exec` from a shell did not hit this error.
- The fix was the full package, `codex-package-aarch64-unknown-linux-musl.tar.gz` (sha256 `c8d0b4ab2a97c595b70b10412d36d0d5464dcef1e1d3246b073d48017cdf5a4f`, matching GitHub's digest). Root unpacked it into `/opt/codex/0.169.0` and linked `/usr/local/bin/codex` to `bin/codex` inside it. The package's `bin/codex` has the same hash as the single binary. `bin/codex-code-mode-host` is sha256 `66874c7881c4004f1c155584e1325cad17795bdd91b886995ad8a043809c1019`. The package also holds `codex-exec-server`, `rg`, `bwrap` and a voice helper.
- The running `codex app-server` had to be stopped after the swap, because it kept using the old file.

### Codex system config file

Codex 0.169.0 reads three files under `/etc/codex`: `config.toml`, `managed_config.toml` and `requirements.toml`. The user's `~/.codex/config.toml` overrides `/etc/codex/config.toml`. `managed_config.toml` and `requirements.toml` override both the user file and `-c` flags. Phase 1 used `managed_config.toml`:

```toml
check_for_update_on_startup = false

[features]
apps = false
```

## Credentials

The Claude token came from `claude setup-token` and was stored in the Mac Keychain as `wardian-claude-token`. It went into the guest over stdin only:

```sh
security find-generic-password -s wardian-claude-token -w | tr -d '\n' \
  | limactl shell wardian-spike sudo sh -c 'umask 027; cat > /etc/wardian/claude-token; chown root:agent /etc/wardian/claude-token; chmod 0640 /etc/wardian/claude-token'
```

`/etc/profile.d/wardian-claude-token.sh` exports `CLAUDE_CODE_OAUTH_TOKEN` from that file for users who can read it.

The first Keychain entry held a 10-character value that was not a token. Phase 2 should check the `sk-ant-oat` prefix and the length before sending it, without printing the value.

Codex logged in with `codex login --device-auth` as `agent`, through Squid. Credentials are in `/home/agent/.codex/auth.json` (0600). The first device code expired before it was approved. The second worked.

## T3

The T3 desktop app connected as `agent` using the `wardian-spike` host entry: Settings, Connections, Environments, Add environment, SSH. It showed `SSH agent@127.0.0.1 · Connected`. The project was `/home/agent/toy`.

- T3 server version 0.1.10, installed in `~/.t3/runtime/versions/0.1.10`.
- On first connect, a launcher script (`~/.t3/ssh-launch/<id>/run-t3.sh`) downloaded `SHA256SUMS` and `t3-0.1.10-linux-arm64.tar.gz` from `https://github.com/pingdotgg/t3code/releases/download`. curl went through Squid, which logged `github.com` and `release-assets.githubusercontent.com`. The script checks the archive against `SHA256SUMS` from the same release. It also says `T3CODE_RELEASE_BASE_URL` can point at a mirror, and that `https://t3.codes/install.sh` with `T3CODE_VERSION` installs a set version ahead of time.
- The archive is self-contained. No Node, npm or compiler was needed on the guest, so node-pty needs no build tools.
- `t3 serve` listens on 127.0.0.1:3773 in the guest. The desktop app forwards a random Mac loopback port to it over SSH.
- The server runs as `agent` with parent PID 1, so it keeps running after the SSH connection closes.
- The server's environment has `http_proxy`, `https_proxy`, `no_proxy` and the uppercase forms from `/etc/environment`, and `CLAUDE_CODE_OAUTH_TOKEN` from the login profile. The launch goes through a login shell.
- The server logged: `This host blocks the sandbox T3's browser runs in, so browser tabs and HTML previews will not start. Run sudo .../t3 browser setup ... once to allow it.` The spike did not run it.
- T3 writes checkpoint commits under `refs/t3/` in the project repo. The fetch refspec below does not bring them to the Mac.

### Which unit served the T3 session

`systemctl list-sockets --all` during the T3 session, SSH lines only:

```
LISTEN                      UNIT                    ACTIVATES
0.0.0.0:22                  ssh.socket              ssh.service
[::]:22                     ssh.socket              ssh.service
vsock::22                   sshd-vsock.socket       -
/run/ssh-unix-local/socket  sshd-unix-local.socket  -
```

The sshd journal shows every T3 login as `Accepted publickey for agent from 192.168.5.2`, which is the TCP path through `ssh.socket`. `ssh.service` itself is disabled and starts only on socket activation.

`sshd-vsock.socket` exists and listens on vsock port 22 even with `ssh.overVsock: false`. systemd-ssh-generator creates it whenever the guest has a vsock device. Phase 2 must mask it so SSH has one path. `sshd-unix-local.socket` is a local Unix socket in `/run`.

## vsock

- Mac side: the instance directory has `ha.sock`, `ssh.sock`, `default_ep.sock` and `default_fd.sock`, all owned by the Mac user. `ssh.sock` and `default_ep.sock` are mode 0600.
- Guest side: the Lima guest agent runs as root and listens on vsock port 2222 (CID 3).
- `/dev/vsock` is mode 0666.
- As `agent`, connecting to CID 2 (the host) on ports 1 to 65535 found no listener. CID 3 gave `ENODEV`. CID 1 reset the connection.

`agent` can open vsock sockets but found nothing on the host to talk to. Phase 2 could add an nftables or seccomp rule if it wants vsock closed to `agent` outright.

## Linger

`pkaction --action-id org.freedesktop.login1.set-self-linger` shows the action allowed for any local user. As `agent`, `loginctl enable-linger` succeeded with no prompt. The spike reversed it with `loginctl disable-linger agent`. With linger on, `agent` processes could outlive its sessions and start at boot. Phase 2 should block it with a polkit rule.

## Time sync

chrony is active and systemd-timesyncd is not. The Lima host agent also sets the guest clock. `_chrony` (UID 988) was dropped trying to reach:

- 192.168.5.2 UDP 53, 136 times
- Ubuntu's NTS servers (185.125.190.121 to .123, 91.189.91.112 and .113) on TCP 4460 (NTS key exchange), 88 times each
- the same servers on UDP 123 (NTP), 8 times each

Ubuntu 26.04's chrony uses NTS. Phase 2 can either allow `_chrony` to DNS and those servers on 4460 and 123, or disable chrony and rely on Lima. Disabling is the smaller change. Clock drift was not measured.

## Other dropped traffic

Totals from the whole phase 1 run:

| User | Destination | Count | What it is |
|---|---|---|---|
| `agent` (1001) | 192.168.5.2 UDP 53 | 68 | Direct DNS from tools running as `agent`, mostly in the 20 seconds after T3 connected. Expected. |
| `agent` (1001) | 192.168.5.2 TCP 22 | 5 | The spike's own reachability test. |
| `fwupd-refresh` (985) | 192.168.5.2 UDP 53 | 52 | fwupd's metadata refresh timer. |
| `systemd-network` (998) | 192.168.5.2 UDP 67 | 38 | DHCP lease renewals. |
| `systemd-network` (998) | ff02::2 ICMPv6 | 26 | IPv6 router solicitation. |
| `proxy` (13) | 172.66.147.243 TCP 80 | 11 | Test HTTP request to example.com. Expected. |

The DHCP lease from 192.168.5.2 lasts one hour, with renewal at 30 minutes. Unicast renewals were dropped. systemd-networkd still kept the address, because the broadcast rebind at 52 minutes goes out on a raw packet socket that nftables' output hook does not see. Phase 2 should allow `systemd-network` to 192.168.5.2 on UDP 67 rather than rely on that.

Root traffic is allowed and never goes through Squid, so none of it is logged. These timers run as root and reach the internet on their own: `apt-daily`, `apt-daily-upgrade`, `motd-news`, `update-notifier-download`, `update-notifier-motd`. Phase 2 should disable them and narrow the `root` rule.

## Every domain in the Squid log

The full phase 1 log, grouped. All are CONNECT to port 443 with status 200 unless noted.

| Domain | Requests | Who | Needed? |
|---|---|---|---|
| `api.anthropic.com` | 55 | Claude Code | Yes. Model API. |
| `http-intake.logs.us5.datadoghq.com` | 8 | Claude Code | No. Telemetry. Anthropic lists it as non-essential traffic. |
| `chatgpt.com` | 78 | Codex | Yes. Model API for ChatGPT login. |
| `auth.openai.com` | 5 | Codex | Yes, at login and token refresh. |
| `ab.chatgpt.com` | 4 | Codex | Unclear. The name suggests experiment flags. Test blocking it in phase 2. |
| `sdmntprcentralus.oaiusercontent.com`, `sdmntprsouthcentralus.oaiusercontent.com`, `sdmntprwestus3.oaiusercontent.com` | 1, 4, 2 | Codex, after T3 connected | Unclear. OpenAI file storage. Test blocking them in phase 2. |
| `github.com`, `release-assets.githubusercontent.com` | 2, 2 | T3 launcher | No, if T3 is installed while provisioning. |
| `example.com` | 2 CONNECT, 2 GET on port 80 | Spike tests | No. The port 80 GETs got no response, because nftables blocked Squid on port 80. |

The 403 lines in the log are the private-range tests listed above.

## Toy task

The toy project lives in `~/Developer/wardian-toy` on the Mac, outside this repo, and was never pushed to GitHub. Its only remote is `sandbox`, pointing at `wardian-spike:toy`. In the guest, `/home/agent/toy` is a non-bare repo with `receive.denyCurrentBranch updateInstead`, so a push from the Mac updates its working tree.

Both agents ran from T3 in full-access mode on the current checkout, one after the other:

| Agent | Model shown in T3 | Branch | Commit | Time | Result |
|---|---|---|---|---|---|
| Claude Code | Claude Opus 5.5, high effort | `agent/claude-top-words` | `813e4f7` | 11 s | 2 tests pass |
| Codex | GPT-6.1-Sol, high effort | `agent/codex-top-words` | `fcfcd3a` | 30 s | 2 tests pass, after the `codex-package` fix |

Neither agent asked for approval, and neither changed `test_words.py`. Both commits touch only `words.py`. Claude's version counts runs of ASCII letters and apostrophes. Codex's version also handles accented letters and returns an empty list for `n <= 0`. Neither diff opens files, starts processes or makes network calls.

The Mac fetched both branches with:

```sh
git -c transfer.fsckObjects=true fetch --no-tags --no-recurse-submodules \
  sandbox '+refs/heads/agent/*:refs/remotes/sandbox/agent/*'
```

### Escape sequences from git fetch

Yes, they reach the Mac terminal. The test added one line to the top of `agent`'s `~/.bashrc`:

```sh
printf "remote-test \033[31mRED\033[0m \033]0;TITLE-SET-BY-VM\007\n" >&2
```

`git fetch` from the Mac then printed the color code and the OSC window-title code unchanged (`cat -v` shows `^[[31m` and `^[]0;TITLE-SET-BY-VM^G`). The `.bashrc` was restored afterwards.

These bytes never pass through git's `remote:` sideband, where git could filter them. sshd starts `agent`'s login shell to run `git-upload-pack`, the shell reads `~/.bashrc`, and anything it writes to stderr goes straight to the Mac's terminal. `agent` owns that file. The Mac's git version (Apple Git 2.50.1) makes no difference on this path. Escapes in the sideband itself were not tested, because this path already passes them through.

Phase 2 options:

- Pipe fetch output through a filter such as `cat -v`, or send it to a file.
- Fetch through a path that does not start `agent`'s shell, for example root writing a `git bundle` and copying it out.

## Destroyed

The phase 1 VM was deleted with `limactl delete --force wardian-spike`. The T3 environment entry for it remains in the desktop app and can be removed by hand.
