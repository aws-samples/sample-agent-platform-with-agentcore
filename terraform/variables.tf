# Mirrors the CDK context knobs in infrastructure/cdk.json one-to-one.

variable "aws_region" {
  description = "Deployment region"
  type        = string
  default     = "ap-northeast-1"
}

# ------------------------------- network -----------------------------------

variable "existing_vpc_id" {
  description = "Reuse an existing VPC (must already have private subnets routing through a NAT). Empty = create a fresh VPC."
  type        = string
  default     = ""
}

variable "existing_private_subnet_ids" {
  description = "Private (NAT-egress) subnet IDs when reusing a VPC. Terraform cannot classify subnets by route table the way `Vpc.fromLookup` does, so list them explicitly."
  type        = list(string)
  default     = []
}

variable "existing_public_subnet_ids" {
  description = "Public subnet IDs when reusing a VPC (ALBs live here)."
  type        = list(string)
  default     = []
}

variable "existing_nat_eip" {
  description = "Informational: the reused VPC's NAT EIP, surfaced as an output for LLM-gateway allow-listing."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR for the created VPC (fresh-VPC mode only)."
  type        = string
  default     = "10.0.0.0/16"
}

# ----------------------------- model backend -------------------------------
# use_bedrock / anthropic_* stay here for the team-demo runtime (SDK image);
# the kernels in terraform/workloads carry their own copies.

variable "llm_gateway_url" {
  description = <<-EOT
    Retired. A gateway URL is no longer given to kernel containers: gateway
    access needs a per-session grant, so the address lives in the model control
    plane (Governance -> Model backends -> litellm -> base_url) and is read by
    the llm-edge service. Kept declared, and rejected when set, so a deployment
    that still sets it gets told where the setting went instead of silently
    getting Bedrock.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.llm_gateway_url == ""
    error_message = "llm_gateway_url is retired. Set enable_llm_edge = true and configure base_url under Governance -> Model backends -> litellm; see docs/deployment.md section 2."
  }
}

variable "use_bedrock" {
  description = "\"1\" routes kernels to Bedrock (CLAUDE_CODE_USE_BEDROCK)."
  type        = string
  default     = "1"
}

variable "anthropic_model" {
  type    = string
  default = ""
}

variable "anthropic_small_fast_model" {
  type    = string
  default = ""
}

variable "anthropic_default_opus_model" {
  type    = string
  default = ""
}

variable "anthropic_default_sonnet_model" {
  type    = string
  default = ""
}

variable "anthropic_default_haiku_model" {
  type    = string
  default = ""
}

# ------------------------------ image tags ---------------------------------

# The kernel, backend and llm-edge image tags are the workloads root's
# (terraform/workloads/variables.tf). What stays here tags the images the
# foundation still runs: Keycloak, the team APIs, the team-demo runtime.
variable "image_tag" {
  description = "Default tag for the foundation's own images (team_auth, team_demo)."
  type        = string
  default     = "latest"
}

variable "agent_observability" {
  description = "Opt in to AgentCore Observability for the headless kernel: grants the telemetry IAM statements and creates the X-Ray delivery destination. Set the same value in terraform/workloads (which creates the delivery sources and runs the observability image variant)."
  type        = bool
  default     = false
}

variable "team_auth_image_tag" {
  type    = string
  default = ""
}

# Keycloak and the team APIs are built from separate Dockerfiles but shared one
# tag until 2026-08-19, so rebuilding only Keycloak left the three team-API
# services pulling a tag that was never pushed — they then failed to start in a
# loop. Override this to move Keycloak on its own.
variable "keycloak_image_tag" {
  type    = string
  default = ""
}

variable "team_demo_image_tag" {
  type    = string
  default = ""
}

variable "team_demo_build" {
  description = "Bump to force a new team-demo runtime version when the image tag is mutable."
  type        = string
  default     = "1"
}

# ------------------------------- portal auth -------------------------------

