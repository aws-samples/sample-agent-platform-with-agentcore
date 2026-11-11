# The two IRSA roles in cluster A. Both trust exactly one service account
# through cluster A's OIDC provider; no other principal can assume either.
#
#   frontend  may invoke the hub API's /mcp methods, and nothing else - and
#             is the one principal the API's resource policy admits.
#   intruder  the negative case: a pod in the same namespace, on the same
#             node, with the same network path to the endpoint, but a role
#             that holds no grant. Its calls must die at API Gateway.
#
# The hub pod in cluster B needs no AWS permissions at all and gets no role.

data "aws_iam_policy_document" "irsa_trust" {
  for_each = {
    frontend = var.frontend_service_account
    intruder = var.intruder_service_account
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.frontend.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.frontend.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.frontend.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${var.frontend_namespace}:${each.value}"]
    }
  }
}

resource "aws_iam_role" "frontend" {
  name                 = "${var.name}-frontend"
  assume_role_policy   = data.aws_iam_policy_document.irsa_trust["frontend"].json
  max_session_duration = 3600
  description          = "IRSA role of the frontend pod in cluster A: may only invoke the MCP hub entry API"
}

data "aws_iam_policy_document" "frontend" {
  statement {
    sid     = "InvokeHubEntry"
    actions = ["execute-api:Invoke"]
    resources = [
      for m in sort(tolist(local.methods)) : "${aws_api_gateway_rest_api.hub.execution_arn}/${local.stage}/${m}/mcp"
    ]
  }
}

resource "aws_iam_role_policy" "frontend" {
  name   = "invoke-hub-entry"
  role   = aws_iam_role.frontend.id
  policy = data.aws_iam_policy_document.frontend.json
}

resource "aws_iam_role" "intruder" {
  name                 = "${var.name}-intruder"
  assume_role_policy   = data.aws_iam_policy_document.irsa_trust["intruder"].json
  max_session_duration = 3600
  description          = "Negative-test IRSA role in cluster A: same network path as the frontend, no grant on the hub entry API"
}
