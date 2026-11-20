# Wardian

Wardian runs AI coding agents (Claude Code and Codex) in disposable Lima VMs on a Mac, so a developer using T3 Code doesn't have to worry about agents reaching their other projects, sensitive files or API keys. Each agent sees one project's code and can install what that project needs from the internet, but it can't reach the Mac or the local network. It is not fool-proof containment. The spec is GitHub issue #1, and `docs/adr/` records why the design is shaped this way.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
