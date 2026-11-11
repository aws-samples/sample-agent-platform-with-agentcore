# ------------------------------------------------------------- memory notes
# "Remember that...", "what do you remember?", "forget..." in a Lark group
# (app/integrations/lark/notes.py). The lark-webhook function writes, lists,
# searches and deletes records of the sample's own memory store, on the
# current group's line only (the code supplies the group; the model cannot).
# Deletion is two-step in code: only a record the group was shown as a match
# within the last hour. Off unless asked for, and only with the store.

locals {
  notes_on = var.memory_notes && var.memory_arn != ""
}

data "aws_iam_policy_document" "notes" {
  count = local.notes_on ? 1 : 0

  statement {
    sid = "OwnMemoryNotes"
    actions = [
      "bedrock-agentcore:BatchCreateMemoryRecords",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:RetrieveMemoryRecords",
      "bedrock-agentcore:DeleteMemoryRecord",
    ]
    resources = [var.memory_arn]
  }
}

resource "aws_iam_role_policy" "notes" {
  count  = local.notes_on ? 1 : 0
  name   = "lark-memory-notes"
  role   = aws_iam_role.function.id
  policy = data.aws_iam_policy_document.notes[0].json
}
