variable "compartment_id" {
  description = "Compartment for the VCN and network resources."
  type        = string
}

variable "name" {
  description = "Display name prefix for this account's network."
  type        = string
}

variable "vcn_cidr" {
  description = "IPv4 VCN CIDR, distinct from other accounts and cluster pod/service CIDRs."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.vcn_cidr))
    error_message = "vcn_cidr must be a valid IPv4 CIDR."
  }
}

variable "subnet_cidr" {
  description = "IPv4 CIDR for the regional public node subnet."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.subnet_cidr))
    error_message = "subnet_cidr must be a valid IPv4 CIDR."
  }

  validation {
    condition = try(alltrue([
      for host in [0, -1] :
      cidrhost("${cidrhost(var.subnet_cidr, host)}/${split("/", var.vcn_cidr)[1]}", 0) == cidrhost(var.vcn_cidr, 0)
    ]), false)
    error_message = "The entire subnet_cidr must be contained within vcn_cidr."
  }
}

variable "peer_subnet_cidrs" {
  description = "Other accounts' node subnet CIDRs, routed through the DRG and trusted for cluster traffic."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for cidr in var.peer_subnet_cidrs : can(cidrnetmask(cidr))])
    error_message = "Every peer subnet must be a valid IPv4 CIDR."
  }
}

variable "create_drg" {
  description = "Create the shared DRG only in its owner's workspace."
  type        = bool
  default     = false
}

variable "drg_id" {
  description = "Shared DRG OCID; required when create_drg is false."
  type        = string
  default     = null

  validation {
    condition     = var.create_drg ? true : try(trimspace(var.drg_id) != "", false)
    error_message = "drg_id must be provided when create_drg is false."
  }
}
