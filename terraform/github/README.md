# GitHub repo rulesets

Terraform manages the rulesets on `MMaahir/wardian`. The `main` ruleset blocks force-pushes and deletion of the default branch for everyone, including the repo admin. It requires no approvals or status checks yet.

## Requirements

- Terraform 1.17 (`brew install hashicorp/tap/terraform`)
- The `gh` CLI, logged in as a repo admin. With `GITHUB_TOKEN` unset, the provider takes its token from `gh auth token`.

## Change a ruleset

Run these from this directory:

```sh
terraform init
terraform plan -out=main.tfplan
terraform apply main.tfplan
rm main.tfplan
terraform plan
```

The last `plan` should report no changes. If it shows changes you didn't make, someone edited the ruleset on GitHub. Apply again to put it back, or copy their change into the `.tf` files.

## State

State stays local in `terraform.tfstate`. Git ignores it, the `.terraform/` folder, and `*.tfplan` files (see the root `.gitignore`). Commit `.terraform.lock.hcl`. It pins the provider version and its checksums for macOS and Linux.

After every apply, copy the state to a private folder outside the repo:

```sh
dir="$HOME/Library/Application Support/wardian/terraform-state/github"
(umask 077 && mkdir -p "$dir" && cp terraform.tfstate "$dir/terraform.tfstate.$(date -u +%Y%m%dT%H%M%SZ)")
```

`umask 077` makes the folder and the copy readable only by you. To restore, copy the newest backup back to `terraform.tfstate`.

If both the state and the backups are lost, import the ruleset again. Its ID is in the URL of the ruleset page on GitHub:

```sh
terraform import github_repository_ruleset.main wardian:<ruleset-id>
```
