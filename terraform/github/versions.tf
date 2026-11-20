terraform {
  required_version = "~> 1.17"

  required_providers {
    github = {
      source  = "integrations/github"
      version = "~> 6.13"
    }
  }
}

# No token here: with GITHUB_TOKEN unset, the provider runs `gh auth token`.
provider "github" {
  owner = "MMaahir"
}
