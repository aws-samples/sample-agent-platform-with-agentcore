#!/usr/bin/env python3
"""Adversarial checks on the edge WAF (terraform/modules/edge_waf) and its
attachment to the internet-facing distributions. Static; no credentials.

The web ACL is the only content defense in front of the portal API and
Keycloak. Each property below is something a later edit could quietly
loosen while every plan still reads green:

  1. both distributions (portal, team_auth) set web_acl_id from the module,
     and the root wires edge_waf into both; the ACL is CLOUDFRONT scope in
     us-east-1 with a default allow (blocks come from rules, not the default).
     enable_edge_waf can turn it off for a test deployment, but defaults to
     true: a deployment that never sets it is protected
  2. the RCE / SSRF signatures are never downgraded: no rule_action_override
     names Log4JRCE, JavaDeserializationRCE or EC2MetaDataSSRF, and
     KnownBadInputs has no override at all
  3. the only counted-then-re-blocked content rules are GenericLFI_BODY and
     CrossSiteScripting_BODY; their exemption is /api/v1/ only (never /mcp,
     never a bare prefix like ^/), and the channel webhook (outside bodies,
     token only) re-blocks them
  4. RestrictedExtensions is exempt on the artifacts read route only
  5. every path match normalizes first (URL_DECODE, NORMALIZE_PATH,
     LOWERCASE): no NONE transform anywhere in the module, and no regex
     lookaround (AWS WAF regex has none; it would silently mis-match)
  6. the rate-limit exemption is the platform egress on the key reads only
     (an ip set AND the discovery/JWKS path), surfaced as a label
  7. logs redact authorization, cookie, x-channel-token and the query string
  8. terraform validate passes in terraform/

Run: python3 terraform/tests/test_edge_waf_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
WAF = os.path.join(FOUNDATION, "modules", "edge_waf", "main.tf")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def balanced(text: str, start: int) -> str:
    """The brace-balanced block whose opening brace is the first one at/after `start`."""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise AssertionError("unterminated block")


def block(text: str, header: str) -> str:
    m = re.search(rf"^{header}\s*\{{", text, re.M)
    assert m, f"no block matching {header!r}"
    return balanced(text, m.start())


def rules(acl: str) -> dict:
    """name -> body of each top-level `rule { ... }` in the web ACL."""
    out = {}
    for m in re.finditer(r"^  rule \{", acl, re.M):
        body = balanced(acl, m.start())
        name = re.search(r'^    name\s*=\s*"([^"]+)"', body, re.M)
        assert name, "rule without a name"
        out[name.group(1)] = body
    return out


def local_list(text: str, name: str) -> list:
    m = re.search(rf"^  {name}\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert m, f"no local {name}"
    return re.findall(r'"([^"]+)"', m.group(1))


def main() -> int:
    waf = read(WAF)
    root = read(FOUNDATION, "main.tf")
    acl = block(waf, r'resource "aws_wafv2_web_acl" "edge"')
    R = rules(acl)

    # 1. attachment and scope
    for mod, res in (("portal", "portal"), ("team_auth", "team_auth")):
        dist = block(read(FOUNDATION, "modules", mod, "main.tf"), rf'resource "aws_cloudfront_distribution" "{res}"')
        assert re.search(r"^\s*web_acl_id\s*=\s*var\.web_acl_arn\s*$", dist, re.M), f"{mod}: distribution must set web_acl_id = var.web_acl_arn"
        call = block(root, rf'module "{mod}"')
        assert re.search(r"web_acl_arn\s*=\s*one\(module\.edge_waf\[\*\]\.web_acl_arn\)", call), f"root: module.{mod} must get web_acl_arn from module.edge_waf"
    var = block(read(FOUNDATION, "variables.tf"), r'variable "enable_edge_waf"')
    assert re.search(r"^\s*default\s*=\s*true\s*$", var, re.M), "enable_edge_waf must default to true"
    edge = block(root, r'module "edge_waf"')
    cnt = re.search(r"^\s*count\s*=\s*(.+)$", edge, re.M)
    assert cnt and "var.enable_edge_waf" in cnt.group(1), "module.edge_waf count must be gated on var.enable_edge_waf"
    for name in ("variables.tf", "terraform.tfvars.example"):
        assert not re.search(r"^\s*enable_edge_waf\s*=\s*false", read(FOUNDATION, name), re.M) or name == "variables.tf", \
            f"{name}: the shipped example must not switch the WAF off (a commented hint is fine)"
    assert re.search(r'scope\s*=\s*"CLOUDFRONT"', acl) and re.search(r'region\s*=\s*"us-east-1"', acl), "the ACL must be CLOUDFRONT scope in us-east-1"
    assert re.search(r"default_action\s*\{\s*allow\s*\{\}\s*\}", acl), "default_action is allow; blocking belongs to the rules"
    ok("portal and team-auth distributions carry the edge ACL (CLOUDFRONT, us-east-1); enable_edge_waf defaults to true")

    # 2. RCE / SSRF never downgraded
    overridden = set(local_list(waf, "crs_count")) | set(local_list(waf, "content_label"))
    ext = re.search(r'^  extension_label\s*=\s*"([^"]+)"', waf, re.M)
    assert ext, "no local extension_label"
    overridden.add(ext.group(1))
    for sig in ("Log4JRCE", "JavaDeserializationRCE", "EC2MetaDataSSRF"):
        assert not any(sig in o for o in overridden), f"{sig} must keep its managed Block action"
        assert sig not in acl, f"{sig} must not be named anywhere in the ACL (an override or a label exemption)"
    assert "rule_action_override" not in R["AWSManagedRulesKnownBadInputsRuleSet"], "KnownBadInputs takes no overrides"
    ok("Log4J / Java deserialization / metadata SSRF keep blocking everywhere")

    # 3. content labels: exemption scoped to /api/v1/, webhook re-blocked
    assert sorted(local_list(waf, "content_label")) == ["CrossSiteScripting_BODY", "GenericLFI_BODY"], \
        "only LFI and XSS body rules may be counted-then-re-blocked"
    content = R["ContentLabelBlock"]
    assert re.search(r"block\s*\{\}", content) and "not_statement" in content, "ContentLabelBlock blocks unless the path is exempt"
    exempt = re.findall(r'regex_string\s*=\s*"([^"]+)"', content)
    assert exempt == ["^/api/v1/"], f"the content exemption must be exactly ^/api/v1/, got {exempt}"
    webhook = R["ContentLabelBlockWebhook"]
    assert re.search(r"block\s*\{\}", webhook) and "not_statement" not in webhook, "the webhook rule blocks on match"
    assert re.findall(r'regex_string\s*=\s*"([^"]+)"', webhook) == ["^/api/v1/channels/[^/]+/webhook"], "webhook rule must name the channel webhook route"
    prio = {n: int(re.search(r"^    priority\s*=\s*(\d+)", b, re.M).group(1)) for n, b in R.items()}
    managed = max(prio["AWSManagedRulesCommonRuleSet"], prio["AWSManagedRulesKnownBadInputsRuleSet"])
    for n in ("ContentLabelBlock", "ContentLabelBlockWebhook", "ExtensionLabelBlock"):
        assert prio[n] > managed, f"{n} must run after the managed groups that emit its labels"
    assert len(set(prio.values())) == len(prio), "rule priorities must be unique"
    ok("LFI / XSS exempt on /api/v1/ only; the channel webhook still blocks them")

    # 4. extension exemption: artifacts route only
    extr = R["ExtensionLabelBlock"]
    assert re.findall(r'regex_string\s*=\s*"([^"]+)"', extr) == ["^/api/v1/sessions/[^/]+/artifacts/"], \
        "RestrictedExtensions exemption must be the artifacts read route only"
    ok("RestrictedExtensions exempt on the artifacts route only")

    # 5. normalization everywhere, no lookaround
    assert '"NONE"' not in waf, "no NONE text transformation: every path match must normalize first"
    assert local_list(waf, "path_transforms") == ["URL_DECODE", "NORMALIZE_PATH", "LOWERCASE"], \
        "path_transforms must decode, normalize, then lowercase"
    regexes = re.findall(r'regex_string\s*=\s*"([^"]+)"', waf)
    assert regexes, "no regexes found"
    for rx in regexes:
        assert not re.search(r"\(\?[=!<]", rx), f"{rx}: AWS WAF regex has no lookaround"
        assert rx.startswith("^/"), f"{rx}: path regexes anchor at ^/"
    n_regex = len(regexes)
    n_dyn = len(re.findall(r'dynamic "text_transformation"\s*\{\s*for_each\s*=\s*local\.path_transforms', waf))
    assert n_dyn == n_regex, f"{n_regex} path regexes but {n_dyn} use local.path_transforms"
    ok(f"all {n_regex} path regexes normalize first; no lookaround")

    # 6. rate-limit exemption = egress ip AND key-read path, via label
    egress = R["PlatformEgressKeyReads"]
    assert re.search(r"count\s*\{\}", egress) and "and_statement" in egress, "egress rule counts (labels only) on ip AND path"
    assert "ip_set_reference_statement" in egress and "openid-connect/certs" in egress and ".well-known" in egress, \
        "the exemption is the egress ip set on the discovery/JWKS reads"
    label = re.search(r'rule_label\s*\{\s*name\s*=\s*"([^"]+)"', egress).group(1)
    origin = R["RateLimitOrigin"]
    assert label in origin and "not_statement" in origin, "RateLimitOrigin must skip only the egress label"
    assert "ip_set_reference_statement" not in origin, "the origin rate limit must not exempt the egress ip wholesale"
    assert "ip_set_reference_statement" not in R["RateLimitCredentials"], "nothing is exempt from the credential rate limit"
    ok("rate-limit exemption is the egress ip on the key reads only")

    # 7. log redaction
    logcfg = block(waf, r'resource "aws_wafv2_web_acl_logging_configuration" "edge"')
    headers = set(h.lower() for h in re.findall(r'single_header\s*\{\s*name\s*=\s*"([^"]+)"', logcfg))
    assert {"authorization", "cookie", "x-channel-token"} <= headers, f"redact the credential headers, got {sorted(headers)}"
    assert re.search(r"redacted_fields\s*\{\s*query_string\s*\{\}\s*\}", logcfg), "redact the query string (OAuth code/state)"
    ok("WAF logs redact credential headers and the query string")

    # 8. validate
    if shutil.which("terraform"):
        for args in (["init", "-backend=false", "-input=false", "-no-color"], ["validate", "-no-color"]):
            p = subprocess.run(["terraform", f"-chdir={FOUNDATION}", *args], capture_output=True, text=True, timeout=600, check=False)  # nosec B603
            assert p.returncode == 0, p.stderr
        ok("terraform/ validates")
    else:
        ok("terraform not on PATH: validate skipped")

    print("edge WAF: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
