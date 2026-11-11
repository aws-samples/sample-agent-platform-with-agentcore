# TFLint for the workloads root (terraform/workloads) and its modules.
#   tflint --init                                   # once: fetches the aws ruleset
#   tflint --chdir=terraform/workloads --config="$PWD/.tflint.hcl"
#   for m in terraform/workloads/modules/*/; do tflint --chdir="$m" --config="$PWD/.tflint.hcl"; done
# The per-module pass is needed: from the root, tflint skips a module's unused
# locals and data sources.
# CI runs the same passes (ci/tf-lint.sh, catalog tf.lint).

config {
  # lint inside ./modules/* as called from the root, with the root's values
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.49.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
