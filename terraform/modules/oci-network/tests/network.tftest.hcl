mock_provider "oci" {}

variables {
  compartment_id    = "ocid1.compartment.oc1..example"
  name              = "k3s-account-1"
  vcn_cidr          = "10.1.0.0/16"
  subnet_cidr       = "10.1.1.0/24"
  peer_subnet_cidrs = ["10.2.1.0/24"]
  create_drg        = true
}

run "preserve_rules_except_public_nodeports" {
  command = plan

  assert {
    condition = toset([
      for rule in oci_core_security_list.nodes.ingress_security_rules : rule.tcp_options[0].min
      if rule.source == "0.0.0.0/0" && rule.protocol == "6"
    ]) == toset([22, 80, 443])
    error_message = "Public TCP ingress must contain only SSH, HTTP, and HTTPS."
  }

  assert {
    condition = alltrue([
      for rule in oci_core_security_list.nodes.ingress_security_rules :
      rule.source != "0.0.0.0/0" || rule.protocol == "6"
    ])
    error_message = "Public UDP NodePort ingress must be removed."
  }

  assert {
    condition = toset([
      for rule in oci_core_security_list.nodes.ingress_security_rules : rule.source if rule.protocol == "all"
    ]) == toset(["10.1.1.0/24", "10.2.1.0/24"])
    error_message = "Private cluster ingress must remain unchanged."
  }

  assert {
    condition = alltrue([
      for rule in oci_core_security_list.nodes.egress_security_rules :
      rule.destination == "0.0.0.0/0" && rule.protocol == "all" && !rule.stateless
    ]) && length(oci_core_security_list.nodes.egress_security_rules) == 1
    error_message = "The existing stateful all-protocol egress rule must remain unchanged."
  }
}

run "invalid_vcn_cidr" {
  command = plan

  variables {
    vcn_cidr = "invalid"
  }

  expect_failures = [var.vcn_cidr]
}

run "subnet_outside_vcn" {
  command = plan

  variables {
    subnet_cidr = "10.2.1.0/24"
  }

  expect_failures = [var.subnet_cidr]
}

run "subnet_larger_than_vcn" {
  command = plan

  variables {
    subnet_cidr = "10.0.0.0/8"
  }

  expect_failures = [var.subnet_cidr]
}

run "invalid_peer_cidr" {
  command = plan

  variables {
    peer_subnet_cidrs = ["2001:db8::/64"]
  }

  expect_failures = [var.peer_subnet_cidrs]
}

run "missing_peer_drg" {
  command = plan

  variables {
    create_drg = false
  }

  expect_failures = [var.drg_id]
}
