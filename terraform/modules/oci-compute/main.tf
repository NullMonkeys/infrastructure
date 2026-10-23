locals {
  instance_names = {
    for name, node in var.nodes : name => "${var.name_prefix}-${name}"
  }

  nodes = {
    for name, node in var.nodes : name => merge(var.defaults, {
      for setting, value in node : setting => value if value != null
    })
  }

  block_volumes = {
    for volume in flatten([
      for node_name, node in local.nodes : [
        for volume_name, volume in node.block_volumes : merge(volume, {
          node_name           = node_name
          instance_name       = local.instance_names[node_name]
          volume_name         = volume_name
          availability_domain = node.availability_domain
          # A single data volume has an unambiguous default. Multiple volumes
          # require explicit paths so adding a map key cannot renumber disks.
          device = volume.device != null ? volume.device : (
            volume.attachment_type == "paravirtualized" && length(node.block_volumes) == 1
            ? "/dev/oracleoci/oraclevdb" : null
          )
        })
      ]
    ]) : jsonencode([volume.node_name, volume.volume_name]) => volume
  }
}

resource "oci_core_instance" "node" {
  for_each = local.nodes

  compartment_id       = var.compartment_id
  availability_domain  = each.value.availability_domain
  display_name         = local.instance_names[each.key]
  shape                = each.value.shape.name
  preserve_boot_volume = false

  freeform_tags = {
    role = each.value.role
  }

  dynamic "shape_config" {
    for_each = each.value.shape.ocpus != null || each.value.shape.memory_in_gbs != null ? [each.value.shape] : []

    content {
      ocpus         = shape_config.value.ocpus
      memory_in_gbs = shape_config.value.memory_in_gbs
    }
  }

  source_details {
    source_type             = "image"
    source_id               = each.value.image_id
    boot_volume_size_in_gbs = each.value.boot_volume_size_in_gbs
    boot_volume_vpus_per_gb = each.value.boot_volume_vpus_per_gb
  }

  create_vnic_details {
    subnet_id        = each.value.subnet_id
    nsg_ids          = each.value.nsg_ids
    assign_public_ip = each.value.assign_public_ip
    assign_ipv6ip    = false
  }

  metadata = merge(each.value.ssh_public_key == null ? {} : {
    ssh_authorized_keys = each.value.ssh_public_key
    }, {
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml", {
      hostname = local.instance_names[each.key]
    }))
  })

  lifecycle {
    precondition {
      condition = alltrue([
        for value in [each.value.image_id, each.value.availability_domain, each.value.subnet_id, try(each.value.shape.name, null)] :
        try(trimspace(value) != "", false)
      ])
      error_message = "Each node must have a non-empty image_id, availability_domain, subnet_id, and shape.name after merging defaults and overrides."
    }

    precondition {
      condition = alltrue([
        for value in [try(each.value.shape.ocpus, null), try(each.value.shape.memory_in_gbs, null)] :
        value == null ? true : value > 0
      ])
      error_message = "Shape OCPUs and memory must be positive when specified."
    }

    precondition {
      condition = (
        each.value.boot_volume_size_in_gbs >= 50 &&
        each.value.boot_volume_size_in_gbs <= 32768 &&
        floor(each.value.boot_volume_size_in_gbs) == each.value.boot_volume_size_in_gbs
      )
      error_message = "Boot volume size must be a whole number between 50 and 32768 GB."
    }
  }
}

resource "oci_core_volume" "data" {
  for_each = local.block_volumes

  compartment_id      = var.compartment_id
  availability_domain = each.value.availability_domain
  display_name        = "${each.value.instance_name}-${each.value.volume_name}"
  size_in_gbs         = each.value.size_in_gbs
  vpus_per_gb         = each.value.vpus_per_gb
}

resource "oci_core_volume_attachment" "data" {
  for_each = local.block_volumes

  instance_id     = oci_core_instance.node[each.value.node_name].id
  volume_id       = oci_core_volume.data[each.key].id
  attachment_type = each.value.attachment_type
  is_read_only    = each.value.is_read_only
  device          = each.value.device

  lifecycle {
    precondition {
      condition     = each.value.attachment_type != "paravirtualized" || each.value.device != null
      error_message = "Set an explicit device path for every paravirtualized volume on nodes with multiple block volumes. A single volume defaults to /dev/oracleoci/oraclevdb."
    }

    precondition {
      condition = each.value.device == null ? true : length([
        for volume in values(local.block_volumes) : volume
        if volume.node_name == each.value.node_name && volume.device == each.value.device
      ]) == 1
      error_message = "Each block volume on a node must use a distinct device path."
    }
  }
}
