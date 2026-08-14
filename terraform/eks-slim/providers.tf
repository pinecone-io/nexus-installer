provider "aws" {
  region = var.region

  # Credentials come from the environment (AWS_PROFILE / AWS_ACCESS_KEY_ID, ...).
  default_tags {
    tags = local.tags
  }
}
