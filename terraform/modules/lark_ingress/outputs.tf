output "facts" {
  description = "What the workloads root needs to create the function, and the URL to give Lark."
  value = {
    function_name = local.function_name
    role_arn      = aws_iam_role.function.arn
    log_group     = aws_cloudwatch_log_group.function.name
    secret_arn    = aws_secretsmanager_secret.lark.arn
    open_base     = var.open_base
    events_url    = "${aws_apigatewayv2_stage.default.invoke_url}lark/events"
    # the ambient-memory function; "" when the jobs are off
    memory_function_name = local.ambient_on ? local.memory_function_name : ""
    memory_role_arn      = local.ambient_on ? aws_iam_role.memory[0].arn : ""
    memory_log_group     = local.ambient_on ? aws_cloudwatch_log_group.memory[0].name : ""
    ambient_model        = var.ambient_model
    # the reminders table; "" when reminders are off
    reminders_table  = local.reminders_on ? aws_dynamodb_table.reminders[0].name : ""
    utc_offset_hours = var.utc_offset_hours
    # the function may remember and forget on the sample's store
    memory_notes = local.notes_on
    # the Lark tools' route; "" when they are off
    mcp_url = var.tools ? "${aws_apigatewayv2_stage.default.invoke_url}lark/mcp" : ""
    # each turn's agent may use the code sandbox and send its files
    sandbox = var.tools && var.sandbox
  }
}
