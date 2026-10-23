terraform {
  cloud {
    organization = "NullMonkeys"
    workspaces {
      project = "oci-k3s-cluster"
    }
  }
}

module "network" {
  source = "../modules/oci-network"

  compartment_id    = var.compartment_id
  name              = "k3s-${var.network_account}"
  vcn_cidr          = local.network_account.vcn_cidr
  subnet_cidr       = local.network_account.subnet_cidr
  peer_subnet_cidrs = [for account in values(local.network_peers) : account.subnet_cidr]
  create_drg        = local.is_network_hub
  drg_id            = local.is_network_hub ? null : data.tfe_outputs.network_hub[0].nonsensitive_values.network.drg_id

  depends_on = [oci_identity_policy.drg_peering]
}

module "compute" {
  source = "../modules/oci-compute"

  compartment_id = var.compartment_id
  name_prefix    = var.network_account
  defaults = merge(var.compute_defaults, {
    subnet_id        = module.network.subnet_id
    assign_public_ip = true
  })
  nodes = var.compute_nodes
}
