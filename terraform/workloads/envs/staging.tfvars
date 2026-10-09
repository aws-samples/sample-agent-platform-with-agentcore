# Staging overlay for the workloads root. Applied ON TOP of terraform.tfvars:
#
#   TF_WORKSPACE=staging terraform plan -var-file=envs/staging.tfvars -out=staging.plan
#   terraform apply staging.plan
#
# Everything about *where* staging runs (the shared cluster, suffixed
# namespaces, the staging realm) is the foundation's decision and arrives in
# its facts parameter (/agent-platform-staging/foundation). Only what the
# application team decides differently for staging goes here.

environment = "staging"
name_suffix = "-staging"

# One replica each is enough to test against.
backend_desired_count  = 1
entry_desired_count    = 1
llm_edge_desired_count = 1