variable "oidc_issuer" {
  description = "Enterprise-SSO mode: Keycloak issuer URL. Empty = Cognito-only."
  type        = string
  default     = ""
}

variable "oidc_realm" {
  description = "Use this realm of the oidc_issuer Keycloak instead of the issuer's own (e.g. a staging realm with its own test users). Empty = issuer as given."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]*$", var.oidc_realm))
    error_message = "oidc_realm must be a bare realm name."
  }
}

variable "oidc_client_id" {
  type    = string
  default = "portal-web"
}

variable "oidc_audience" {
  type    = string
  default = "agent-platform"
}

variable "service_api_throttle" {
  description = "Stage-wide throttle of the private service-entry API, shared by every caller and route (channels, the sandbox service API). Every sandbox job poll is a request: 200 jobs polled each second plus creates is about 300 requests per second. The account-level API Gateway limit (10000 rps) is above any value here."
  type = object({
    rate_limit  = number
    burst_limit = number
  })
  default = { rate_limit = 50, burst_limit = 100 }
  validation {
    condition     = var.service_api_throttle.rate_limit >= 1 && var.service_api_throttle.burst_limit >= var.service_api_throttle.rate_limit && var.service_api_throttle.rate_limit <= 5000
    error_message = "service_api_throttle: 1 <= rate_limit <= 5000, and burst_limit at least rate_limit."
  }
}

variable "service_api_allowed_vpces" {
  description = "Caller interface-VPC-endpoint IDs pinned in (and associated with) the private service-entry API."
  type        = list(string)
  default     = []
  # Empty means no aws:SourceVpce condition at all. A second environment in a
  # shared account (staging) must not run its private API that open; one that
  # joins another state's cluster gets that owner's in-VPC endpoint added
  # (main.tf, data.aws_vpc_endpoint.owner_service_entry), so the list is never
  # empty there either.
  validation {
    condition     = var.environment == "production" || length(var.service_api_allowed_vpces) > 0 || var.existing_eks_cluster_name != ""
    error_message = "service_api_allowed_vpces must not be empty outside production (empty = no aws:SourceVpce restriction), unless the environment joins an existing cluster (its in-VPC endpoint is added)."
  }
}

# ----------------------------- deploy staging ------------------------------
# CDK deployed stacks one at a time (repos -> push images -> runtimes -> ...).
# These flags reproduce that order across the two roots: the foundation with
# enable_runtime = false creates repos, roles and edges only; push images; then
# the full foundation and terraform/workloads. The flags are published in the
# facts parameter, so the workloads root follows them without restating them.

variable "enable_runtime" {
  description = "Create the AgentCore runtimes (requires kernel images pushed)."
  type        = bool
  default     = true
}

variable "enable_portal" {
  description = "Create the portal (requires backend image pushed + enable_runtime)."
  type        = bool
  default     = true
}

variable "enable_llm_edge" {
  description = "Create the llm-edge service. Required to use the 'litellm' model backend: it holds the gateway key so session containers never receive one. A Bedrock-direct deployment can leave this off."
  type        = bool
  default     = false
}

variable "llm_edge_certificate_arn" {
  description = "ACM certificate for the internal llm-edge listener. Empty serves plain HTTP inside the VPC; set this to encrypt the leg carrying prompt content."
  type        = string
  default     = ""
}

variable "enable_agentcore_gateway_backend" {
  description = "Create the AgentCore Gateway + LiteLLM inference target and the per-session caller role for the 'agentcore_gateway' model backend (no key on the platform side; see docs/deployment.md Option A2). The LiteLLM key lives in an AgentCore Identity API-key credential provider created out of band."
  type        = bool
  default     = false
}

variable "agentcore_gateway_litellm_endpoint" {
  description = "LiteLLM base URL for the inference target (https://...). With agentcore_gateway_private_endpoint set, the name on the internal ALB's publicly trusted certificate."
  type        = string
  default     = ""
}

