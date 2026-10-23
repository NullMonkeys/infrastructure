locals {
  network_account = var.network_accounts[var.network_account]
  network_hub     = var.network_accounts[var.network_hub_account]
  is_network_hub  = var.network_account == var.network_hub_account
  network_peers = {
    for name, account in var.network_accounts : name => account
    if name != var.network_account
  }
  local_terraform_group_name = local.is_network_hub ? null : one([
    for group in data.oci_identity_groups.local_terraform[0].groups : group.name
    if group.id == local.network_account.terraform_group_ocid
  ])
}

data "oci_identity_groups" "local_terraform" {
  provider = oci.home
  count    = local.is_network_hub ? 0 : 1

  compartment_id = var.tenancy_ocid

  lifecycle {
    postcondition {
      condition = length([
        for group in self.groups : group.id
        if group.id == local.network_account.terraform_group_ocid
      ]) == 1
      error_message = "The configured Terraform group OCID must identify exactly one local IAM group."
    }
  }
}

data "tfe_outputs" "network_hub" {
  count = local.is_network_hub ? 0 : 1

  organization = "NullMonkeys"
  workspace    = local.network_hub.workspace_name
}

resource "oci_identity_policy" "drg_peering" {
  provider = oci.home
  for_each = local.is_network_hub ? local.network_peers : {
    (var.network_hub_account) = local.network_hub
  }

  compartment_id = var.tenancy_ocid
  name           = "k3s-${var.network_account}-drg-${each.key}"
  description    = "Shared cluster DRG access between ${var.network_account} and ${each.key}."
  statements = concat([
    "Define tenancy PeerTenancy as ${each.value.tenancy_ocid}",
    ], local.is_network_hub ? [
    "Define group PeerTerraform as ${each.value.terraform_group_ocid}",
    "Admit group PeerTerraform of tenancy PeerTenancy to manage drgs in tenancy",
    "Admit group PeerTerraform of tenancy PeerTenancy to manage vcns in tenancy",
    ] : [
    "Endorse group ${local.local_terraform_group_name} to manage drgs in tenancy PeerTenancy",
    "Endorse group ${local.local_terraform_group_name} to manage vcns in tenancy PeerTenancy",
  ])
}
