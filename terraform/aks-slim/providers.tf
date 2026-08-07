provider "azurerm" {
  features {}

  # Null falls back to ARM_SUBSCRIPTION_ID from the environment; set var.subscription_id
  # to pin a specific subscription.
  subscription_id = var.subscription_id
}
