# Terraform + provider version pins for the aks-slim AKS package.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Dev uses local state (mirrors the deliberate LOCAL-backend choice on this
  # workstream). To share state, uncomment and point at an Azure blob container:
  #
  # backend "azurerm" {
  #   resource_group_name  = "..."
  #   storage_account_name = "..."
  #   container_name       = "tfstate"
  #   key                  = "aks-dev.tfstate"
  # }
}
