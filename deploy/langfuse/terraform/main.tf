###############################################################################
# Hetzner Frankfurt provisioning for the Putsch Langfuse self-host.
#
# What this provisions:
#   - 1× CCX33 (or HA pair: 2× CCX33 + Hetzner LB) running Docker
#   - Hetzner Object Storage bucket (S3-compatible) for nightly backups
#   - DNS A record for langfuse.putsch.internal (private DNS zone)
#   - Volume for ClickHouse data (separate from the boot disk; resize is
#     a non-event)
#
# What this DOESN'T provision (deliberately):
#   - The .env secrets — those come from Hetzner Vault via cloud-init
#   - The age recipient keys for backups — held offline
#   - The Tailscale tailnet membership — handled by the SRE on first SSH
###############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.48"
    }
  }
  # State lives in Hetzner Object Storage (same bucket as backups, separate
  # prefix). Use the encrypted backend with versioning enabled.
  backend "s3" {
    bucket                      = "putsch-tfstate"
    key                         = "langfuse/terraform.tfstate"
    region                      = "eu-central-fra"
    endpoint                    = "https://fsn1.your-objectstorage.com"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    force_path_style            = true
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

###############################################################################
# Server(s)
###############################################################################
resource "hcloud_server" "langfuse" {
  count       = var.ha_pair ? 2 : 1
  name        = "langfuse-fra-${count.index + 1}"
  server_type = "ccx33"
  location    = "fsn1"
  image       = "ubuntu-24.04"
  labels = {
    role        = "langfuse"
    environment = var.environment
    managed_by  = "terraform"
  }
  user_data = templatefile("${path.module}/cloud-init.yaml", {
    docker_compose_repo = var.docker_compose_repo
    docker_compose_ref  = var.docker_compose_ref
  })
  public_net {
    ipv4_enabled = false   # private network only
    ipv6_enabled = false
  }
  network {
    network_id = hcloud_network.putsch_vpc.id
  }
}

resource "hcloud_volume" "clickhouse" {
  count    = var.ha_pair ? 2 : 1
  name     = "langfuse-clickhouse-${count.index + 1}"
  size     = var.clickhouse_volume_gb
  location = "fsn1"
  format   = "ext4"
}

resource "hcloud_volume_attachment" "clickhouse" {
  count     = var.ha_pair ? 2 : 1
  volume_id = hcloud_volume.clickhouse[count.index].id
  server_id = hcloud_server.langfuse[count.index].id
  automount = true
}

###############################################################################
# Network
###############################################################################
resource "hcloud_network" "putsch_vpc" {
  name     = "putsch-vpc"
  ip_range = "10.10.0.0/16"
}

resource "hcloud_network_subnet" "fra" {
  network_id   = hcloud_network.putsch_vpc.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "10.10.1.0/24"
}

###############################################################################
# Load balancer (HA only)
###############################################################################
resource "hcloud_load_balancer" "langfuse" {
  count              = var.ha_pair ? 1 : 0
  name               = "langfuse-fra-lb"
  load_balancer_type = "lb11"
  location           = "fsn1"
}

resource "hcloud_load_balancer_target" "lb_targets" {
  count            = var.ha_pair ? 2 : 0
  load_balancer_id = hcloud_load_balancer.langfuse[0].id
  type             = "server"
  server_id        = hcloud_server.langfuse[count.index].id
  use_private_ip   = true
}

###############################################################################
# Object Storage (backups)
###############################################################################
# Hetzner Object Storage is S3-compatible but managed outside the
# hcloud provider — provisioning is via the Hetzner Cloud Console or
# the Hetzner Object Storage API. For Terraform, we use the
# `restapi_object` provider in real deployments; this is a stub.
locals {
  backup_bucket_name = "putsch-langfuse-backups-${var.environment}"
}

output "backup_bucket" {
  value = local.backup_bucket_name
}

###############################################################################
# Server private IPs (consumed by cloud-init for cluster bootstrap)
###############################################################################
output "server_private_ips" {
  value = [for s in hcloud_server.langfuse : s.network[0].ip]
}