variable "agentcore_gateway_credential_provider_arn" {
  description = "AgentCore Identity API-key credential provider ARN holding the LiteLLM virtual key. Prefer agentcore_gateway_credential_provider_name: a full ARN carries the account id, and the tracked overlays (envs/) are published with the public snapshot."
  type        = string
  default     = ""
}

variable "agentcore_gateway_credential_provider_name" {
  description = "Name of the AgentCore Identity API-key credential provider holding the LiteLLM virtual key; the ARN is built from this account and region. Takes precedence over agentcore_gateway_credential_provider_arn."
  type        = string
  default     = ""
}

variable "agentcore_gateway_models" {
  description = "LiteLLM model names the inference target accepts (keep in line with the virtual key)."
  type        = list(string)
  default     = []
}

variable "agentcore_gateway_private_endpoint" {
  description = "Reach LiteLLM through a managed VPC Lattice resource gateway: vpc_id/subnet_ids/security_group_ids for the ENIs in LiteLLM's VPC, routing_domain = internal ALB DNS. Null = over the internet."
  type = object({
    vpc_id             = string
    subnet_ids         = list(string)
    security_group_ids = list(string)
    routing_domain     = string
  })
  default = null
}

variable "enable_edge_waf" {
  description = "Put a WAF web ACL (modules/edge_waf: AWS managed rules + per-IP rate limits, CLOUDFRONT scope) in front of the portal and team-auth distributions. Recommended for anything reachable by real users; a short-lived test deployment can turn it off to save the web ACL charges (about USD 20/month per environment plus per-request fees)."
  type        = bool
  default     = true
}

variable "enable_team_auth" {
  description = "Optional enterprise-SSO demo: Keycloak + team APIs (requires team-auth images pushed)."
  type        = bool
  default     = false
}

variable "enable_team_demo" {
  description = "Optional JWT-inbound demo runtime (requires enable_team_auth and a live Keycloak — AgentCore validates the discovery URL at create time)."
  type        = bool
  default     = false
}

variable "enable_gateway_vpce" {
  description = "Interface VPC endpoint (private DNS) for AgentCore Gateway in the platform VPC, so runtime -> gateway traffic stays off the NAT path."
  type        = bool
  default     = false
}

variable "enable_mcp_hub_demo" {
  description = "Optional self-hosted MCP hub demo (requires enable_team_auth): a hub EC2 replacing AgentCore Gateway as the tool backend (MCPHUB-HMAC-SHA256 inbound), plus an EC2 playing the calling application. Package the hub source first — scripts/package_mcp_hub.sh."
  type        = bool
  default     = false
}

variable "mcp_hub_source_s3_key" {
  description = "Workspace-bucket key of the packaged MCP hub source zip (scripts/package_mcp_hub.sh uploads it)."
  type        = string
  default     = "mcp-hub/source.zip"
}

variable "async_artifact_prefixes" {
  description = "S3 key prefixes the headless (SDK) kernel may write async task outputs to. Add your own pipelines' output prefixes."
  type        = list(string)
  default     = ["feeds"]
}

# --------------------------------- EKS -------------------------------------
# The cluster the containers (backend, entry, llm-edge, Keycloak, team APIs)
# run on. Pods authenticate to AWS with IRSA; the EKS Pod Identity agent is
# not installed.

variable "eks_kubernetes_version" {
  description = "Kubernetes minor version for the cluster and its add-ons."
  type        = string
  default     = "1.36"
}

variable "eks_node_instance_type" {
  description = "Graviton instance type for the managed node group. Must support ENI trunking (security groups for Pods): m/c/r families from 6g on; no `t` family. Every platform image is arm64."
  type        = string
  default     = "m7g.large"

  validation {
    condition     = can(regex("^[a-z]+[0-9]+g[a-z]*\\.", var.eks_node_instance_type))
    error_message = "eks_node_instance_type must be a Graviton (arm64) type such as m7g.large — the platform images are linux/arm64 only."
  }
}

