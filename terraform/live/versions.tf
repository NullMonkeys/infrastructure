terraform {
  required_version = "~> 1.17.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 9.11.0"
    }
    tfe = {
      source  = "hashicorp/tfe"
      version = "~> 0.83.0"
    }
  }
}
