terraform {
  required_version = "~> 1.17.0"

  # Settings in backend.hcl (outputs of ../bootstrap): terraform init -backend-config=backend.hcl
  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.14"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.13"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.10"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.142"
    }
  }
}
