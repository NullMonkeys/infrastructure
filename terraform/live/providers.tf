provider "oci" {
  auth         = "APIKey"
  tenancy_ocid = var.tenancy_ocid
  region       = var.region
}

provider "oci" {
  alias        = "home"
  auth         = "APIKey"
  tenancy_ocid = var.tenancy_ocid
  region       = var.home_region
}

provider "tfe" {
  organization = "NullMonkeys"
}
