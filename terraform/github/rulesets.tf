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
  # Zero approvals: a pull request can merge once the CircleCI jobs pass.
  rules {
    deletion         = true
    non_fast_forward = true

    pull_request {
      required_approving_review_count = 0
    }

    # The contexts the CircleCI GitHub App posts for the ci workflow's jobs.
    required_status_checks {
      strict_required_status_checks_policy = false

      required_check {
        context = "ci/circleci: python"
      }
      required_check {
        context = "ci/circleci: incus"
      }
      required_check {
        context = "ci/circleci: mutation"
      }
    }
  }
}
