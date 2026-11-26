# ------------------------------------------------------------------ PostgreSQL (control-plane state)
resource "azurerm_postgresql_flexible_server" "pg" {
  name                          = "psql-${local.name}"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = var.location
  version                       = "16"
  sku_name                      = var.pg_sku
  storage_mb                    = var.pg_storage_mb
  auto_grow_enabled             = true
  delegated_subnet_id           = azurerm_subnet.pg.id
  private_dns_zone_id           = azurerm_private_dns_zone.z["pg"].id
  public_network_access_enabled = false
  backup_retention_days         = 35
  geo_redundant_backup_enabled  = true
  zone                          = "1"
  tags                          = local.tags

  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  maintenance_window {
    day_of_week  = 0
    start_hour   = 3
    start_minute = 0
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.z]
}

resource "azurerm_postgresql_flexible_server_database" "factory" {
  name      = "factory"
  server_id = azurerm_postgresql_flexible_server.pg.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_configuration" "tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_configuration" "audit" {
  name      = "pgaudit.log"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "DDL,ROLE"
}

# Workload identities become Entra principals on the server; the migrate identity owns the schema,
# api/worker get DML only (granted by migrations/postgres/bootstrap/000_roles.sql run as the migrate principal).
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "migrate" {
  server_name         = azurerm_postgresql_flexible_server.pg.name
  resource_group_name = azurerm_resource_group.rg.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = azurerm_user_assigned_identity.wl["migrate"].principal_id
  principal_name      = azurerm_user_assigned_identity.wl["migrate"].name
  principal_type      = "ServicePrincipal"
}

# ------------------------------------------------------------------ Key Vault (signing keys, secrets)
resource "azurerm_key_vault" "kv" {
  name                          = substr("kv-${local.name}-${random_string.sfx.result}", 0, 24)
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = var.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "premium" # HSM-backed keys
  enable_rbac_authorization     = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false
  tags                          = local.tags
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
  }
}

resource "random_string" "sfx" {
  length  = 4
  special = false
  upper   = false
}

resource "azurerm_private_endpoint" "kv" {
  name                = "pe-${local.name}-kv"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  subnet_id           = azurerm_subnet.pe.id
  private_service_connection {
    name                           = "kv"
    private_connection_resource_id = azurerm_key_vault.kv.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "kv"
    private_dns_zone_ids = [azurerm_private_dns_zone.z["kv"].id]
  }
}

resource "azurerm_key_vault_key" "signing" {
  for_each     = toset(local.signing_keys)
  name         = each.value
  key_vault_id = azurerm_key_vault.kv.id
  key_type     = "EC-HSM"
  curve        = "P-256"
  key_opts     = ["sign", "verify"]
  rotation_policy {
    automatic { time_before_expiry = "P30D" }
    expire_after         = "P1Y"
    notify_before_expiry = "P45D"
  }
  depends_on = [azurerm_role_assignment.kv_admin]
}

resource "azurerm_role_assignment" "kv_admin" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = data.azurerm_client_config.current.object_id
}

# Least privilege: worker signs with build/release keys; api signs approvals/policy decisions.
locals {
  key_grants = {
    "worker/factory-ci-builder"     = { wl = "worker", key = "factory-ci-builder" }
    "worker/factory-release"        = { wl = "worker", key = "factory-release" }
    "worker/factory-skill-registry" = { wl = "worker", key = "factory-skill-registry" }
    "worker/factory-policy"         = { wl = "worker", key = "factory-policy" }
    "api/factory-approval"          = { wl = "api", key = "factory-approval" }
    "api/factory-policy"            = { wl = "api", key = "factory-policy" }
  }
}

resource "azurerm_role_assignment" "kv_sign" {
  for_each             = local.key_grants
  scope                = "${azurerm_key_vault.kv.id}/keys/${each.value.key}"
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.wl[each.value.wl].principal_id
  depends_on           = [azurerm_key_vault_key.signing]
}

resource "azurerm_role_assignment" "kv_verify" {
  for_each             = local.workloads
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Crypto Service Encryption User" # read public keys for verification
  principal_id         = azurerm_user_assigned_identity.wl[each.key].principal_id
}

resource "azurerm_role_assignment" "kv_secrets" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.wl["worker"].principal_id
}

# ------------------------------------------------------------------ evidence archive (WORM)
resource "azurerm_storage_account" "evidence" {
  name                            = substr(replace("st${local.name}ev${random_string.sfx.result}", "-", ""), 0, 24)
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "GZRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  public_network_access_enabled   = false
  allow_nested_items_to_be_public = false
  tags                            = local.tags
  blob_properties {
    versioning_enabled = true
    delete_retention_policy { days = 365 }
  }
}

resource "azurerm_storage_container" "evidence" {
  name                  = "evidence"
  storage_account_id    = azurerm_storage_account.evidence.id
  container_access_type = "private"
}

resource "azurerm_storage_container_immutability_policy" "evidence" {
  storage_container_resource_manager_id = azurerm_storage_container.evidence.resource_manager_id
  immutability_period_in_days           = var.evidence_retention_days
  protected_append_writes_all_enabled   = true
  locked                                = var.environment == "prod"
}

resource "azurerm_private_endpoint" "blob" {
  name                = "pe-${local.name}-blob"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  subnet_id           = azurerm_subnet.pe.id
  private_service_connection {
    name                           = "blob"
    private_connection_resource_id = azurerm_storage_account.evidence.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [azurerm_private_dns_zone.z["blob"].id]
  }
}

resource "azurerm_role_assignment" "evidence_writer" {
  for_each             = toset(["api", "worker"])
  scope                = azurerm_storage_container.evidence.resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.wl[each.key].principal_id
}
