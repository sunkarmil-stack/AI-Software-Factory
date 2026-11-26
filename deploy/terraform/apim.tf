# Enterprise AI gateway (Azure API Management, internal VNet mode).
# Model, MCP and enterprise-API traffic from the factory egresses ONLY through here.
# In most banks this APIM is owned by the enterprise integration team; set var.create_apim=false and
# pass var.existing_apim_id to register the factory's products/subscriptions on the shared instance.
resource "azurerm_api_management" "aigw" {
  count                         = var.create_apim ? 1 : 0
  name                          = "apim-${local.name}"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = var.location
  publisher_name                = var.apim_publisher_name
  publisher_email               = var.apim_publisher_email
  sku_name                      = "Premium_${var.apim_units}"
  zones                         = var.apim_units >= 3 ? ["1", "2", "3"] : null
  virtual_network_type          = "Internal"
  public_network_access_enabled = true # required at create time for Internal mode; no public gateway endpoint
  min_api_version               = "2021-08-01"
  tags                          = local.tags
  virtual_network_configuration { subnet_id = azurerm_subnet.apim.id }
  identity { type = "SystemAssigned" }
  security {
    tls_rsa_with_aes128_cbc_sha_ciphers_enabled = false
    triple_des_ciphers_enabled                  = false
  }
}

locals {
  apim_id   = var.create_apim ? azurerm_api_management.aigw[0].id : var.existing_apim_id
  apim_name = var.create_apim ? azurerm_api_management.aigw[0].name : element(split("/", var.existing_apim_id), 8)
  apim_principal = var.create_apim ? azurerm_api_management.aigw[0].identity[0].principal_id : var.existing_apim_principal_id
}

resource "azurerm_api_management_logger" "ai" {
  name                = "appinsights"
  api_management_name = local.apim_name
  resource_group_name = azurerm_resource_group.rg.name
  resource_id         = azurerm_application_insights.ai.id
  application_insights { connection_string = azurerm_application_insights.ai.connection_string }
}

# Upstream model deployments (Azure OpenAI / Azure AI Foundry incl. Claude) are registered as backends.
resource "azurerm_api_management_backend" "model" {
  for_each            = var.model_backends
  name                = each.key
  api_management_name = local.apim_name
  resource_group_name = azurerm_resource_group.rg.name
  protocol            = "http"
  url                 = each.value.url
  circuit_breaker_rule {
    name                     = "trip-on-5xx-429"
    trip_duration            = "PT1M"
    accept_retry_after       = true
    failure_condition {
      count             = 5
      interval_duration = "PT1M"
      status_code_range { min = 429, max = 429 }
      status_code_range { min = 500, max = 599 }
    }
  }
}

resource "azurerm_api_management_api" "models" {
  name                  = "factory-models"
  api_management_name   = local.apim_name
  resource_group_name   = azurerm_resource_group.rg.name
  revision              = "1"
  display_name          = "Factory model access (OpenAI-compatible)"
  path                  = "models"
  protocols             = ["https"]
  subscription_required = false # identity is the Entra workload token, validated in policy
}

resource "azurerm_api_management_api_policy" "models" {
  api_name            = azurerm_api_management_api.models.name
  api_management_name = local.apim_name
  resource_group_name = azurerm_resource_group.rg.name
  xml_content = templatefile("${path.module}/../apim/models-policy.xml", {
    tenant_id      = data.azurerm_client_config.current.tenant_id
    gateway_aud    = var.gateway_audience
    allowed_appids = join(",", [for k, v in azurerm_user_assigned_identity.wl : v.client_id if k != "migrate"])
    tpm_limit      = var.factory_tokens_per_minute
    monthly_quota  = var.factory_tokens_per_month
  })
}

resource "azurerm_api_management_api" "enterprise" {
  for_each              = var.enterprise_apis
  name                  = "factory-${each.key}"
  api_management_name   = local.apim_name
  resource_group_name   = azurerm_resource_group.rg.name
  revision              = "1"
  display_name          = "Factory access to ${each.key}"
  path                  = "apis/${each.key}"
  protocols             = ["https"]
  service_url           = each.value.url
  subscription_required = false
}

resource "azurerm_api_management_api_policy" "enterprise" {
  for_each            = var.enterprise_apis
  api_name            = azurerm_api_management_api.enterprise[each.key].name
  api_management_name = local.apim_name
  resource_group_name = azurerm_resource_group.rg.name
  xml_content = templatefile("${path.module}/../apim/enterprise-api-policy.xml", {
    tenant_id       = data.azurerm_client_config.current.tenant_id
    gateway_aud     = var.gateway_audience
    allowed_appids  = join(",", [for k, v in azurerm_user_assigned_identity.wl : v.client_id if k != "migrate"])
    backend_aud     = each.value.audience
    calls_per_min   = each.value.calls_per_minute
    write_methods   = join(",", each.value.write_methods)
  })
}

# MCP servers fronted by APIM (APIM "MCP server" APIs are created by the platform team; this grants routing).
resource "azurerm_api_management_named_value" "kill_switch" {
  name                = "factory-enabled"
  api_management_name = local.apim_name
  resource_group_name = azurerm_resource_group.rg.name
  display_name        = "factory-enabled"
  value               = "true" # enterprise kill switch: set to false to revoke ALL factory egress (runbook RB-11)
  lifecycle { ignore_changes = [value] } # operated by runbook, not by terraform
}

resource "azurerm_api_management_named_value" "revoked" {
  for_each            = toset(["revoked-models", "revoked-apis"])
  name                = each.value
  api_management_name = local.apim_name
  resource_group_name = azurerm_resource_group.rg.name
  display_name        = each.value
  value               = "none" # comma-separated aliases / api names revoked by Model Risk or the SoR owner
  lifecycle { ignore_changes = [value] }
}
