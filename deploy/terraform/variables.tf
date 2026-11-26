variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "nonprod", "prod"], var.environment)
    error_message = "environment must be dev, nonprod or prod"
  }
}
variable "location" {
  type    = string
  default = "canadacentral"
}
variable "tags" {
  type    = map(string)
  default = {}
}
variable "vnet_cidr" {
  type    = string
  default = "10.40.0.0/16"
}
variable "kubernetes_version" {
  type    = string
  default = "1.31"
}
variable "aks_admin_group_ids" { type = list(string) }
variable "app_max_nodes" {
  type    = number
  default = 10
}
variable "sandbox_max_nodes" {
  type    = number
  default = 10
}
variable "pg_sku" {
  type    = string
  default = "GP_Standard_D4ds_v5"
}
variable "pg_storage_mb" {
  type    = number
  default = 262144
}
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "evidence_retention_days" {
  type        = number
  default     = 2555 # 7 years; confirm with Records Management
  description = "WORM retention for exported evidence"
}

# --- enterprise AI gateway
variable "create_apim" {
  type    = bool
  default = true
}
variable "existing_apim_id" {
  type    = string
  default = ""
}
variable "existing_apim_principal_id" {
  type    = string
  default = ""
}
variable "apim_units" {
  type    = number
  default = 3
}
variable "apim_publisher_name" {
  type    = string
  default = "AI Platform"
}
variable "apim_publisher_email" { type = string }
variable "gateway_audience" {
  type    = string
  default = "api://enterprise-ai-gateway"
}
variable "factory_tokens_per_minute" {
  type    = number
  default = 200000
}
variable "factory_tokens_per_month" {
  type    = number
  default = 500000000
}
variable "model_backends" {
  description = "Upstream model endpoints keyed by gateway alias (claude-delivery, gpt-delivery, rag-assistant ...)"
  type = map(object({
    url = string
  }))
  default = {}
}
variable "enterprise_apis" {
  description = "Systems of record the factory may call through the gateway (deposits, payments, data-platform)"
  type = map(object({
    url              = string
    audience         = string
    calls_per_minute = number
    write_methods    = list(string)
  }))
  default = {}
}
