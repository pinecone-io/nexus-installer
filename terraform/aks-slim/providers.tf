provider "azurerm" {
  features {}

  # When null, the provider falls back to ARM_SUBSCRIPTION_ID from the environment
  # (source .byoc.azure.local.env). Set var.subscription_id to pin a specific sub.
  subscription_id = var.subscription_id
}
