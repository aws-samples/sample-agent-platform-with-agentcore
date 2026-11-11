# ------------------------------ sample memories -----------------------------
# A sample agent that remembers (sample_runtimes = { <id> = { memory = true } })
# gets its own AgentCore Memory store, and its execution role reaches that
# store and no other.
#
# Not the platform's default store: the session user is root in the microVM,
# so whatever the role may read, the session may read. Memory isolates
# callers by actor id only, and the actor id is chosen by the caller of the
# data-plane API, so a grant on a shared store is a grant on every actor in
# it. One store per sample makes "this sample's memory" an IAM boundary, the
# way its own role makes "this sample's tools" one at the gateway.
#
# The strategies and namespace match the stores the backend creates
# (backend/app/services/memory_service.py: semantic "facts" and user
# preferences under /users/{actorId}), because the kernel retrieves from that
# namespace (runtimes/agent-sdk-kernel/src/main.py, MEMORY_NAMESPACE).

locals {
  memory_samples = { for k, v in var.sample_runtimes : k => v if v.memory }
}

resource "aws_bedrockagentcore_memory" "sample" {
  for_each = local.memory_samples

  # Memory names allow letters, digits and underscores, at most 48 characters
  name                  = "sample_${replace(each.key, "-", "_")}${replace(var.name_suffix, "-", "_")}"
  description           = "Long-term memory of the '${each.key}' sample agent; only that sample's runtime role may use it"
  event_expiry_duration = 90

  lifecycle {
    precondition {
      condition     = length("sample_${replace(each.key, "-", "_")}${replace(var.name_suffix, "-", "_")}") <= 48
      error_message = "the memory name for sample '${each.key}' exceeds AgentCore's 48 characters; shorten the sample id or the name suffix."
    }
  }
}

resource "aws_bedrockagentcore_memory_strategy" "sample_facts" {
  for_each = local.memory_samples

  memory_id           = aws_bedrockagentcore_memory.sample[each.key].id
  name                = "facts"
  description         = "Semantic facts extracted from conversations"
  type                = "SEMANTIC"
  namespace_templates = ["/users/{actorId}"]
}

resource "aws_bedrockagentcore_memory_strategy" "sample_preferences" {
  for_each = local.memory_samples

  memory_id           = aws_bedrockagentcore_memory.sample[each.key].id
  name                = "preferences"
  description         = "Stable user preferences"
  type                = "USER_PREFERENCE"
  namespace_templates = ["/users/{actorId}"]

  # one strategy change at a time on a store
  depends_on = [aws_bedrockagentcore_memory_strategy.sample_facts]
}

# The data-plane actions the headless kernel uses (the same set as the shared
# SDK role's MemoryData, sdk_extras in iam.tf), on this sample's store only.
# Inline rather than an aws_iam_policy_document: the document would reference
# the store resource, which the offline policy renderer (tests/render_policies.py)
# drops when it renders this module's documents.
resource "aws_iam_role_policy" "sample_memory" {
  for_each = local.memory_samples

  name = "memory"
  role = aws_iam_role.sample[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "OwnMemoryData"
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:GetEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:ListActors",
        "bedrock-agentcore:ListSessions",
        "bedrock-agentcore:GetMemoryRecord",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:RetrieveMemoryRecords",
      ]
      Resource = [aws_bedrockagentcore_memory.sample[each.key].arn]
    }]
  })
}
