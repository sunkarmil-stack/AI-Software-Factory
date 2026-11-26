# Feed these into deploy/helm/ai-software-factory/values-<env>.yaml (see docs/production.md §3).
output "workload_client_ids" {
  value = { for k, v in azurerm_user_assigned_identity.wl : k => v.client_id }
}
output "tenant_id" { value = data.azurerm_client_config.current.tenant_id }
output "key_vault_url" { value = azurerm_key_vault.kv.vault_uri }
output "postgres_fqdn" { value = azurerm_postgresql_flexible_server.pg.fqdn }
output "acr_login_server" { value = azurerm_container_registry.acr.login_server }
output "ai_gateway_url" {
  value = var.create_apim ? "${azurerm_api_management.aigw[0].gateway_url}/" : ""
}
output "otel_endpoint" {
  value       = "http://otel-collector.observability:4318"
  description = "In-cluster OpenTelemetry Collector exporting to Application Insights"
}
output "app_insights_connection_string" {
  value     = azurerm_application_insights.ai.connection_string
  sensitive = true
}
output "evidence_container_url" {
  value = "${azurerm_storage_account.evidence.primary_blob_endpoint}${azurerm_storage_container.evidence.name}"
}
