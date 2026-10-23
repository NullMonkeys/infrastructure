mock_provider "oci" {}

variables {
  compartment_id = "ocid1.compartment.oc1..example"
  name_prefix    = "account-1"
  defaults = {
    image_id            = "ocid1.image.oc1..example"
    availability_domain = "example:EU-MADRID-1-AD-1"
    subnet_id           = "ocid1.subnet.oc1..example"
    shape               = { name = "VM.Standard.A1.Flex", ocpus = 1, memory_in_gbs = 6 }
  }
  nodes = {
    worker-1 = { role = "worker" }
  }
}

run "account_scoped_hostname" {
  command = plan

  assert {
    condition     = oci_core_instance.node["worker-1"].display_name == "account-1-worker-1"
    error_message = "The account prefix and stable map key must determine the instance name."
  }

  assert {
    condition = (
      startswith(base64decode(oci_core_instance.node["worker-1"].metadata.user_data), "#cloud-config\n") &&
      yamldecode(base64decode(oci_core_instance.node["worker-1"].metadata.user_data)).hostname == "account-1-worker-1"
    )
    error_message = "Cloud-init must have its header and set the same Linux hostname as the instance name."
  }
}

run "another_account_and_earlier_key" {
  command = plan

  variables {
    name_prefix = "account-2"
    nodes = {
      worker-0 = { role = "worker" }
      worker-1 = { role = "worker" }
    }
  }

  assert {
    condition = (
      oci_core_instance.node["worker-1"].display_name == "account-2-worker-1" &&
      yamldecode(base64decode(oci_core_instance.node["worker-1"].metadata.user_data)).hostname == "account-2-worker-1"
    )
    error_message = "Accounts must have different hostnames, and inserting an earlier key must not renumber existing names."
  }
}

run "data_volume_inventory" {
  command = plan

  variables {
    nodes = {
      worker-1 = {
        role = "worker"
        block_volumes = {
          data = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdb" }
        }
      }
    }
  }

  assert {
    condition = (
      output.nodes["worker-1"].block_volumes.data.device == "/dev/oracleoci/oraclevdb" &&
      output.nodes["worker-1"].block_volumes.data.attachment_type == "paravirtualized" &&
      output.nodes["worker-1"].block_volumes.data.is_read_only == false
    )
    error_message = "Ansible needs the actual attachment's stable device path, mode, and access type."
  }
}

run "single_volume_gets_consistent_device_by_default" {
  command = plan

  variables {
    nodes = {
      worker-1 = { role = "worker", block_volumes = { data = { size_in_gbs = 50 } } }
    }
  }

  assert {
    condition = (
      oci_core_volume_attachment.data["[\"worker-1\",\"data\"]"].device == "/dev/oracleoci/oraclevdb" &&
      output.nodes["worker-1"].block_volumes.data.device == "/dev/oracleoci/oraclevdb"
    )
    error_message = "Omitting device on a single data volume must configure a stable path on the actual attachment and publish it to inventory."
  }
}

run "explicit_single_volume_device_is_preserved" {
  command = plan

  variables {
    nodes = {
      worker-1 = {
        role          = "worker"
        block_volumes = { data = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdc" } }
      }
    }
  }

  assert {
    condition     = output.nodes["worker-1"].block_volumes.data.device == "/dev/oracleoci/oraclevdc"
    error_message = "An explicitly configured device must take precedence over the single-volume default."
  }
}

run "multiple_volumes_require_explicit_devices" {
  command = plan

  variables {
    nodes = {
      worker-1 = {
        role = "worker"
        block_volumes = {
          data  = { size_in_gbs = 50 }
          extra = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdc" }
        }
      }
    }
  }

  expect_failures = [oci_core_volume_attachment.data]
}

run "multiple_volume_devices_remain_stable" {
  command = plan

  variables {
    nodes = {
      worker-1 = {
        role = "worker"
        block_volumes = {
          data    = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdb" }
          earlier = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdc" }
        }
      }
    }
  }

  assert {
    condition = (
      output.nodes["worker-1"].block_volumes.data.device == "/dev/oracleoci/oraclevdb" &&
      output.nodes["worker-1"].block_volumes.earlier.device == "/dev/oracleoci/oraclevdc"
    )
    error_message = "Volumes must retain their explicitly selected device paths regardless of map order."
  }
}

run "duplicate_volume_devices_are_rejected" {
  command = plan

  variables {
    nodes = {
      worker-1 = {
        role = "worker"
        block_volumes = {
          data  = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdb" }
          extra = { size_in_gbs = 50, device = "/dev/oracleoci/oraclevdb" }
        }
      }
    }
  }

  expect_failures = [oci_core_volume_attachment.data]
}

run "missing_required_settings" {
  command = plan

  variables {
    defaults = {}
  }

  expect_failures = [oci_core_instance.node["worker-1"]]
}

run "node_overrides_supply_required_settings" {
  command = plan

  variables {
    defaults = {}
    nodes = {
      worker-1 = {
        role                = "worker"
        image_id            = "ocid1.image.oc1..example"
        availability_domain = "example:EU-MADRID-1-AD-1"
        subnet_id           = "ocid1.subnet.oc1..example"
        shape               = { name = "VM.Standard.E2.1.Micro" }
      }
    }
  }
}

run "invalid_hostname" {
  command = plan

  variables {
    name_prefix = "Account_1"
  }

  expect_failures = [var.nodes]
}

run "invalid_shape_size" {
  command = plan

  variables {
    nodes = {
      worker-1 = { role = "worker", shape = { name = "VM.Standard.A1.Flex", ocpus = 0 } }
    }
  }

  expect_failures = [oci_core_instance.node["worker-1"]]
}

run "invalid_boot_volume" {
  command = plan

  variables {
    nodes = {
      worker-1 = { role = "worker", boot_volume_size_in_gbs = 49 }
    }
  }

  expect_failures = [oci_core_instance.node["worker-1"]]
}

run "invalid_block_volume" {
  command = plan

  variables {
    nodes = {
      worker-1 = { role = "worker", block_volumes = { data = { size_in_gbs = 1 } } }
    }
  }

  expect_failures = [var.nodes]
}

run "invalid_volume_attachment" {
  command = plan

  variables {
    nodes = {
      worker-1 = { role = "worker", block_volumes = { data = { size_in_gbs = 50, attachment_type = "invalid" } } }
    }
  }

  expect_failures = [var.nodes]
}
