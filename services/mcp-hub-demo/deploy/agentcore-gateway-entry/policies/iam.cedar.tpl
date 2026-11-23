// Application-facing gateway: exactly one role may call the hub's tool.
//
// The principal is the caller's assumed-role ARN with the session name
// removed, so it is stable across sessions and `==` matches it. Another role
// that IAM lets invoke this gateway (the intruder pod holds the same
// bedrock-agentcore:InvokeGateway grant) is permitted nothing here: the
// engine is default-deny.
permit(
  principal == AgentCore::IamEntity::"${PRINCIPAL}",
  action == AgentCore::Action::"${ACTION}",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
);
