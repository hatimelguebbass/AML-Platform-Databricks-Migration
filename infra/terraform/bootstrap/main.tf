# Remote state of the prod configuration. Its own state lives in the account it creates: the first
# apply runs on local state, then `terraform init -migrate-state` moves it there (docs/migration-plan.md).
terraform {
  required_version = "~> 1.17.0"

  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.14"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # The account refuses shared keys: data-plane calls authenticate with Entra ID.
  storage_use_azuread = true
}

data "azurerm_client_config" "current" {}

locals {
  # Storage account names are global: a suffix derived from the subscription keeps reruns stable.
  suffix = substr(sha1(var.subscription_id), 0, 6)
  tags = {
    project    = "aml-platform-databricks-migration"
    managed_by = "terraform/bootstrap"
  }
}

resource "azurerm_resource_group" "state" {
  name     = "rg-amlmig-tfstate"
  location = var.location
  tags     = local.tags
}

resource "azurerm_storage_account" "state" {
  name                            = "stamlmigstate${local.suffix}"
  resource_group_name             = azurerm_resource_group.state.name
  location                        = azurerm_resource_group.state.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  default_to_oauth_authentication = true
  tags                            = local.tags

  blob_properties {
    # A corrupted or deleted state is restored from a previous blob version.
    versioning_enabled = true
    delete_retention_policy {
      days = 7
    }
    container_delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "state" {
  name               = "tfstate"
  storage_account_id = azurerm_storage_account.state.id
}

resource "azurerm_role_assignment" "state_operator" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}
