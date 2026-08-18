terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }

  # Local state by default. To share state across operators, uncomment and point at
  # a GCS backend:
  #
  # backend "gcs" {
  #   bucket = "..."
  #   prefix = "gke-slim"
  # }
}
