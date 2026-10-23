locals {
  drg_id = var.create_drg ? oci_core_drg.hub[0].id : var.drg_id
}

resource "oci_core_vcn" "cluster" {
  compartment_id = var.compartment_id
  display_name   = var.name
  cidr_blocks    = [var.vcn_cidr]
  is_ipv6enabled = false
}

resource "oci_core_drg" "hub" {
  count = var.create_drg ? 1 : 0

  compartment_id = var.compartment_id
  display_name   = "${var.name}-drg"
}

resource "oci_core_drg_attachment" "vcn" {
  drg_id       = local.drg_id
  display_name = var.name

  network_details {
    id   = oci_core_vcn.cluster.id
    type = "VCN"
  }
}

resource "oci_core_internet_gateway" "nodes" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.cluster.id
  display_name   = "${var.name}-internet"
  enabled        = true
}

resource "oci_core_route_table" "nodes" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.cluster.id
  display_name   = "${var.name}-nodes"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.nodes.id
  }

  dynamic "route_rules" {
    for_each = var.peer_subnet_cidrs

    content {
      destination       = route_rules.value
      destination_type  = "CIDR_BLOCK"
      network_entity_id = oci_core_drg_attachment.vcn.drg_id
    }
  }
}

resource "oci_core_security_list" "nodes" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.cluster.id
  display_name   = "${var.name}-nodes"

  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "all"
    stateless        = false
  }

  dynamic "ingress_security_rules" {
    for_each = setunion(var.peer_subnet_cidrs, toset([var.subnet_cidr]))

    content {
      description = "Private cluster traffic"
      source      = ingress_security_rules.value
      source_type = "CIDR_BLOCK"
      protocol    = "all"
      stateless   = false
    }
  }

  dynamic "ingress_security_rules" {
    for_each = [
      { min = 22, max = 22 },
      { min = 80, max = 80 },
      { min = 443, max = 443 },
    ]

    content {
      source      = "0.0.0.0/0"
      source_type = "CIDR_BLOCK"
      protocol    = "6"
      stateless   = false

      tcp_options {
        min = ingress_security_rules.value.min
        max = ingress_security_rules.value.max
      }
    }
  }
}

resource "oci_core_subnet" "nodes" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.cluster.id
  display_name               = "${var.name}-nodes"
  cidr_block                 = var.subnet_cidr
  route_table_id             = oci_core_route_table.nodes.id
  security_list_ids          = [oci_core_security_list.nodes.id]
  prohibit_public_ip_on_vnic = false
  prohibit_internet_ingress  = false
}
