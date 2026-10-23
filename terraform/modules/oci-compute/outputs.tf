output "nodes" {
  description = "Node inventory and disk IDs keyed by node name."
  value = {
    for name, instance in oci_core_instance.node : name => {
      role           = local.nodes[name].role
      instance_name  = local.instance_names[name]
      instance_id    = instance.id
      private_ip     = instance.private_ip
      public_ip      = instance.public_ip
      boot_volume_id = instance.boot_volume_id
      block_volume_ids = {
        for key, volume in local.block_volumes : volume.volume_name => oci_core_volume.data[key].id
        if volume.node_name == name
      }
      block_volumes = {
        for key, volume in local.block_volumes : volume.volume_name => {
          device          = oci_core_volume_attachment.data[key].device
          attachment_type = oci_core_volume_attachment.data[key].attachment_type
          is_read_only    = oci_core_volume_attachment.data[key].is_read_only
        }
        if volume.node_name == name
      }
    }
  }
}
