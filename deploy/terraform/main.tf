# AI Software Factory - Azure production landing zone (one environment per state file).
# STATUS: authored against azurerm 4.14.0 docs; NOT applied or `terraform validate`d in the build environment.
terraform {
  required_version = ">= 1.7"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "= 4.14.0" }
    azuread = { source = "hashicorp/azuread", version = "~> 3.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "azurerm" {} # configured per environment: -backend-config=env/<env>.backend.hcl
}

provider "azurerm" {
  features {
    key_vault { purge_soft_delete_on_destroy = false }
  }
  storage_use_azuread = true
}

data "azurerm_client_config" "current" {}

locals {
  name = "asf-${var.environment}"
  tags = merge(var.tags, { system = "ai-software-factory", environment = var.environment, data_class = "confidential" })
  # Workloads that get an Entra identity federated to a Kubernetes service account.
  workloads = {
    api     = { namespace = "factory", sa = "factory-api" }
    worker  = { namespace = "factory", sa = "factory-worker" }
    migrate = { namespace = "factory", sa = "factory-migrate" }
  }
  # Factory signing identities -> Key Vault EC keys (must match keyvault.keys in values.yaml).
  signing_keys = ["factory-ci-builder", "factory-release", "factory-skill-registry", "factory-approval", "factory-policy"]
}

resource "azurerm_resource_group" "rg" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ------------------------------------------------------------------ observability
resource "azurerm_log_analytics_workspace" "law" {
  name                = "law-${local.name}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = local.tags
}

resource "azurerm_application_insights" "ai" {
  name                = "appi-${local.name}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  workspace_id        = azurerm_log_analytics_workspace.law.id
  application_type    = "web"
  tags                = local.tags
}

# ------------------------------------------------------------------ identities
resource "azurerm_user_assigned_identity" "wl" {
  for_each            = local.workloads
  name                = "id-${local.name}-${each.key}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "wl" {
  for_each            = local.workloads
  name                = "fic-${each.key}"
  resource_group_name = azurerm_resource_group.rg.name
  parent_id           = azurerm_user_assigned_identity.wl[each.key].id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject             = "system:serviceaccount:${each.value.namespace}:${each.value.sa}"
}