variable "eks_node_count" {
  description = "Desired (and minimum) node count. One per private subnet spreads each Deployment's replicas across AZs; three m7g.large carry the full platform with headroom."
  type        = number
  default     = 3

  validation {
    condition     = var.eks_node_count >= 2
    error_message = "eks_node_count must be at least 2 so a two-replica Deployment can spread across AZs."
  }
}

variable "eks_node_max_count" {
  description = "Upper bound the node group may scale to."
  type        = number
  default     = 4
}

variable "eks_public_access_cidrs" {
  description = "CIDRs allowed to reach the cluster's public Kubernetes API endpoint (Terraform, kubectl and helm run from outside the VPC). The API is IAM-authenticated regardless; narrow this to the operators' egress addresses in production."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "eks_admin_principal_arns" {
  description = "IAM principals granted cluster-admin through EKS access entries, besides the one running the apply (which is bootstrapped automatically)."
  type        = list(string)
  default     = []
}

variable "eks_lb_controller_chart_version" {
  description = "aws-load-balancer-controller Helm chart version (its TargetGroupBinding registers pods into the Terraform-owned target groups)."
  type        = string
  default     = "3.5.0"
}

variable "eks_fluent_bit_chart_version" {
  description = "aws-for-fluent-bit Helm chart version (container logs to CloudWatch)."
  type        = string
  default     = "0.2.0"
}

# ------------------------------ test isolation -----------------------------

variable "environment" {
  description = "Which environment this state is. production lives in the default workspace; any other value must run in a Terraform workspace of the same name (checked on every plan), so one environment's tfvars cannot be applied to another's state."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must be a lowercase name, e.g. production or staging."
  }
}

variable "existing_eks_cluster_name" {
  description = "Join an EKS cluster that another state owns instead of creating one. Workloads go into namespaces suffixed with name_suffix; the cluster, node group and controllers stay with the owner. Empty = create the cluster."
  type        = string
  default     = ""
}

variable "existing_eks_log_group_prefix" {
  description = "Log group prefix the owning cluster's Fluent Bit writes under (only with existing_eks_cluster_name). Empty = /eks/<cluster name>."
  type        = string
  default     = ""
}

variable "name_suffix" {
  description = "Appended to every fixed resource name (IAM roles, table, repos, runtimes, ...) so a test copy can coexist with the CDK deployment in the same account. Lowercase alphanumerics and hyphens; keep it short. Empty for production parity."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-z0-9-]*$", var.name_suffix))
    error_message = "name_suffix must be lowercase alphanumerics/hyphens (it is embedded in bucket and repo names)."
  }
}

variable "platform_version_python" {
  description = "Python interpreter Terraform uses for scripts/set_inference_target.py (the agentcore_gateway backend) and scripts/discover_sample_tool_providers.py (sample_tools). The runtimes' set_platform_version.py runs from terraform/workloads with its own setting."
  type        = string
  default     = "python3"
}

# ----------------------------- Sample Agents --------------------------------
# The sample-tools gateway (modules/sample_tools) and the sample runtimes'
# execution roles (modules/runtime). The application's workloads root adds
# the tools, Cedar policies and runtimes themselves.

variable "enable_sample_tools" {
  description = "Create the Sample Agents tool gateway, its policy engine and the sample runtime roles (requires enable_runtime)."
  type        = bool
  default     = false
}

# ------------------------------- sandboxes ----------------------------------
# One-shot code sandboxes (modules/sandbox + the workloads root's sandbox
# runtime). Off by default.

variable "enable_sandbox" {
  description = "Create the one-shot code sandbox network, role and image repository (requires enable_runtime)."
  type        = bool
  default     = false
}

