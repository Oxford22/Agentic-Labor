variable "hcloud_token" {
  description = "Hetzner Cloud API token. Set via HCLOUD_TOKEN env var or Vault."
  type        = string
  sensitive   = true
}

variable "environment" {
  description = "Deployment environment label."
  type        = string
  default     = "production-fra"
  validation {
    condition     = contains(["development", "staging", "production-fra"], var.environment)
    error_message = "environment must be one of development, staging, production-fra"
  }
}

variable "ha_pair" {
  description = "Deploy a 2-node HA pair behind a Hetzner load balancer."
  type        = bool
  default     = true
}

variable "clickhouse_volume_gb" {
  description = "Volume size in GiB for the ClickHouse data disk per node."
  type        = number
  default     = 500
  validation {
    condition     = var.clickhouse_volume_gb >= 100 && var.clickhouse_volume_gb <= 10000
    error_message = "clickhouse_volume_gb must be between 100 and 10000"
  }
}

variable "docker_compose_repo" {
  description = "Git repo URL that holds deploy/langfuse/."
  type        = string
}

variable "docker_compose_ref" {
  description = "Git ref to check out. Use a SHA in production for reproducibility."
  type        = string
  default     = "main"
}
