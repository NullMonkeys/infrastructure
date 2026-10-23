mock_provider "oci" {}

mock_provider "oci" {
  alias = "home"
}

mock_provider "tfe" {}

variables {
  tenancy_ocid        = "ocid1.tenancy.oc1..account1"
  compartment_id      = "ocid1.compartment.oc1..example"
  region              = "eu-madrid-1"
  home_region         = "eu-madrid-1"
  network_account     = "account-1"
  network_hub_account = "account-1"
  network_accounts = {
    account-1 = {
      tenancy_ocid         = "ocid1.tenancy.oc1..account1"
      terraform_group_ocid = "ocid1.group.oc1..account1"
      workspace_name       = "account-1"
      vcn_cidr             = "10.1.0.0/16"
      subnet_cidr          = "10.1.1.0/24"
    }
    account-2 = {
      tenancy_ocid         = "ocid1.tenancy.oc1..account2"
      terraform_group_ocid = "ocid1.group.oc1..account2"
      workspace_name       = "account-2"
      vcn_cidr             = "10.2.0.0/16"
      subnet_cidr          = "10.2.1.0/24"
    }
  }
}

run "valid_account_registry" {
  command = plan
}

run "inventory_output_contract" {
  command = plan

  variables {
    deployment_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    compute_nodes = {
      worker-1 = { role = "worker", block_volumes = { data = { size_in_gbs = 50 } } }
    }
    compute_defaults = {
      image_id            = "ocid1.image.oc1..example"
      availability_domain = "example:EU-MADRID-1-AD-1"
      shape               = { name = "VM.Standard.A1.Flex", ocpus = 1, memory_in_gbs = 6 }
    }
  }

  assert {
    condition = (
      output.inventory.account == "account-1" &&
      output.inventory.commit == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" &&
      output.inventory.hosts["account-1-worker-1"].role == "worker" &&
      toset(keys(output.inventory.hosts["account-1-worker-1"])) == toset(["role", "public_ip", "private_ip", "block_volumes"]) &&
      output.inventory.hosts["account-1-worker-1"].block_volumes.data.device == "/dev/oracleoci/oraclevdb"
    )
    error_message = "Inventory must carry deployment provenance and compact host details keyed by unique hostname."
  }

  assert {
    condition     = contains(keys(output.resources), "account-1-worker-1")
    error_message = "OCI identifiers must be kept separately from the inventory output."
  }
}

run "overlapping_vcns" {
  command = plan

  variables {
    network_accounts = {
      account-1 = {
        tenancy_ocid         = "ocid1.tenancy.oc1..account1"
        terraform_group_ocid = "ocid1.group.oc1..account1"
        workspace_name       = "account-1"
        vcn_cidr             = "10.0.0.0/8"
        subnet_cidr          = "10.1.1.0/24"
      }
      account-2 = {
        tenancy_ocid         = "ocid1.tenancy.oc1..account2"
        terraform_group_ocid = "ocid1.group.oc1..account2"
        workspace_name       = "account-2"
        vcn_cidr             = "10.2.0.0/16"
        subnet_cidr          = "10.2.1.0/24"
      }
    }
  }

  expect_failures = [var.network_accounts]
}

run "duplicate_workspaces" {
  command = plan

  variables {
    network_accounts = {
      account-1 = {
        tenancy_ocid         = "ocid1.tenancy.oc1..account1"
        terraform_group_ocid = "ocid1.group.oc1..account1"
        workspace_name       = "account-1"
        vcn_cidr             = "10.1.0.0/16"
        subnet_cidr          = "10.1.1.0/24"
      }
      account-2 = {
        tenancy_ocid         = "ocid1.tenancy.oc1..account2"
        terraform_group_ocid = "ocid1.group.oc1..account2"
        workspace_name       = "account-1"
        vcn_cidr             = "10.2.0.0/16"
        subnet_cidr          = "10.2.1.0/24"
      }
    }
  }

  expect_failures = [var.network_accounts]
}

run "unknown_account_key" {
  command = plan

  variables {
    network_account = "missing"
  }

  expect_failures = [var.network_account]
}
