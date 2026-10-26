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

# One replica each is enough to test against, except the service entry: the
# sandbox acceptance run drives 200 concurrent sandboxes through it (N02), and
# one pod answered API Gateway late enough for 504s at that load.
backend_desired_count  = 1
entry_desired_count    = 3
llm_edge_desired_count = 1

# Sample Agents: same tool list as production (the default of sample_tool_ids).
# Whether stock-quote is deployed depends on the foundation publishing a
# staging credential provider for it, not on anything here.

# Sandbox acceptance on staging: the customer's own image (the trial package's
# sandbox_image with the claim patch, built for arm64 and pushed to staging's
# sandbox-kernel repository as cust-v1 / cust-v2, different IMAGE_VERSION) as
# two versions, so F01 can pin each; and the simulated customer host's role as
# the one caller of the sandbox service API. Production keeps the reference
# image and no callers.
# v2 also has a Fast Runtime (AgentCore platform V2, snapshot restore) next to
# its Standard one, so the same host measures both side by side; F01 still
# tells the versions apart by IMAGE_VERSION. cust-v2b is cust-v2 with the
# customer's control server drawing its per-sandbox marker and start time on
# the first request instead of at start-up (sandbox-delivery
# control_server-v2.patch): on a Fast Runtime a value computed at start-up is
# the snapshot's, the same in every sandbox, and fails the customer's own F11.
# cust-v1 still computes it at start-up, so v1 has no Fast Runtime.
sandbox_images = {
  v1 = { tag = "cust-v1" }
  v2 = { tag = "cust-v2b", fast = true }
}
sandbox_service_caller_role_names = ["agent-platform-sandbox-host-sim"]
