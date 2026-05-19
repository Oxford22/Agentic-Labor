###############################################################################
# deploy/memory/terraform/main.tf
#
# Provisions a dedicated Neo4j host for putsch-memory in Hetzner Frankfurt
# (region fsn1). Memory-heavy sizing: 64 GB RAM, NVMe.
#
# This is the substrate the Putsch agentic stack reads from and writes to.
# Sizing rationale:
#   - Page cache: 32 GB — covers full graph hot-set for the first 18 months
#   - Heap: 16 GB — large enough for analytics queries without GC pauses
#   - Remaining 16 GB: OS, APOC, GDS workspace, backup buffer
###############################################################################

terraform {
  required_version = ">= 1.7"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.47"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.30"
    }
  }
  backend "s3" {
    # Encrypted, EU-only state. Endpoint configured via -backend-config.
    bucket         = "putsch-tf-state-frankfurt"
    key            = "putsch-memory/terraform.tfstate"
    region         = "eu-central-1"
    encrypt        = true
    dynamodb_table = "putsch-tf-locks"
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

###############################################################################
# Inputs
###############################################################################

variable "hcloud_token" {
  description = "Hetzner Cloud API token (read from vault, never inline)."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare token for the internal DNS zone."
  type        = string
  sensitive   = true
}

variable "environment" {
  description = "deployment environment — drives sizing and naming"
  type        = string
  validation {
    condition     = contains(["prod", "stage", "dev"], var.environment)
    error_message = "environment must be one of: prod, stage, dev."
  }
}

variable "ssh_keys" {
  description = "Hetzner SSH key IDs allowed to access the bastion path."
  type        = list(string)
}

variable "betriebsrat_audit_email" {
  description = "Email address forwarded a DPIA-relevant change diff on every apply."
  type        = string
  default     = "betriebsrat-it@putsch.example"
}

###############################################################################
# Sizing table
###############################################################################

locals {
  # Server-type rationale:
  #   ccx33 = 8 vCPU dedicated, 32 GB RAM — fine for dev/stage
  #   ccx53 = 16 vCPU dedicated, 64 GB RAM, NVMe — prod target
  sizing = {
    prod  = { server_type = "ccx53", volume_gb = 1000, snapshot_retention_days = 14 }
    stage = { server_type = "ccx43", volume_gb = 500,  snapshot_retention_days = 7  }
    dev   = { server_type = "ccx33", volume_gb = 200,  snapshot_retention_days = 3  }
  }
  active = local.sizing[var.environment]

  # All hosts pinned to fsn1 (Frankfurt). Crossing the border requires a new ADR.
  location = "fsn1"

  labels = {
    project       = "putsch-memory"
    environment   = var.environment
    data_residency = "DE"
    cost_center   = "platform-eng"
    owner         = "platform-eng@putsch.example"
    compliance    = "gdpr,eu-ai-act,betrvg"
  }
}

###############################################################################
# Network — private, no public Bolt
###############################################################################

resource "hcloud_network" "memory" {
  name     = "putsch-memory-${var.environment}"
  ip_range = "10.42.0.0/16"
  labels   = local.labels
}

resource "hcloud_network_subnet" "memory" {
  network_id   = hcloud_network.memory.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "10.42.10.0/24"
}

resource "hcloud_firewall" "memory" {
  name   = "putsch-memory-${var.environment}"
  labels = local.labels

  # SSH — only from bastion subnet
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["10.42.99.0/24"]
  }

  # Bolt — only inside the cluster
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "7687"
    source_ips = ["10.42.0.0/16"]
  }

  # Backup port — only inside the cluster
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "6362"
    source_ips = ["10.42.0.0/16"]
  }

  # Prometheus scrape
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "2004"
    source_ips = ["10.42.50.0/24"]
  }

  # No public ingress for the graph. By design.
}

###############################################################################
# Dedicated NVMe volume for /var/lib/neo4j/data
###############################################################################

resource "hcloud_volume" "neo4j_data" {
  name      = "putsch-memory-neo4j-${var.environment}"
  location  = local.location
  size      = local.active.volume_gb
  format    = "ext4"
  automount = false
  labels    = local.labels
}

###############################################################################
# The Neo4j node
###############################################################################

resource "hcloud_server" "neo4j" {
  name        = "putsch-memory-neo4j-${var.environment}"
  server_type = local.active.server_type
  image       = "ubuntu-24.04"
  location    = local.location
  ssh_keys    = var.ssh_keys
  firewall_ids = [hcloud_firewall.memory.id]
  labels      = local.labels

  # cloud-init bootstraps Docker, mounts the NVMe volume, installs the
  # systemd-managed docker compose unit, and seeds the Neo4j password from
  # the secret manager mounted at /etc/putsch/secrets.env.
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    environment       = var.environment
    backup_bucket     = "putsch-memory-backups-frankfurt"
    neo4j_volume_dev  = "/dev/disk/by-id/scsi-0HC_Volume_${hcloud_volume.neo4j_data.id}"
    compose_path      = "/opt/putsch-memory/docker-compose.yml"
    audit_email       = var.betriebsrat_audit_email
  })

  network {
    network_id = hcloud_network.memory.id
    ip         = "10.42.10.20"
  }

  depends_on = [hcloud_network_subnet.memory]
}

resource "hcloud_volume_attachment" "neo4j_data" {
  volume_id = hcloud_volume.neo4j_data.id
  server_id = hcloud_server.neo4j.id
  automount = false
}

###############################################################################
# Daily snapshots — independent from the per-15-minute online backups
###############################################################################

resource "hcloud_snapshot" "neo4j_daily_seed" {
  server_id   = hcloud_server.neo4j.id
  description = "Initial snapshot post-provision (${timestamp()})"
  labels      = local.labels
}

###############################################################################
# Internal DNS — only reachable inside the VPC
###############################################################################

resource "cloudflare_record" "neo4j_internal" {
  zone_id = data.cloudflare_zone.internal.id
  name    = "neo4j.${var.environment}.memory"
  type    = "A"
  value   = hcloud_server.neo4j.network[*].ip[0]
  proxied = false
  ttl     = 60
  comment = "putsch-memory Neo4j — internal-only, see ADR-005"
}

data "cloudflare_zone" "internal" {
  name = "internal.putsch.example"
}

###############################################################################
# Outputs
###############################################################################

output "neo4j_private_ip" {
  description = "Private IP of the Neo4j node — Bolt is on :7687."
  value       = hcloud_server.neo4j.network[*].ip[0]
}

output "neo4j_bolt_uri" {
  description = "Bolt URI for use by graphiti_client."
  value       = "bolt://neo4j.${var.environment}.memory.internal.putsch.example:7687"
}

output "snapshot_retention_days" {
  description = "Daily snapshot retention window."
  value       = local.active.snapshot_retention_days
}
