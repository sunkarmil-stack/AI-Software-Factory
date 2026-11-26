# Azure reference infrastructure for one service namespace. Applied only by the scoped
# deployment identity via the promotion pipeline; agents may propose changes via PR.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "= 4.14.0" }
  }
}

variable "environment" { type = string }
variable "service_name" { type = string }
variable "resource_group" { type = string }
variable "location" { type = string }

resource "azurerm_user_assigned_identity" "workload" {
  name                = "${var.service_name}-${var.environment}-id"
  resource_group_name = var.resource_group
  location            = var.location
}

resource "azurerm_key_vault" "svc" {
  name                          = "${var.service_name}-${var.environment}-kv"
  resource_group_name           = var.resource_group
  location                      = var.location
  tenant_id                     = "00000000-0000-0000-0000-000000000000"
  sku_name                      = "standard"
  public_network_access_enabled = false
  purge_protection_enabled      = true
  enable_rbac_authorization     = true
}
