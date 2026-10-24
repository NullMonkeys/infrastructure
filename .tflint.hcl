tflint {
  required_version = "= 0.65.0"
}

config {
  # Recursive linting checks each child module once in its own directory.
  call_module_type = "none"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
