config {
  call_module_type = "none"
}

plugin "terraform" {
  enabled = true
  preset  = "all"
}

plugin "azurerm" {
  enabled = true
  version = "0.32.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}

# The prod window ends with terraform destroy (step 10): prevent_destroy would block the planned
# teardown. The state account keeps blob versions instead.
rule "azurerm_resources_missing_prevent_destroy" {
  enabled = false
}
