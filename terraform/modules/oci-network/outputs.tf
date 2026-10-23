output "vcn_id" {
  description = "This account's VCN OCID."
  value       = oci_core_vcn.cluster.id
}

output "subnet_id" {
  description = "Regional public node subnet OCID."
  value       = oci_core_subnet.nodes.id
}

output "drg_id" {
  description = "Shared DRG OCID, created locally or supplied by its owner."
  value       = local.drg_id
}

output "attachment_id" {
  description = "This account's VCN-to-DRG attachment OCID."
  value       = oci_core_drg_attachment.vcn.id
}
