# Spike phase 2 notes: the acceptance sandbox

Issue #3. These notes record a fresh Lima VM built with the allowlist from phase 1, and the checks run against it. Squid allows three host names on port 443 and nothing else. nftables limits the `agent` user to Squid and loopback, Squid to DNS and public port 443, and root to DNS and public ports 80 and 443.

Run on 2026-11-17 and 2026-11-18. Host: macOS 26.6 on Apple silicon, Lima 2.3.0. The VM template is `spike/spike-phase2.yaml`. The Mac's LAN address and LAN gateway appear below as `<mac-lan-ip>` and `<lan-gateway>`.

The ticket is not finished. Every check that needs the Claude token, a Codex login or the T3 desktop app is still waiting on a user step. See [Pending user steps](#pending-user-steps).

## Summary so far

- Every network, SSH, file permission and linger control that can be tested without the agents passed. Squid's and nftables' private-range blocks each stop the Mac on their own.
- A broken `/etc/nftables.conf` keeps port 22 closed at boot, because `ssh.socket` requires `nftables.service`. It also leaves the guest with no firewall at all until someone fixes it.
- The initramfs gets a DHCP lease from 192.168.5.2 about one second before the rules load. Nothing else leaves early.
- `sshd-unix-local.socket` was open to every local user (`0666`). It is now masked along with `sshd-vsock.socket`.
- Squid escapes `"` and `\` in the JSON log, but passes bytes above 0x7f raw, so a log line can be invalid UTF-8.
- `agent`'s subuid range moved to `165536` once Lima's admin account became UID 1000. Phase 1's hard-coded `usermod` range removed nothing.
- Two more root timers reach the network: `ua-timer` and `apport-autoreport`. Both are masked now.
- `codex cloud` probably uses `chatgpt.com`, which the allowlist must keep for the model API. If so, that criterion will fail.

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| A fresh VM from the phase 1 template is used | Pass | `limactl create` from `spike/spike-phase2.yaml`, which is the phase 1 template plus a fixed admin user and time zone. Same image digest. No instance existed before. |
| Squid listens on loopback only, CONNECT to 443 only, denies non-allowlisted names before any lookup, denies the cache manager, ends with deny-all | Pass | `ss` shows only `127.0.0.1:3128`. GET and CONNECT to ports 80, 22 and 8443 get 403. A packet capture showed no DNS query for two non-allowlisted names, and one for the allowlisted control. The cache manager gets 403. See [Squid](#squid). |
| The allowlist holds only the agents' API and login hosts plus read-only hosts the toy task needs | Pass for the config; pending for sufficiency | The allowlist is `api.anthropic.com`, `chatgpt.com`, `auth.openai.com`. The toy task needs no other host. Whether both agents work with only these is pending the toy task runs. |
| As `agent`, a non-allowlisted domain fails, and so do `dig`, `getent hosts` and `resolvectl query` | Pass | Nine non-allowlisted names got `CONNECT tunnel failed, response 403`. `dig` reached no server, `getent hosts` exited 2, `resolvectl query` failed. |
| As `agent`, the Mac (192.168.5.2 and its LAN address) can't be reached, directly or through Squid | Pass | Direct attempts timed out and raised the `agent_dropped` counter. Through Squid, every IP literal and Mac name got 403. Squid's and nftables' private-range blocks were each tested on their own with a positive control. See [Private-range tests](#private-range-tests). |
| `sudo` fails for `agent` | Pass | `sudo -n true` exits 1. `sudo -l` says `user agent may not run sudo`. |
| As `agent`, `-R` and agent forwarding are refused, a `-L` to a non-loopback address fails, T3's loopback forward still works | Pass with a test listener; T3 itself pending | `-R` fails with `remote port forwarding failed`. With `ForwardAgent=yes`, `SSH_AUTH_SOCK` is empty. `-L`, `-D` and `-W` to anything but `127.0.0.1:3773` get `administratively prohibited`. `-L` to `127.0.0.1:3773` reached a test listener. T3's own connect is pending. |
| `agent` can't write the CLI binaries or their config, or the token and login-profile files. No update host is on the allowlist | Pass | Every append and create attempt got `Permission denied`. `find -writable` as `agent` found nothing under the CLI and config paths. No update host is on the allowlist. The token file does not exist yet. See [Files agent can't write](#files-agent-cant-write). |
| Inside a T3 session, no AWS, Google, Cloud SDK, Anthropic or OpenAI API variables | Pending user step | An SSH login shell as `agent` has none of them. The T3 session check needs the desktop app. See [Questions](#questions-for-the-issue) about `CLAUDE_CODE_OAUTH_TOKEN`. |
| Claude and Codex each finish the toy task through Squid; the notes record Codex's transport | Pending user step | Needs the token and a Codex login. |
| In a T3 session, every tool each agent can call is listed, and none acts on the Mac | Pending user step | Needs the desktop app. |
| Modifying or replacing the T3 server, then asking for a Mac-side tool, is refused by the desktop app | Pending user step | Needs the desktop app. `agent` can rename the root-owned server directory, so the tamper test is possible. |
| No account connector is active for either agent | Pending user step | Managed config turns off Claude's claude.ai connectors and Codex's `apps` feature. `codex features list` shows `apps false`. A live check needs logged-in agents. |
| `codex cloud` can't see or start tasks on your repos | Pending user step | Needs a Codex login. |
| All of the above hold with both agents in full-access mode | Pending user step | Needs the agent runs. |
| The spike notes and template are committed | Pass | This file and `spike/spike-phase2.yaml` on branch `spike/phase-2`. |

Other checks the phase 1 notes asked for:

| Check | Status |
|---|---|
| `sshd-vsock.socket` and `sshd-unix-local.socket` masked; only `ssh.socket` listens | Pass |
| `ssh.socket` has `Requires=` and `After=nftables.service` | Pass |
| Boot with a broken `/etc/nftables.conf` leaves port 22 closed | Pass. See [Boot with a broken nftables.conf](#boot-with-a-broken-nftablesconf). |
| Linger denied for `agent`, allowed for root | Pass |
| `"` in a URL does not break the JSON log line | Pass, but bytes above 0x7f pass through raw. See [Squid log format](#squid-log-format). |
| DHCP renewal works with the new rule | Pass. See [DHCP](#dhcp). |
| `agent` straight to a public IP fails | Pass |
| No packet leaves before the rules load | Fail, minor. The initramfs gets a DHCP lease about one second before nftables loads. See [What leaves before the rules load](#what-leaves-before-the-rules-load). |

## Template

`spike/spike-phase2.yaml` is the phase 1 template with two additions:

- `user` sets the admin account to `lima`, comment `Lima admin`, UID 1000. Without it Lima copies the Mac's login name, full name and UID, and `agent` can read them in `/etc/passwd`. Lima's cloud-config creates no `ubuntu` user, so UID 1000 is free.
- `timezone: "Etc/UTC"`. Without it Lima copies the Mac's time zone.

The pinned image (`release-20261016`, digest `sha256:8eb876c2...`) still downloads, and Lima used its cached copy. The merged config showed the same values as phase 1 for `ssh`, `portForwards`, `networkOpts` and `param`, plus the new `user` and `timezone`. Boot took 12 seconds.

The `agent` UID is 1001, as in phase 1. With UID 1000 for `lima`, `adduser` gave `agent` the subuid range `165536:65536`, not `100000:65536` as in phase 1. The phase 1 `usermod --del-subuids 100000-165535` command removed nothing. The command that worked is `usermod --del-subuids 165536-231071 --del-subgids 165536-231071 agent`. An automated build should read the range from `/etc/subuid` and not hard-code it.

## Provisioning

Root provisioned everything through `limactl shell wardian-spike sudo bash -s` with a script on stdin. The full contents of the files it wrote are in [Guest files](#guest-files). In order:

1. Purged `snapd` and `packagekit` (neither was installed). Installed `squid`, `git`, `curl`, `ripgrep`, `bind9-dnsutils`, `nftables` and `ncat`.
2. Created `agent` with `adduser --disabled-password`, removed its subuid and subgid range, and installed the Mac's `~/.ssh/wardian-spike-agent.pub` as its only authorized key.
3. Wrote the sshd `Match User agent` block, masked `sshd-vsock.socket` and `sshd-unix-local.socket`, and added a drop-in that makes `ssh.socket` require nftables.
4. Masked systemd-resolved and its two sockets, and wrote a static `/etc/resolv.conf`.
5. Masked chrony and the root timers. See [Time sync and timers](#time-sync-and-timers).
6. Installed Claude Code, Codex and the T3 server. See [CLIs and T3 server](#clis-and-t3-server).
7. Wrote and started the Squid config.
8. Wrote the managed configs for both CLIs, the proxy block in `/etc/environment`, `/etc/profile.d/wardian-claude-token.sh`, and the polkit linger rule. Created `/etc/wardian` as `root:agent 0750`, empty.
9. Wrote `/etc/nftables.conf`, added a drop-in that empties `ExecStop`, and enabled and started `nftables.service`.
10. As `agent`, created `~/toy` with `receive.denyCurrentBranch updateInstead` and a git identity. The Mac pushed the toy project's `main` (`c8fb1a6`) into it. Both tests fail there as expected until `top_words` is written.

The Mac's known-hosts file `~/.ssh/known_hosts.d/wardian-spike` held the destroyed VM's key. It now holds the new VM's ed25519 key, read with `limactl shell wardian-spike cat /etc/ssh/ssh_host_ed25519_key.pub`. The fingerprint is `SHA256:HkiOH+OXpn7R8UUlRc/e9NK8V4IPTqnUPTEv/FRfuEc`, and `ssh wardian-spike` connects with `StrictHostKeyChecking yes`.

Loading the firewall on the running VM stalled the `limactl shell` session that loaded it for about two minutes. The kernel log showed root's sshd replies on that connection dropped with `wardian-drop: ... SPT=22 ... UID=0`. Before the rules loaded, no table hooked conntrack (Lima's `ip nat` table has only an unhooked `LIMADNS` chain), so the kernel had not tracked that connection. When the rules loaded, it picked the connection up mid-stream as new, not established. New SSH connections after that worked. At boot the rules load before the network, so this happens only when loading the rules on a live VM.

## The agent user and SSH

`agent` belongs to `agent` and `users` only. `sudo -n true` fails with `I'm afraid I can't do that`, exit 1, and `sudo -l` says `user agent may not run sudo`. `/etc/subuid` and `/etc/subgid` list only `lima`.

`/etc/ssh/sshd_config.d/50-wardian-agent.conf` now has `PermitOpen 127.0.0.1:3773`. `sshd -T -C user=agent,...` shows:

```
x11forwarding no
allowtcpforwarding local
allowagentforwarding no
allowstreamlocalforwarding no
permittunnel no
permitopen 127.0.0.1:3773
permitlisten none
```

The `lima` admin account keeps sshd's defaults, with every kind of forwarding allowed. `agent` has no key for it.

### Forwarding tests

Run from the Mac as `agent`, with a test listener (`ncat -lk 127.0.0.1 3773`) standing in for T3's server and a second one on 3774:

| Request | Result |
|---|---|
| `ssh -W 127.0.0.1:3773` | Connected, printed the listener's banner |
| `ssh -L 13773:127.0.0.1:3773` | Connected, printed the banner |
| `ssh -D` then SOCKS to `127.0.0.1:3773` | Connected, printed the banner |
| `-W` or `-L` to `127.0.0.1:3774`, `localhost:3773`, `127.0.0.1:3128`, `192.168.5.2:53`, `<mac-lan-ip>:7000`, `1.1.1.1:443` | `channel N: open failed: administratively prohibited: open failed` |
| `ssh -D` then SOCKS to `1.1.1.1:443` | Refused |
| `ssh -W /run/dbus/system_bus_socket` | `open failed: connect failed`; sshd logged `refused streamlocal port forward` |
| `ssh -R 0:127.0.0.1:22` and `-R 127.0.0.1:18022:127.0.0.1:22` | `remote port forwarding failed`; no listener appeared in the guest |
| `ssh -o ForwardAgent=yes` | The client sent `auth-agent-req@openssh.com`; `SSH_AUTH_SOCK` in the session was empty |

For every refused `-L`, `-W` and `-D`, sshd logged a line like `Received request from 192.168.5.2 port 64730 to connect to host <mac-lan-ip> port 7000, but the request was denied.`

`PermitOpen` matches the host string exactly. `localhost:3773` is refused, so T3 must keep asking for `127.0.0.1:3773`, which it did in phase 1. The test listeners were stopped afterwards so they don't hold port 3773 when T3 connects.

### SSH sockets

`sshd-unix-local.socket` listened on `/run/ssh-unix-local/socket` with mode `0666`, owned by root. `agent` connected to it and got sshd's banner (`SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3.8`). The socket starts `sshd@.service`, which runs `sshd -i` with the same config and also reads keys from a systemd credential. `limactl` and T3 both use TCP, so nothing needs this socket. It is masked, which leaves one way into sshd, and that way depends on nftables. The generator writes its units to `/run/systemd/generator`, and `/etc/systemd/system` takes precedence over that, so `systemctl mask` works on them.

After masking, `systemctl list-sockets --all` shows only:

```
LISTEN    UNIT        ACTIVATES
0.0.0.0:22 ssh.socket ssh.service
[::]:22    ssh.socket ssh.service
```

`ss -A vsock -lnp` shows only the Lima guest agent on vsock port 2222. The stale socket file `/run/ssh-unix-local/socket` stayed until reboot, with no listener behind it.

`/etc/systemd/system/ssh.socket.d/50-wardian-nftables.conf` adds `Requires=nftables.service` and `After=nftables.service`. `systemctl show ssh.socket` lists `nftables.service` under both.

## Network controls

### Resolver

As in phase 1: systemd-resolved and its varlink and monitor sockets are masked, and `/etc/resolv.conf` is a static file with `nameserver 192.168.5.2`. `nsswitch.conf` has `hosts: files dns`.

### nftables

`/etc/nftables.conf` still replaces only `table inet wardian`. Changes from phase 1:

- Named counters `agent_dropped`, `proxy_private_dropped` and `root_new`, so each test can show which rule acted.
- `systemd-network` may send UDP from port 68 to 192.168.5.2 port 67, for DHCP renewals.
- Root and `_apt` are narrowed. They may reach 192.168.5.2 on port 53 and public addresses on TCP 80 and 443. Private IPv4 and IPv6 ranges are dropped for them, so root can't reach the Mac either. Each new root connection is logged with the prefix `wardian-root:`.

Root kept ports 80 and 443 so the admin can still install packages. A root process that reaches the internet now leaves a log line. Phase 1 could not see root traffic at all. Dropping root's outbound traffic entirely would also work once provisioning is scripted, since nothing that runs as root after provisioning needs the network. The check: a root connection to 1.1.1.1:443 succeeded and logged `wardian-root: DST=1.1.1.1 DPT=443 UID=0`, and root connections to `<mac-lan-ip>:7000` and 192.168.5.2:7000 timed out with `wardian-drop` lines.

`systemctl cat nftables` is unchanged from phase 1 apart from the drop-in. The drop-in `/etc/systemd/system/nftables.service.d/50-wardian.conf` sets an empty `ExecStop=`. The packaged `ExecStop` runs `nft flush ruleset`, which removes every table, Lima's included, and would leave agent processes such as T3's server running with no firewall. With the drop-in, stopping the unit leaves the rules loaded, and `Requires=` still stops `ssh.socket`. Following the issue's instructions, nftables.service was never stopped or restarted on the live VM, so this drop-in's stop behavior is untested.

Packets that systemd-network sends to `ff02::2` (IPv6 router solicitation) are still dropped and logged. The guest has no IPv6 router, so this changes nothing.

### Private-range tests

Each layer was tested on its own, with a positive control and the counters. The Mac had AirPlay listening on `*:5000` and `*:7000`, which gave a real service to aim at.

**Positive control: the Mac is reachable when nothing blocks it.** A temporary rule at the top of the output chain let `lima` (UID 1000) reach 192.168.5.2 and `<mac-lan-ip>` on TCP 22, 5000 and 7000. As `lima`, `<mac-lan-ip>:7000` and `<mac-lan-ip>:5000` connected. 192.168.5.2 refused ports 7000, 5000 and 22 with `Connection refused`, even though AirPlay listens on all the Mac's addresses. That shows `hostLoopback: false` blocks the Mac's loopback services at Lima, before the guest firewall plays any part. The temporary rule was deleted by its handle, and a listing afterwards showed no copy left.

**nftables alone, agent.** As `agent` with no proxy, connections to `<mac-lan-ip>:7000`, `<mac-lan-ip>:5000`, `192.168.5.2:7000`, `192.168.5.2:53`, `1.1.1.1:443`, `[::ffff:<mac-lan-ip>]:7000` and `[::ffff:1.1.1.1]:443` all timed out. `curl --noproxy '*' https://1.1.1.1/` timed out. `agent_dropped` went from 0 to 49. The IPv4-mapped addresses went out as IPv4 and the agent rule caught them.

**nftables alone, Squid's user.** As `proxy` (Squid's UID 13), outside Squid, connections to `192.168.5.2:443`, `<mac-lan-ip>:443`, `<mac-lan-ip>:7000`, `<lan-gateway>:443`, `169.254.169.254:443`, `[::ffff:192.168.5.2]:443`, `[::ffff:<mac-lan-ip>]:7000` and `[fe80::1%eth0]:443` all timed out. `proxy_private_dropped` went from 20 to 52 on the second run. The control, `1.1.1.1:443` as `proxy`, connected. `1.1.1.1:80` as `proxy` timed out in the final drop rule. This is the test phase 1 could not run, because Squid's ACL always denied first.

**Squid alone.** Two lines added to `/etc/hosts` pointed allowlisted names at the Mac: `192.168.5.2 chatgpt.com` and `<mac-lan-ip> auth.openai.com`. After a Squid restart, CONNECT to both got 403, and the journal logged `"squid_status":"TCP_DENIED","server_ip":"-"`. `proxy_private_dropped` did not move (0 before and after), so Squid's `to_private` ACL denied the request before Squid tried to connect. `/etc/hosts` was restored and Squid restarted.

**Through Squid, as agent.** CONNECT to port 443 got 403 for `1.1.1.1`, `192.168.5.2`, `<mac-lan-ip>`, `host.lima.internal`, `localhost`, `127.0.0.1`, `[::1]`, `169.254.169.254`, `[::ffff:192.168.5.2]` and `[fd00:ec2::254]`. A plain GET got 403 for `192.168.5.2`, `<mac-lan-ip>` and `host.lima.internal`. The allowlist ACL denies all of these first, since none is an allowlisted name.

`localhost`, `127.0.0.1` and `[::1]` need `--noproxy '' -x http://127.0.0.1:3128` to test Squid, because `no_proxy` sends them direct. Direct, they hit nothing on port 443.

### Squid

`/etc/squid/squid.conf` keeps phase 1's listener, cache and log settings and adds `visible_hostname wardian-spike` (otherwise Squid tries reverse DNS on its host name at startup). The access rules, in order:

```
http_access deny manager
http_access deny !CONNECT
http_access deny !SSL_ports
http_access deny !allowlist
http_access deny to_private
http_access allow localhost
http_access deny all
```

`allowlist` is `dstdomain -n api.anthropic.com chatgpt.com auth.openai.com`. `-n` stops Squid from reverse-resolving an IP literal for this ACL, so an IP literal never matches. `to_private` is a `dst` ACL, which resolves the name, so it comes after the allowlist. `squid -k parse` reports only `WARNING: HTTP requires the use of Via`, which is expected with `via off`.

Results as `agent`:

- `api.anthropic.com` returned HTTP 404 through the tunnel, `chatgpt.com` 200, `auth.openai.com` 200. Each CONNECT got 200.
- CONNECT to `example.com`, `ab.chatgpt.com`, `github.com`, `release-assets.githubusercontent.com`, `api.openai.com`, `statsig.anthropic.com`, `http-intake.logs.us5.datadoghq.com`, `sdmntprwestus3.oaiusercontent.com` and `downloads.claude.ai` got 403.
- A plain GET to `http://api.anthropic.com/` got 403. CONNECT to `api.anthropic.com` on ports 80, 22 and 8443 got 403.
- `http://127.0.0.1:3128/squid-internal-mgr/menu` through the proxy and `/squid-internal-mgr/info` sent straight to Squid both got 403. A raw `GET cache_object://localhost/info` got `400 Bad Request`.

**No lookup before the deny.** After a Squid restart to empty its DNS cache, `tcpdump -i any port 53` ran while `agent` requested `nolookup-2115524668.example.org`, `nolookup-2115524668.anthropic.com`, and then `chatgpt.com` as a control. Both random names got 403. The capture held only `A? chatgpt.com.` and `AAAA? chatgpt.com.` and no line with `nolookup`.

### Squid log format

The log format is unchanged from phase 1. Crafted requests went straight to Squid with `ncat`, and Python's `json.loads` parsed each journal line:

| Request | Logged `url` | Line parses? |
|---|---|---|
| `GET http://example.com/q"x":1,"y` | `http://example.com/q\"x\":1,\"y` | Yes |
| `GET http://example.com/back\slash` | `http://example.com/back\\slash` | Yes |
| `GET http://exa"mple.com/` | `http://exa\"mple.com/`, and `host` was `exa\"mple.com` | Yes |
| `CONNECT exa"mple.com:443` | `exa%22mple.com:443`, status 400 | Yes |
| `CONNECT chatgpt.com:443"` | `chatgpt.com:443%22`, status 400 | Yes |
| `GE"T http://example.com/` | `error:invalid-request`, status 400 | Yes |
| URL with ESC, `\x01` or `\x7f` | `error:invalid-request`, status 400 | Yes |
| URL with UTF-8 `é` | raw UTF-8 | Yes |
| URL with raw bytes `\xff\xfe` | the raw bytes | No: `UnicodeDecodeError` |

Squid 7.7 escaped `"` and `\` with a backslash in `url` and `host`, though the format sets no encoding. A quote can't end a field early or add a fake one. Squid rejected requests with control characters before logging them. Bytes 0x80 and above pass through unchanged, so a client can write a line that is not valid UTF-8. A strict JSON parser fails on that line. A parser has to decode with replacement, or the format can use Squid's `%#` (URL encoding) on request fields so every byte is printable ASCII. Fields named `bytes_out` and `bytes_in` hold `%>st` (request size from the client) and `%<st` (reply size to the client), so the names are backwards from the client's point of view. The automation ticket should rename them.

## CLIs and T3 server

Root installed all three. None of them update themselves, and no update host is on the allowlist.

Claude Code:

- Same apt repo and key as phase 1, fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE` checked before use. 2.1.311-1 is still the newest version in the stable channel.
- Installed `claude-code=2.1.311-1` and held it. `/usr/bin/claude` has the same sha256 as phase 1, `2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3`.
- `/etc/claude-code/managed-settings.json` adds `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` to phase 1's settings, since the telemetry host is now blocked.

Codex:

- `codex-package-aarch64-unknown-linux-musl.tar.gz` from release `rust-v0.169.0`. sha256 `c8d0b4ab2a97c595b70b10412d36d0d5464dcef1e1d3246b073d48017cdf5a4f`, which matches the digest GitHub's API gives for the asset and the phase 1 notes.
- Unpacked into `/opt/codex/0.169.0` with `--no-same-owner`, owned by root, no group or world write. `/usr/local/bin/codex` links to `bin/codex`. Both `bin/codex` and `bin/codex-code-mode-host` match the phase 1 hashes.
- `/etc/codex/managed_config.toml` is the same as phase 1. As `agent`, `codex features list` shows `apps false`. `plugins` is `true`. Phase 1 left it on, and it is not clear whether plugins can reach an account connector. The connector check will show that.

T3 server:

- T3's launcher script (phase 1, [T3](phase-1-notes.md#t3)) treats the runtime as installed when `~/.t3/runtime/versions/<version>/t3` is executable and `.install-complete` in that directory holds the version string. It then runs it and downloads nothing.
- Root downloaded `SHA256SUMS` and `t3-0.1.10-linux-arm64.tar.gz` from the `v0.1.10` release. `SHA256SUMS` matched GitHub's asset digest `46bb6407...`, and `sha256sum -c` passed for the archive (`5ffbd224...`, also GitHub's digest).
- Root unpacked it the way the launcher does (`--strip-components=1`) into `/home/agent/.t3/runtime/versions/0.1.10`, wrote `.install-complete` with `0.1.10`, and made the directory owned by root with no group or world write. `t3 --version` as `agent` printed `t3 v0.1.10`. `t3` sha256 is `1bd3c691d755fc1b46113f9067c535829146a30c62db2c126040297dd003e8f4`.
- `~/.t3`, `~/.t3/runtime` and `~/.t3/runtime/versions` belong to `agent`, because the launcher writes lock files and launch scripts there. That means `agent` can rename or replace the version directory: `mv 0.1.10 0.1.10.moved` worked as `agent` and was moved back. Root ownership stops edits in place, not replacement. This is the situation the tamper test covers.
- Whether T3 connects with no GitHub access and a root-owned runtime is pending the desktop app. If the server needs to write inside its own runtime directory, the connect will fail, and the fix is to make it `agent`-owned.

## Files agent can't write

As `agent`, appending to each of these failed with `Permission denied`: `/usr/bin/claude`, `/opt/codex/0.169.0/bin/codex`, `/opt/codex/0.169.0/bin/codex-code-mode-host`, `/usr/local/bin/codex`, `/etc/claude-code/managed-settings.json`, `/etc/codex/managed_config.toml`, `/etc/profile.d/wardian-claude-token.sh`, `/etc/environment`, `/etc/squid/squid.conf`, `/etc/nftables.conf`, and the T3 server binary.

Creating a file failed with `Permission denied` in `/etc/wardian`, `/etc/claude-code`, `/etc/codex`, `/opt/codex/0.169.0/bin`, `/usr/local/bin`, `/etc/profile.d` and the T3 runtime directory. `find /opt/codex /etc/claude-code /etc/codex /etc/wardian <t3 runtime> -writable` as `agent` printed nothing.

`/etc/wardian` is `root:agent 0750` and holds no token yet. The token file will be `root:agent 0640`, readable by `agent` and not writable.

`agent` can still write `~/.claude/settings.json` and `~/.codex/config.toml`. Both CLIs apply the root-owned managed files over those, as phase 1 tested for Codex.

## Linger

`/etc/polkit-1/rules.d/50-wardian-linger.rules` returns `NO` for `org.freedesktop.login1.set-self-linger` and `set-user-linger` when the subject is `agent`.

- As `agent`, `loginctl enable-linger` and `loginctl enable-linger agent` both print `Could not enable linger: Access denied` and exit 1. `/var/lib/systemd/linger/` has no `agent` file.
- As root, `loginctl enable-linger agent` exits 0 and creates `/var/lib/systemd/linger/agent`. `disable-linger` removed it again.
- Positive control: with the rule moved out and polkit restarted, `agent`'s `loginctl enable-linger` exited 0. After disabling it, restoring the rule and restarting polkit, it was denied again.

Lima turns on linger for its own `lima` admin account at boot. That account is outside `agent`'s reach.

## Time sync and timers

chrony is masked. The Lima host agent sets the guest clock, and its log reports drift around 65 ms (`Time sync: drift 65ms within threshold`, every 10 seconds). systemd-timesyncd is inactive. Allowing chrony would mean opening DNS and Ubuntu's NTS servers to `_chrony`, for no gain over the host agent.

Masked timers: `apt-daily`, `apt-daily-upgrade`, `motd-news`, `update-notifier-download`, `update-notifier-motd`, `fwupd-refresh`, plus two the phase 1 notes did not list: `ua-timer` (Ubuntu Pro client, runs `/usr/lib/ubuntu-advantage/timer.py` as root) and `apport-autoreport` (uploads crash reports when enabled). Phase 1 could not see them, because root traffic was not logged.

Timers still active, none of which needs the network: `sysstat-collect`, `sysstat-rotate`, `sysstat-summary`, `systemd-tmpfiles-clean`, `dpkg-db-backup`, `logrotate`, `man-db`, `e2scrub_all`, `xfs_scrub_all`, `fstrim`.

## DHCP

The lease from 192.168.5.2 lasts one hour. The guest got it at 23:36:03. A `tcpdump` on `eth0` for UDP 67 and 68 caught the unicast renewal at 30 minutes:

```
00:06:04.141632 IP 192.168.5.15.68 > 192.168.5.2.67: BOOTP/DHCP, Request from 52:55:55:ff:97:0c, length 292
00:06:04.142130 IP 192.168.5.2.67 > 192.168.5.15.68: BOOTP/DHCP, Reply, length 300
```

The kernel log had no `wardian-drop` line for port 67, and `ip addr` showed `valid_lft 3578sec` right after, so the lease was renewed. In phase 1 nftables dropped these renewals.

## Boot with a broken nftables.conf

With port 22 closed, `limactl shell` can't get in to repair the file, so a one-off unit did the checks and the repair from inside the guest. `wardian-boottest.service` (After and WantedBy `multi-user.target`) wrote the state of the firewall and SSH to `/var/log/wardian-boottest.log`, waited 120 seconds, copied a saved good config back, ran `systemctl start nftables.service` and `systemctl start ssh.socket`, and disabled itself. Root appended `this line is not nft syntax` to `/etc/nftables.conf` (`nft -c` reported `syntax error, unexpected newline` on line 65), then ran `limactl stop` and `limactl start --timeout 15m`.

The guest journal:

```
[1.614585] nft[628]: /etc/nftables.conf:65:28-28: Error: syntax error, unexpected newline
[1.595164] systemd[1]: nftables.service: Failed with result 'exit-code'.
[1.599662] systemd[1]: Dependency failed for ssh.socket - OpenBSD Secure Shell server socket.
[1.599692] systemd[1]: Dependency failed for ssh.service - OpenBSD Secure Shell server.
[1.599717] systemd[1]: ssh.socket: Job ssh.socket/start failed with result 'dependency'.
```

At 00:07:16 the harness recorded `nftables.service` failed, `ssh.socket` and `ssh.service` inactive, and no nft tables at all. `ss -ltn` showed only Squid on `127.0.0.1:3128`. `ss -A vsock` showed only the Lima guest agent on 2222, and no Unix socket named ssh existed. 120 seconds later nothing had changed.

From the Mac, `ssh -o BatchMode=yes wardian-spike true` ran every 5 seconds. From 00:07:15 to 00:09:16 every attempt failed with `kex_exchange_identification: read: Connection reset by peer`. The Lima host agent accepts on the Mac's port 60022 and resets the connection when the guest refuses. After the harness restored the config at 00:09:16, `nftables.service` and `ssh.socket` were active, both tables were loaded, port 22 listened, and the 00:09:21 attempt exited 0. `limactl start` waited for SSH and finished with `READY` at 00:09:18.

So a firewall that fails to load keeps SSH closed, and T3 can't reach the guest. With no table loaded, though, the guest has no outbound filtering either. Nothing ran as `agent` during that boot, since `agent` has no linger and logs in only through SSH. Squid was running and had no firewall around it.

After the test, root deleted the harness, checked the config with `nft -c`, and rebooted. The clean boot had `nftables`, `ssh.socket`, `squid` and `polkit` active, `table inet wardian` and `table ip nat` loaded, only `ssh.socket` on port 22, no `/run/ssh-unix-local`, and only the guest agent on vsock. As `agent`, `chatgpt.com` worked, `example.com` got 403, `getent` failed, linger was denied, and the toy repo was intact.

### What leaves before the rules load

The clean boot's journal shows the initramfs running its own systemd-networkd before the real root starts:

```
[0.220451] localhost systemd[1]: Reached target network-pre.target - Preparation for Network.
[0.296850] localhost systemd[1]: Starting systemd-networkd.service - Network Management...
[0.513301] localhost systemd-networkd[340]: enp0s1: DHCPv4 address 192.168.5.15/24, gateway 192.168.5.2 acquired from 192.168.5.2
[1.515923] lima-wardian-spike systemd[1]: Starting nftables.service - nftables...
[1.599759] lima-wardian-spike systemd[1]: Finished nftables.service - nftables.
[2.642212] lima-wardian-spike systemd[1]: Starting systemd-networkd.service - Network Management...
```

The initramfs contains networkd but no nftables, so a DHCP exchange with 192.168.5.2 happens about one second before the rules load. In the real root, nftables finishes before `network-pre.target` and networkd, as phase 1 read from the unit file. No user process exists during the initramfs stage, so this is DHCP and kernel IPv6 traffic only, and it goes to Lima's own address.

Drops logged during the clean boot: `systemd-network` router solicitations to `ff02::2`, two kernel MLD reports to `ff02::16`, and one UDP packet each from `_chrony` (UID 988) to `127.0.0.1:323` and `[::1]:323`. Port 323 is chronyd's command port, so something ran `chronyc` as `_chrony` and found no chronyd. Removing the chrony package would stop it.

## Pending user steps

These need the Keychain, a browser login or the T3 desktop app. They run in this order on the same VM.

1. Copy the Claude token into the guest over stdin, then check its prefix and length without printing it:

   ```sh
   security find-generic-password -s wardian-claude-token -w | tr -d '\n' \
     | limactl shell wardian-spike sudo sh -c 'umask 027; cat > /etc/wardian/claude-token; chown root:agent /etc/wardian/claude-token; chmod 0640 /etc/wardian/claude-token'
   limactl shell wardian-spike sudo sh -c 'case "$(head -c 10 /etc/wardian/claude-token)" in sk-ant-oat) echo "prefix ok";; *) echo "prefix WRONG";; esac; wc -c < /etc/wardian/claude-token; ls -l /etc/wardian/claude-token'
   ssh wardian-spike 'printf x >> /etc/wardian/claude-token; bash -lc "cd ~/toy && claude -p \"Reply with just the word pong\""'
   ```

   The append must fail with `Permission denied` and Claude must answer `pong`.

2. Log Codex in as `agent` and approve the device code in a browser:

   ```sh
   ssh -t wardian-spike 'bash -lc "codex login --device-auth"'
   ```

3. In the T3 desktop app, remove the phase 1 environment entry, then add a new one: Settings, Connections, Environments, Add environment, SSH, host `wardian-spike`. Open the project `/home/agent/toy`. Afterwards, `ssh wardian-spike 'ls -la ~/.t3/runtime/versions'` should show only the root-owned `0.1.10`, and the Squid log should have no `github.com` request.

4. In a T3 session for each agent, ask it to run `env | cut -d= -f1 | sort` and to list every tool it can call. Check that no tool acts on the Mac.

5. Run the toy task with each agent in full-access mode, as in phase 1. Then fetch the branches from the Mac with the phase 1 fetch command.

6. Tamper test: as `agent`, replace or modify the T3 server under `~/.t3/runtime/versions/0.1.10`, reconnect, and ask for a Mac-side tool. The desktop app must refuse.

7. Check connectors: Claude's `/mcp` list and Codex's apps and MCP list should show no account connector.

8. Run `codex cloud` as `agent` and try to list tasks. This may succeed. `codex cloud` most likely talks to `chatgpt.com`, which is on the allowlist for the model API, and Squid sees only the host name inside a CONNECT tunnel, not the path. If it lists tasks, this criterion fails and the notes must say so.

After these steps, the Squid log lists every host each agent asked for:

```sh
limactl shell wardian-spike sudo journalctl -u squid -o cat | grep '^{' | python3 -c 'import sys,json,collections; c=collections.Counter((d["host"],d["status"]) for d in map(json.loads,sys.stdin)); [print(n,h,s) for (h,s),n in c.most_common()]'
```

## Questions for the issue

- The issue says a T3 session has no Anthropic API variables. Claude Code logs in here through `CLAUDE_CODE_OAUTH_TOKEN`, which the login profile exports. Should the issue name that one variable as the allowed exception?
- If `codex cloud` works through `chatgpt.com`, the only fix at the proxy is TLS inspection, which the design avoids. Is a failing `codex cloud` check a no-go, or does the criterion change to "documented and accepted"?
- vsock is still open to `agent`. Phase 1 found no listener on the host side, and this run did not change that. The issue does not require closing it. Should a later ticket restrict `AF_VSOCK` for `agent`?
- Root may still reach public addresses on ports 80 and 443, now logged. Should the automated build drop root's outbound traffic entirely once provisioning is done?

## Guest files

Every file root wrote or changed, as they stand on the VM. The one-off test helpers (`/usr/local/bin/wardian-tcpprobe`, a Python connect probe, and the boot-test unit) were deleted after use and are not listed.

`/etc/nftables.conf`:

```
#!/usr/sbin/nft -f
# Wardian spike phase 2 (issue #3). Outbound policy is drop.
# Only this table is replaced; Lima's own "ip nat" table is left alone.

define resolver4 = 192.168.5.2
define squid_port = 3128

table inet wardian
delete table inet wardian

table inet wardian {
	set private4 {
		type ipv4_addr; flags interval
		elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
			     169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.168.0.0/16,
			     198.18.0.0/15, 224.0.0.0/4, 240.0.0.0/4 }
	}
	set private6 {
		type ipv6_addr; flags interval
		elements = { ::/127, ::ffff:0:0/96, 64:ff9b::/96, fc00::/7, fe80::/10, ff00::/8 }
	}

	counter dropped { }
	counter agent_dropped { }
	counter proxy_private_dropped { }
	counter root_new { }

	chain output {
		type filter hook output priority filter; policy drop;

		# 1. Replies and related traffic.
		ct state established,related accept

		# 2. The agent reaches Squid and its own non-DNS loopback ports, nothing else.
		oif "lo" meta skuid "agent" tcp dport $squid_port accept
		oif "lo" meta skuid "agent" meta l4proto { tcp, udp } th dport != 53 accept
		meta skuid "agent" counter name "agent_dropped" jump drop_logged

		# 3. Squid reaches the resolver on 53, then public addresses on 443 only.
		meta skuid "proxy" ip daddr $resolver4 meta l4proto { tcp, udp } th dport 53 accept
		meta skuid "proxy" ip daddr @private4 counter name "proxy_private_dropped" jump drop_logged
		meta skuid "proxy" ip6 daddr @private6 counter name "proxy_private_dropped" jump drop_logged
		meta skuid "proxy" tcp dport 443 accept

		# 4. DHCP lease renewals to Lima's DHCP server.
		meta skuid "systemd-network" ip daddr $resolver4 udp sport 68 udp dport 67 accept

		# 5. Root and apt: the resolver, then public addresses on 80 and 443,
		# each new connection logged. No private ranges, so not the Mac.
		meta skuid { "root", "_apt" } ip daddr $resolver4 meta l4proto { tcp, udp } th dport 53 accept
		meta skuid { "root", "_apt" } ip daddr @private4 jump drop_logged
		meta skuid { "root", "_apt" } ip6 daddr @private6 jump drop_logged
		meta skuid { "root", "_apt" } tcp dport { 80, 443 } limit rate 10/second burst 20 packets log prefix "wardian-root: " level info flags skuid
		meta skuid { "root", "_apt" } tcp dport { 80, 443 } counter name "root_new" accept

		# 6. Everything else.
		jump drop_logged
	}

	chain drop_logged {
		limit rate 10/second burst 20 packets log prefix "wardian-drop: " level info flags skuid
		counter name "dropped" drop
	}
}
```

`/etc/squid/squid.conf`:

```
# Wardian spike phase 2 (issue #3): the allowlist.
# Loopback only, CONNECT to 443 only, allowlisted names only.

http_port 127.0.0.1:3128
pinger_enable off
visible_hostname wardian-spike
cache deny all
dns_nameservers 192.168.5.2
forwarded_for delete
via off

acl CONNECT method CONNECT
acl SSL_ports port 443

# -n: match the name as the client sent it. Squid never looks up a name or
# reverses an IP address for this ACL, so an IP literal never matches.
acl allowlist dstdomain -n api.anthropic.com chatgpt.com auth.openai.com

# dst resolves the name, so it comes after the allowlist deny. It stops an
# allowlisted name that resolves to a private address.
acl to_private dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
acl to_private dst ::/127 64:ff9b::/96 fc00::/7 fe80::/10 ff00::/8

http_access deny manager
http_access deny !CONNECT
http_access deny !SSL_ports
http_access deny !allowlist
http_access deny to_private
http_access allow localhost
http_access deny all

# One JSON object per request, sent to syslog so it lands in the journal
# under squid.service.
logformat wardian_json {"time":"%{%Y-%m-%dT%H:%M:%S%z}tl","client":"%>a","method":"%rm","host":"%>rd","port":"%>rP","url":"%>ru","status":%>Hs,"squid_status":"%Ss","server_ip":"%<a","bytes_out":%>st,"bytes_in":%<st,"ms":%tr}
access_log syslog:daemon.info wardian_json
cache_log /var/log/squid/cache.log
```

`/etc/ssh/sshd_config.d/50-wardian-agent.conf`:

```
# Wardian: limits on what an SSH session as agent may ask for.
# T3 forwards a Mac port to its server on 127.0.0.1:3773; nothing else may be opened.
Match User agent
  AllowTcpForwarding local
  PermitOpen 127.0.0.1:3773
  PermitListen none
  AllowAgentForwarding no
  AllowStreamLocalForwarding no
  X11Forwarding no
  PermitTunnel no
```

`/etc/systemd/system/ssh.socket.d/50-wardian-nftables.conf`:

```
# Wardian: never listen for SSH unless the firewall loaded.
# Requires= stops this socket if nftables.service fails or stops;
# After= makes it wait until the rules are in place.
[Unit]
Requires=nftables.service
After=nftables.service
```

`/etc/systemd/system/nftables.service.d/50-wardian.conf`:

```
# Wardian: stopping this unit leaves the rules loaded. The packaged ExecStop
# runs "nft flush ruleset", which removes every table, Lima's included, and
# would leave agent processes with no firewall. Requires= on ssh.socket still
# stops SSH when this unit stops.
[Service]
ExecStop=
```

`/etc/systemd/system/squid.service.d/50-wardian.conf`:

```
[Service]
LogRateLimitIntervalSec=0
```

`/etc/claude-code/managed-settings.json`:

```
{
  "env": {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_UPDATES": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "disableClaudeAiConnectors": true
}
```

`/etc/codex/managed_config.toml`:

```
# Wardian: root-owned. Codex 0.169.0 applies this file over ~/.codex/config.toml and -c flags.
check_for_update_on_startup = false

[features]
# ChatGPT apps (connectors).
apps = false
```

`/etc/profile.d/wardian-claude-token.sh`:

```
# Wardian: export the Claude token only for users who can read it (the agent group).
if [ -r /etc/wardian/claude-token ]; then
	CLAUDE_CODE_OAUTH_TOKEN="$(cat /etc/wardian/claude-token)"
	export CLAUDE_CODE_OAUTH_TOKEN
fi
```

`/etc/polkit-1/rules.d/50-wardian-linger.rules`:

```
// Wardian: agent may not turn on linger, so none of its processes outlive
// its sessions or start at boot. Root does not go through polkit.
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.login1.set-self-linger" ||
         action.id == "org.freedesktop.login1.set-user-linger") &&
        subject.user == "agent") {
        return polkit.Result.NO;
    }
});
```

`/etc/resolv.conf`:

```
# Wardian: static resolver (Lima host agent). systemd-resolved is masked.
nameserver 192.168.5.2
```

`/etc/environment` gained this block after Lima's own lines:

```
# BEGIN WARDIAN PROXY
http_proxy=http://127.0.0.1:3128
https_proxy=http://127.0.0.1:3128
HTTP_PROXY=http://127.0.0.1:3128
HTTPS_PROXY=http://127.0.0.1:3128
no_proxy=localhost,127.0.0.1,::1
NO_PROXY=localhost,127.0.0.1,::1
# END WARDIAN PROXY
```

Masked units: `sshd-vsock.socket`, `sshd-unix-local.socket`, `systemd-resolved.service`, `systemd-resolved-varlink.socket`, `systemd-resolved-monitor.socket`, `chrony.service`, `apt-daily.timer`, `apt-daily-upgrade.timer`, `motd-news.timer`, `update-notifier-download.timer`, `update-notifier-motd.timer`, `fwupd-refresh.timer`, `ua-timer.timer`, `apport-autoreport.timer`.
