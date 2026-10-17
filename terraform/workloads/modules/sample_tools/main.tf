# The Sample Agents tool stack, workloads half: what changes with the samples.
#
# The foundation owns the identities and the shell (its modules/sample_tools:
# the sample-tools gateway, its execution role, the policy engine, the tool
# Lambdas' execution role) and publishes them as facts. This module adds, per
# tool package under samples/tools/<id>/:
#
#   - a Lambda target (tool.yaml kind: lambda): the function from src/, a
#     gateway target with the tool schema from tool.yaml
#   - an OpenAPI target (tool.yaml kind: openapi): no code of ours; the spec
#     is the tool definition and the gateway signs each call with the API-key
#     credential provider the facts name for this tool. No provider, no
#     target: the sample is deployed without that tool
#   - the tool's Cedar policy (policy.cedar.tftpl), rendered with the gateway
#     ARN and the sample runtime roles it may permit
#
# Two tools, two shapes on purpose. crypto-price (Lambda) shows a tool whose
# upstream is public and keyless but whose inputs want validating, caching and
# normalising. stock-quote (OpenAPI) shows a tool whose upstream is a clean
# REST API that only needs its key kept out of the agent: the key lives in
# AgentCore Identity, the gateway injects it, no runtime or function ever
# holds it. The agent sees two MCP tools under one gateway and cannot tell
# the difference; the policy engine decides which runtime role may call
# which, with which inputs.

data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region

  tools_dir = "${path.module}/../../../../samples/tools"
  tools     = { for id in var.tool_ids : id => yamldecode(file("${local.tools_dir}/${id}/tool.yaml")) }

  lambda_tools  = { for id, t in local.tools : id => t if t.kind == "lambda" }
  openapi_tools = { for id, t in local.tools : id => t if t.kind == "openapi" && contains(keys(var.credential_provider_arns), id) }
  # OpenAPI tools whose provider is not configured: skipped, and said so in the plan
  openapi_skipped = [for id, t in local.tools : id if t.kind == "openapi" && !contains(keys(var.credential_provider_arns), id)]

  deployed_tools = merge(local.lambda_tools, local.openapi_tools)

  # the Cedar policies name the sample runtime roles by their assumed-role
  # form (arn:aws:sts::<acct>:assumed-role/<role-name>), which is what the
  # gateway sees as AgentCore::IamEntity
  sample_principals = {
    for id, arn in var.sample_role_arns :
    id => "arn:aws:sts::${split(":", arn)[4]}:assumed-role/${element(split("/", arn), length(split("/", arn)) - 1)}"
  }

  # Which sample may call which tool: sample.yaml's requires_tools is the one
  # declaration (the portal reads it for the clone gate), so the Cedar policies
  # read it too. Rendering every tool for every sample principal would hand a
  # second sample the first one's tools the day it is added.
  agents_dir = "${path.module}/../../../../samples/agents"
  sample_manifests = {
    for f in fileset(local.agents_dir, "*/sample.yaml") :
    dirname(f) => yamldecode(file("${local.agents_dir}/${f}"))
  }
  tool_principals = {
    for tid in keys(local.tools) : tid => {
      for sid, principal in local.sample_principals : sid => principal
      if contains([for t in try(local.sample_manifests[sid].requires_tools, []) : t.id], tid)
    }
  }
}

# ------------------------------------------------------------ Lambda tools

data "archive_file" "tool" {
  for_each = local.lambda_tools

  type        = "zip"
  source_dir  = "${local.tools_dir}/${each.key}/src"
  output_path = "${path.module}/.build/${each.key}.zip"
}

resource "aws_lambda_function" "tool" {
  for_each = local.lambda_tools

  function_name = "agent-platform-sample-${each.key}${var.name_suffix}"
  description   = each.value.description
  role          = var.lambda_role_arn
  runtime       = lookup(each.value, "runtime", "python3.13")
  architectures = ["arm64"]
  handler       = lookup(each.value, "handler", "handler.handler")

  filename         = data.archive_file.tool[each.key].output_path
  source_code_hash = data.archive_file.tool[each.key].output_base64sha256

  timeout     = lookup(each.value, "timeout_s", 15)
  memory_size = lookup(each.value, "memory_mb", 256)

  environment {
    variables = merge(
      { TOOL_ID = each.key, NAME_SUFFIX = var.name_suffix },
      lookup(each.value, "env", {}),
    )
  }

  tags = { SampleTool = each.key }
}

resource "aws_bedrockagentcore_gateway_target" "lambda" {
  for_each = local.lambda_tools

  gateway_identifier = var.gateway_id
  name               = each.key
  description        = each.value.description

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool[each.key].arn

        tool_schema {
          dynamic "inline_payload" {
            for_each = each.value.tools
            content {
              name        = inline_payload.value.name
              description = inline_payload.value.description

              input_schema {
                type        = "object"
                description = lookup(inline_payload.value, "input_description", null)

                dynamic "property" {
                  for_each = inline_payload.value.inputs
                  content {
                    name        = property.value.name
                    type        = property.value.type
                    description = lookup(property.value, "description", null)
                    required    = lookup(property.value, "required", false)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

# ------------------------------------------------------------ OpenAPI tools

resource "aws_bedrockagentcore_gateway_target" "openapi" {
  for_each = local.openapi_tools

  gateway_identifier = var.gateway_id
  name               = each.key
  description        = each.value.description

  credential_provider_configuration {
    api_key {
      provider_arn              = var.credential_provider_arns[each.key]
      credential_location       = upper(each.value.credential.location)
      credential_parameter_name = each.value.credential.parameter_name
    }
  }

  target_configuration {
    mcp {
      open_api_schema {
        inline_payload {
          payload = file("${local.tools_dir}/${each.key}/${lookup(each.value, "openapi", "openapi.json")}")
        }
      }
    }
  }
}

# ------------------------------------------------------------ Cedar policies

# One policy per deployed tool. The template receives the gateway ARN, the
# principals of the samples whose sample.yaml requires this tool, and the
# tool's own id; it decides which of its operations those principals may
# call, and with which inputs. A tool whose target is
# skipped gets no policy either: the engine is default-deny, and a policy
# naming an action the gateway does not expose fails validation.
resource "aws_bedrockagentcore_policy" "tool" {
  for_each = local.deployed_tools

  name             = "sample_${replace(each.key, "-", "_")}${replace(var.name_suffix, "-", "_")}"
  policy_engine_id = var.policy_engine_id
  description      = "sample tool ${each.key}: which sample runtime may call it, with which inputs"

  definition {
    cedar {
      statement = templatefile("${local.tools_dir}/${each.key}/policy.cedar.tftpl", {
        gateway_arn = var.gateway_arn
        target      = each.key
        principals  = local.tool_principals[each.key]
      })
    }
  }

  # the action names exist only once the target does
  depends_on = [aws_bedrockagentcore_gateway_target.lambda, aws_bedrockagentcore_gateway_target.openapi]
}
