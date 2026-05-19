output "private_network_id" {
  value       = hcloud_network.putsch_vpc.id
  description = "Network id of the Putsch VPC. Attach app servers here."
}

output "langfuse_endpoint" {
  value = var.ha_pair ? (
    "http://${hcloud_load_balancer.langfuse[0].ipv4}:3000"
  ) : (
    "http://${hcloud_server.langfuse[0].network[0].ip}:3000"
  )
  description = <<-EOT
    The internal Langfuse endpoint. Wire this into the Putsch private DNS
    zone as langfuse.putsch.internal.
  EOT
}
