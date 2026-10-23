variable "compartment_id" {
  description = "Compartment for instances and block volumes."
  type        = string
}

variable "name_prefix" {
  description = "Unique account prefix; node names and Linux hostnames are <name_prefix>-<node map key>."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.name_prefix) != ""
    error_message = "name_prefix must be a non-empty, unique account prefix."
  }
}

variable "defaults" {
  description = "Shared node settings. Non-null node overrides take precedence; shape is replaced as a whole."
  type = object({
    image_id            = optional(string)
    availability_domain = optional(string)
    subnet_id           = optional(string)
    nsg_ids             = optional(set(string), [])
    shape = optional(object({
      name          = string
      ocpus         = optional(number)
      memory_in_gbs = optional(number)
    }))
    ssh_public_key          = optional(string)
    boot_volume_size_in_gbs = optional(number, 50)
    boot_volume_vpus_per_gb = optional(number, 10)
    assign_public_ip        = optional(bool, false)
  })
  default  = {}
  nullable = false
}

variable "nodes" {
  description = "Nodes keyed by stable name, with a control-plane or worker role and optional overrides."
  type = map(object({
    role                = string
    image_id            = optional(string)
    availability_domain = optional(string)
    subnet_id           = optional(string)
    nsg_ids             = optional(set(string))
    shape = optional(object({
      name          = string
      ocpus         = optional(number)
      memory_in_gbs = optional(number)
    }))
    ssh_public_key          = optional(string)
    boot_volume_size_in_gbs = optional(number)
    boot_volume_vpus_per_gb = optional(number)
    assign_public_ip        = optional(bool)
    block_volumes = optional(map(object({
      size_in_gbs     = number
      vpus_per_gb     = optional(number, 10)
      attachment_type = optional(string, "paravirtualized")
      is_read_only    = optional(bool, false)
      # Single paravirtualized volumes default to /dev/oracleoci/oraclevdb.
      # For multiple volumes, set a distinct device for every volume.
      device = optional(string)
    })), {})
  }))

  validation {
    condition     = alltrue([for node in var.nodes : contains(["control-plane", "worker"], node.role)])
    error_message = "Each node's role must be \"control-plane\" or \"worker\"."
  }

  validation {
    condition = alltrue([
      for name in keys(var.nodes) : can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", "${var.name_prefix}-${name}"))
    ])
    error_message = "Each <name_prefix>-<node map key> must be 1-63 lowercase letters, digits, or hyphens, starting and ending with a letter or digit."
  }

  validation {
    condition = alltrue(flatten([
      for node in var.nodes : [
        for volume in node.block_volumes : try(
          volume.size_in_gbs >= 50 && volume.size_in_gbs <= 32768 && floor(volume.size_in_gbs) == volume.size_in_gbs,
          false
        )
      ]
    ]))
    error_message = "Block volume sizes must be whole numbers between 50 and 32768 GB."
  }

  validation {
    condition = alltrue(flatten([
      for node in var.nodes : [
        for volume in node.block_volumes : contains(["iscsi", "paravirtualized", "nvme", "service_determined"], volume.attachment_type)
      ]
    ]))
    error_message = "Block volume attachment_type must be iscsi, paravirtualized, nvme, or service_determined."
  }

  default  = {}
  nullable = false
}
