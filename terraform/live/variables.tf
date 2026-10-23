variable "deployment_commit" {
  description = "Git commit supplied as a run-specific variable by the deployment workflow; null for manual runs."
  type        = string
  default     = null
}

variable "tenancy_ocid" {
  description = "OCI tenancy managed by this workspace."
  type        = string
}

variable "region" {
  description = "OCI region managed by this workspace."
  type        = string
}

variable "home_region" {
  description = "This account's OCI home region, used for IAM policies."
  type        = string
}

variable "compartment_id" {
  description = "Target compartment OCID, or tenancy OCID for the root compartment."
  type        = string
}

variable "network_accounts" {
  description = "Shared account registry; use identical values in all four workspaces."
  type = map(object({
    tenancy_ocid         = string
    terraform_group_ocid = string
    workspace_name       = string
    vcn_cidr             = string
    subnet_cidr          = string
  }))
  nullable = false

  validation {
    condition = length(var.network_accounts) > 0 && alltrue([
      for name, account in var.network_accounts : try(alltrue([
        for value in [name, account.tenancy_ocid, account.terraform_group_ocid, account.workspace_name] : trimspace(value) != ""
      ]), false)
    ])
    error_message = "network_accounts must contain accounts with non-empty keys, tenancy_ocid, terraform_group_ocid, and workspace_name."
  }

  validation {
    condition = (
      length(distinct([for account in var.network_accounts : account.tenancy_ocid])) == length(var.network_accounts) &&
      length(distinct([for account in var.network_accounts : account.workspace_name])) == length(var.network_accounts)
    )
    error_message = "Each account must have a distinct tenancy_ocid and HCP workspace_name."
  }

  validation {
    condition = alltrue([
      for account in var.network_accounts : can(cidrnetmask(account.vcn_cidr)) && can(cidrnetmask(account.subnet_cidr))
    ])
    error_message = "Every account's vcn_cidr and subnet_cidr must be valid IPv4 CIDRs."
  }

  validation {
    condition = alltrue([
      for account in var.network_accounts : try(alltrue([
        for host in [0, -1] :
        cidrhost("${cidrhost(account.subnet_cidr, host)}/${split("/", account.vcn_cidr)[1]}", 0) == cidrhost(account.vcn_cidr, 0)
      ]), false)
    ])
    error_message = "Every account's subnet_cidr must be fully contained within its vcn_cidr."
  }

  validation {
    condition = alltrue(flatten([
      for name, account in var.network_accounts : [
        for other_name, other in var.network_accounts : try(
          cidrhost("${cidrhost(account.vcn_cidr, 0)}/${split("/", other.vcn_cidr)[1]}", 0) != cidrhost(other.vcn_cidr, 0),
          false
        ) if name != other_name
      ]
    ]))
    error_message = "Account VCN CIDRs must not overlap."
  }
}

variable "network_account" {
  description = "This workspace's key in network_accounts; its tenancy must match tenancy_ocid."
  type        = string

  validation {
    condition     = contains(keys(var.network_accounts), var.network_account)
    error_message = "network_account must be a key in network_accounts."
  }

  validation {
    condition     = try(var.network_accounts[var.network_account].tenancy_ocid == var.tenancy_ocid, false)
    error_message = "The selected account's tenancy_ocid must match the workspace tenancy_ocid."
  }
}

variable "network_hub_account" {
  description = "Key in network_accounts for the account that owns the shared DRG."
  type        = string

  validation {
    condition     = contains(keys(var.network_accounts), var.network_hub_account)
    error_message = "network_hub_account must be a key in network_accounts."
  }
}

variable "compute_defaults" {
  description = "Shared node settings. Non-null node overrides take precedence; shape is replaced as a whole."
  type = object({
    image_id            = optional(string)
    availability_domain = optional(string)
    nsg_ids             = optional(set(string), [])
    shape = optional(object({
      name          = string
      ocpus         = optional(number)
      memory_in_gbs = optional(number)
    }))
    ssh_public_key          = optional(string)
    boot_volume_size_in_gbs = optional(number, 50)
    boot_volume_vpus_per_gb = optional(number, 10)
  })
  default  = {}
  nullable = false
}

variable "compute_nodes" {
  description = "Nodes keyed by stable name, with a control-plane or worker role and optional overrides."
  type = map(object({
    role                = string
    image_id            = optional(string)
    availability_domain = optional(string)
    nsg_ids             = optional(set(string))
    shape = optional(object({
      name          = string
      ocpus         = optional(number)
      memory_in_gbs = optional(number)
    }))
    ssh_public_key          = optional(string)
    boot_volume_size_in_gbs = optional(number)
    boot_volume_vpus_per_gb = optional(number)
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
    condition     = alltrue([for node in var.compute_nodes : contains(["control-plane", "worker"], node.role)])
    error_message = "Each node's role must be \"control-plane\" or \"worker\"."
  }

  default  = {}
  nullable = false
}
