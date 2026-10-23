output "inventory" {
  description = "Hosts for Ansible, keyed by their unique Linux hostname."
  value = {
    account = var.network_account
    commit  = var.deployment_commit
    hosts = {
      for node in values(module.compute.nodes) : node.instance_name => {
        role          = node.role
        private_ip    = node.private_ip
        public_ip     = node.public_ip
        block_volumes = node.block_volumes
      }
    }
  }
}

output "resources" {
  description = "OCI instance and volume identifiers, separate from connection details."
  value = {
    for node in values(module.compute.nodes) : node.instance_name => {
      instance_id      = node.instance_id
      boot_volume_id   = node.boot_volume_id
      block_volume_ids = node.block_volume_ids
    }
  }
}

output "network" {
  description = "This account's VCN, subnet, shared DRG, and attachment IDs."
  value       = module.network
}
