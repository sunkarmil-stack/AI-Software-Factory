# Private AKS: Entra-only auth, workload identity, Azure CNI + network policy,
# a dedicated Kata (pod sandboxing) node pool for untrusted build/test jobs.
resource "azurerm_kubernetes_cluster" "aks" {
  name                              = "aks-${local.name}"
  resource_group_name               = azurerm_resource_group.rg.name
  location                          = var.location
  dns_prefix                        = local.name
  kubernetes_version                = var.kubernetes_version
  private_cluster_enabled           = true
  sku_tier                          = "Standard"
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  local_account_disabled            = true
  role_based_access_control_enabled = true
  automatic_upgrade_channel         = "patch"
  azure_policy_enabled              = true
  tags                              = local.tags

  azure_active_directory_role_based_access_control {
    tenant_id              = data.azurerm_client_config.current.tenant_id
    admin_group_object_ids = var.aks_admin_group_ids
    azure_rbac_enabled     = true
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = "Standard_D4ds_v5"
    vnet_subnet_id               = azurerm_subnet.aks.id
    auto_scaling_enabled         = true
    min_count                    = 3
    max_count                    = 6
    zones                        = ["1", "2", "3"]
    only_critical_addons_enabled = true
    os_sku                       = "AzureLinux"
    upgrade_settings { max_surge = "33%" }
  }

  identity { type = "SystemAssigned" }

  network_profile {
    network_plugin    = "azure"
    network_policy    = "cilium"
    network_data_plane = "cilium"
    outbound_type     = "userDefinedRouting" # egress via the enterprise firewall; route table owned by the hub
    load_balancer_sku = "standard"
  }

  oms_agent { log_analytics_workspace_id = azurerm_log_analytics_workspace.law.id }

  key_vault_secrets_provider { secret_rotation_enabled = true }
}

resource "azurerm_kubernetes_cluster_node_pool" "app" {
  name                  = "app"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = "Standard_D8ds_v5"
  vnet_subnet_id        = azurerm_subnet.aks.id
  auto_scaling_enabled  = true
  min_count             = 3
  max_count             = var.app_max_nodes
  zones                 = ["1", "2", "3"]
  os_sku                = "AzureLinux"
  node_labels           = { "factory/pool" = "app" }
  tags                  = local.tags
}

# Pod sandboxing (Kata) pool: only sandbox Jobs tolerate its taint.
resource "azurerm_kubernetes_cluster_node_pool" "sandbox" {
  name                  = "sandbox"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = "Standard_D8ds_v5" # must support nested virtualization
  vnet_subnet_id        = azurerm_subnet.aks.id
  workload_runtime      = "KataVmIsolation"
  os_sku                = "AzureLinux"
  auto_scaling_enabled  = true
  min_count             = 1
  max_count             = var.sandbox_max_nodes
  zones                 = ["1", "2", "3"]
  node_labels           = { "factory/pool" = "sandbox" }
  node_taints           = ["factory/sandbox=true:NoSchedule"]
  tags                  = local.tags
}

resource "azurerm_container_registry" "acr" {
  name                          = replace("acr${local.name}", "-", "")
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = var.location
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = false
  zone_redundancy_enabled       = true
  tags                          = local.tags
}

resource "azurerm_private_endpoint" "acr" {
  name                = "pe-${local.name}-acr"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  subnet_id           = azurerm_subnet.pe.id
  private_service_connection {
    name                           = "acr"
    private_connection_resource_id = azurerm_container_registry.acr.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "acr"
    private_dns_zone_ids = [azurerm_private_dns_zone.z["acr"].id]
  }
}

resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}