variable "sandbox_network" {
  description = "Where sandboxes run and what they may reach. Required when enable_sandbox: see modules/sandbox/variables.tf for each field."
  type = object({
    subnets = list(object({
      cidr = string
      az   = string
    }))
    # empty: the security groups of the VPC's ECR api/dkr and Logs interface
    # endpoints, looked up by the root (main.tf), so no tfvars names one
    endpoint_security_group_ids = optional(list(string), [])
    callback_targets = optional(list(object({
      description       = string
      port              = number
      security_group_id = optional(string, "")
      cidr              = optional(string, "")
    })), [])
    state_object_arns = optional(list(string), [])
    # key prefixes in THIS environment's workspaces bucket ("sandbox-sim/"):
    # the root turns each into that one bucket's exact object ARN, so an
    # overlay names state locations without an account id and without a
    # bucket-name wildcard (a "*" there spans the account and region segments
    # and admits every account's bucket: Security Agent on MR !45)
    state_prefixes              = optional(list(string), [])
    extra_image_repository_arns = optional(list(string), [])
  })
  default = null
  validation {
    condition = var.sandbox_network == null || alltrue([
      for p in try(var.sandbox_network.state_prefixes, []) : can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*/$", p))
    ])
    error_message = "a state prefix is one path segment ending in / (\"sandbox-sim/\"): no wildcard, no leading /, no nesting."
  }
  # The bucket must be exact: a wildcard there ("*" spans the account and
  # region segments, "?" matches one character each) admits other accounts'
  # buckets. An allowlist of the characters a bucket name may have, so no
  # wildcard character is left out (Security Agent on MR !45, twice).
  validation {
    condition = var.sandbox_network == null || alltrue([
      for a in try(var.sandbox_network.state_object_arns, []) : can(regex("^arn:aws:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/", a))
    ])
    error_message = "state_object_arns: arn:aws:s3:::<exact bucket name>/<key pattern> (no wildcard in the bucket name: * or ? there matches other accounts' buckets); use state_prefixes for this environment's workspaces bucket."
  }
  validation {
    condition     = !var.enable_sandbox || var.sandbox_network != null
    error_message = "enable_sandbox needs sandbox_network (subnets and the VPC endpoint security groups)."
  }
}

variable "sample_runtimes" {
  description = "Sample agents that get their own runtime and execution role, by sample id. Keys must match the workloads root's sample list."
  type        = map(object({}))
  default     = {}
}

variable "sample_tools_credential_provider_arns" {
  description = "Explicit API-key credential providers for sample tools, by tool package, on top of the ones found by name (<env>sample-tool-<tool>; register a key with scripts/set_sample_tool_key.sh and the next apply picks it up). Normally empty. A tool without a provider is deployed without its target."
  type        = map(string)
  default     = {}
}

variable "sample_tools_policy_mode" {
  description = "Policy engine mode on the sample-tools gateway: LOG_ONLY (trace, do not enforce) or ENFORCE."
  type        = string
  default     = "LOG_ONLY"
}

# Custom domains for the two CloudFront distributions. null = the default
# *.cloudfront.net domain. A distribution's default domain changes every time
# it is recreated (a new account, a rebuild), and the Keycloak one is baked
# into the OIDC issuer every token is checked against; a custom domain keeps
# both stable. certificate_arn: ACM in us-east-1. The DNS CNAME to the
# distribution is created outside Terraform (see the custom_domain_targets output).
variable "portal_custom_domain" {
  type = object({
    name            = string
    certificate_arn = string
  })
  default = null
}

variable "team_auth_custom_domain" {
  type = object({
    name            = string
    certificate_arn = string
  })
  default = null
}

# Seed the Keycloak database from an RDS snapshot (see modules/team_auth
# db_snapshot_identifier); create-time only. Used to move the IdP between
# accounts with its realms and client secrets intact.
variable "keycloak_db_snapshot_identifier" {
  type    = string
  default = null
}

variable "keycloak_db_kms_key_arn" {
  description = "KMS key for the restored Keycloak database; required when the snapshot is shared from another account."
  type        = string
  default     = null
}
