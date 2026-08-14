terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
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
  # an S3 backend:
  #
  # backend "s3" {
  #   bucket = "..."
  #   key    = "eks-slim.tfstate"
  #   region = "us-east-1"
  # }
}
