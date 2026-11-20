---
status: accepted
---

# Open egress instead of an allowlist

Wardian first confined agents to a three-host Squid allowlist (`api.anthropic.com`, `chatgpt.com`, `auth.openai.com`) with no DNS and no sudo. That kept them away from the Mac, but they couldn't install anything a project needs. On 2026-11-20 the goal changed: protect the developer's other projects, sensitive Mac files and host credentials, while agents install packages, change the project and hand back code the developer can trust. Exfiltration of the project itself is no longer a goal. So the agent gets the open internet over IPv4 with DNS, and nftables drops every private, link-local and metadata IPv4 address and all non-loopback IPv6 for every user, which keeps the Mac and the LAN out of reach apart from DNS and DHCP to Lima's gateway. A Mac with a public IPv4 address, or a service forwarded to it by the router, stays reachable; issue #1 lists this as an accepted risk. Squid and the proxy settings are deleted. A two-model council (Claude Opus 5.5, GPT-6.2 Sol) reviewed the change over seven rounds; the details are in issue #1.

## Considered options

- **Keep the allowlist and add package registries.** Every project needs a different set of hosts, so the list never stays complete, and tools that ignore proxy variables still fail.
- **Keep Squid with an open allowlist, for hostname logs.** Logs stop being a goal under the new scope, and the proxy would still break tools that ignore `HTTPS_PROXY`.
- **System packages listed at `create` only, with no sudo.** Safer, but the agent can't add a package mid-task, which the goal asks for. Instead `agent` gets one sudo grant, `wardian-apt`, which takes only package names, installs only new packages, and powers the VM off if the firewall changed afterwards.

## Consequences

- The agent can send the project and its own login anywhere. The residual risks in issue #1 list this and the rest.
- Each sandbox holds one agent's login (`create --agent`), so a compromise exposes one account.
- Code still moves only by git over SSH. `wardian push` and `wardian fetch` replace the hand-written git setup and connect only to the sandbox.
- The spike's allowlist and Squid results in `spike/` are history, not requirements.
