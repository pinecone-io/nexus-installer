provider "google" {
  project = var.project
  region  = var.region

  # Credentials come from the environment: Application Default Credentials
  # (GOOGLE_APPLICATION_CREDENTIALS / `gcloud auth application-default login`) or a
  # GOOGLE_OAUTH_ACCESS_TOKEN. No key material lives in the module.
}
