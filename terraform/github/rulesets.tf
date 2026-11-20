resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = "wardian"
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  # No bypass_actors block: the rules apply to the repo admin too.
  # No approvals or status checks yet. Issue #13 adds the CI checks.
  rules {
    deletion         = true
    non_fast_forward = true
  }
}
