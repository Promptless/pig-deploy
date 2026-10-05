terraform {
  backend "azurerm" {
    use_azuread_auth = true
  }
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 4.36.0"
    }
  }
}
