# Coder template "mcp-hub": a workspace with VS Code (code-server), a
# terminal, Claude Code and Codex, both wired to the MCP hub through the JWT
# gateway.
#
# Who the user is, for the hub: their Keycloak sign-in, linked once to Coder
# as the external auth provider `keycloak` (the template requires it, so the
# create page asks for it). Claude Code reads a fresh access token through a
# headers helper on every connection (Claude Code's headersHelper, Codex's
# http_headers_helper); Coder refreshes it with the refresh token it keeps.
# The template holds no credential: model calls use the pod's IRSA role
# (Bedrock: two Claude models and one GPT model), tool calls the user's token.
#
# Pushed by ../deploy.sh with the variables below filled from terraform output.

terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "~> 2.20"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.5"
    }
  }
}

variable "namespace" {
  description = "Namespace of the workspace pods (the caller namespace of the demo)."
  type        = string
}

variable "service_account" {
  description = "Service account of the workspace pods: its IRSA role may invoke the two Claude models and nothing else."
  type        = string
}

variable "coder_internal_url" {
  description = "The Coder server inside the cluster. The workspace agent connects here: the public URL is behind a WAF that admits the operator only."
  type        = string
}

variable "jwt_gateway_url" {
  type = string
}

variable "region" {
  type = string
}

variable "claude_model_main" {
  type = string
}

variable "claude_model_fast" {
  type = string
}

variable "codex_model" {
  description = "Codex's model: a global. inference profile of an OpenAI model, through bedrock-runtime's Responses API."
  type        = string
}

variable "image" {
  description = "Workspace image, pinned by digest (code-server, multi-arch)."
  type        = string
}

provider "coder" {
  url = var.coder_internal_url
}

# In-cluster: the Coder server's own service account, which the chart lets
# manage pods in var.namespace only.
provider "kubernetes" {}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# The user's Keycloak sign-in, linked to Coder. Declaring it makes the
# create page show "Login with Keycloak" before the workspace can start.
data "coder_external_auth" "keycloak" {
  id = "keycloak"
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "arm64" # Graviton node
  dir  = "/home/coder/project"

  startup_script          = file("${path.module}/startup.sh")
  startup_script_behavior = "blocking"

  env = {
    AWS_REGION                    = var.region
    CLAUDE_CODE_USE_BEDROCK       = "1"
    ANTHROPIC_MODEL               = var.claude_model_main
    ANTHROPIC_DEFAULT_HAIKU_MODEL = var.claude_model_fast
    DISABLE_AUTOUPDATER           = "1"
    CODEX_MODEL                   = var.codex_model
    JWT_GATEWAY_URL               = var.jwt_gateway_url
  }

  display_apps {
    vscode          = false
    vscode_insiders = false
    web_terminal    = true
    ssh_helper      = false
  }
}

resource "coder_app" "code_server" {
  agent_id     = coder_agent.main.id
  slug         = "code-server"
  display_name = "VS Code"
  icon         = "/icon/code.svg"
  url          = "http://localhost:13337/?folder=/home/coder/project"
  subdomain    = false
  share        = "owner"
  order        = 1

  healthcheck {
    url       = "http://localhost:13337/healthz"
    interval  = 5
    threshold = 12
  }
}

resource "coder_app" "claude" {
  agent_id     = coder_agent.main.id
  slug         = "claude"
  display_name = "Claude Code"
  icon         = "/icon/claude.svg"
  command      = "cd /home/coder/project && exec claude"
  share        = "owner"
  order        = 2
}

resource "coder_app" "codex" {
  agent_id     = coder_agent.main.id
  slug         = "codex"
  display_name = "Codex"
  icon         = "/icon/openai.svg"
  command      = "cd /home/coder/project && exec codex"
  share        = "owner"
  order        = 3
}

resource "kubernetes_pod_v1" "main" {
  count = data.coder_workspace.me.start_count

  metadata {
    name      = "coder-ws-${lower(data.coder_workspace_owner.me.name)}-${lower(data.coder_workspace.me.name)}"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/name"    = "coder-workspace"
      "app.kubernetes.io/part-of" = "coder"
      "com.coder.resource"        = "true"
      "com.coder.workspace.id"    = data.coder_workspace.me.id
      "com.coder.workspace.name"  = data.coder_workspace.me.name
      "com.coder.user.id"         = data.coder_workspace_owner.me.id
      "com.coder.user.username"   = data.coder_workspace_owner.me.name
    }
  }

  spec {
    service_account_name = var.service_account

    # The workspace pool: a node group in a subnet of its own, the range the
    # JWT gateway's VPC endpoint admits (../terraform/endpoints.tf). A
    # workspace anywhere else could not reach the gateway.
    node_selector = {
      "mcp-hub-demo/pool" = "workspace"
    }
    toleration {
      key      = "mcp-hub-demo/pool"
      operator = "Equal"
      value    = "workspace"
      effect   = "NoSchedule"
    }

    security_context {
      run_as_non_root = true
      run_as_user     = 1000
      run_as_group    = 1000
      fs_group        = 1000
      seccomp_profile {
        type = "RuntimeDefault"
      }
    }

    container {
      name    = "dev"
      image   = var.image
      command = ["sh", "-c", coder_agent.main.init_script]

      env {
        name  = "CODER_AGENT_TOKEN"
        value = coder_agent.main.token
      }
      env {
        name  = "HOME"
        value = "/home/coder"
      }

      security_context {
        allow_privilege_escalation = false
        capabilities {
          drop = ["ALL"]
        }
      }

      resources {
        requests = {
          cpu    = "250m"
          memory = "768Mi"
        }
        limits = {
          memory = "2560Mi"
        }
      }

      volume_mount {
        name       = "home"
        mount_path = "/home/coder"
      }
    }

    # No persistent volume: the demo cluster has no EBS CSI driver. Stopping
    # the workspace clears the home directory; the startup script sets
    # everything up again, and the Keycloak link lives in Coder, not here.
    volume {
      name = "home"
      empty_dir {}
    }
  }
}
