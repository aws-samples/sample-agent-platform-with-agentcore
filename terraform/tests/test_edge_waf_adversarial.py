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
  2. the RCE signatures are never downgraded: no rule_action_override names
     Log4JRCE or JavaDeserializationRCE, and KnownBadInputs has no override
     at all
  2b. metadata SSRF is counted for the query arguments and the body only
     (cookie and URI path keep their managed Block), and each is blocked
     again by its label on every path unless the request carries the label of
     a loopback sign-in. That label is an allowlist of one request shape: the
     one Keycloak endpoint, a redirect_uri that is http://localhost or
     127.0.0.1, and a parameter string that - whole, decoded once - is made
     only of that redirect_uri, an https resource indicator with a DNS host
     name, and parameters whose values hold no slash, colon, backslash, at
     sign or percent sign at all; no dotted
     quad but the redirect's own 127.0.0.1; a body inspected in full. The
     shape is checked structurally and then exercised: the rules' own regexes
     are evaluated over real sign-in requests (labelled) and over smuggling
     attempts (never labelled) - addresses with and without a scheme, in
     every spelling, in every parameter position - and every character a URL
     needs is shown to be refused in any other parameter
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
import urllib.parse

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


def local_str(text: str, name: str) -> str:
    """A string local, HCL escapes undone (the module's regexes use only \\\\)."""
    m = re.search(rf'^  {name}\s*=\s*"((?:[^"\\]|\\.)*)"', text, re.M)
    assert m, f"no local {name}"
    # a local may be built from other locals: resolve them all the way down
    return re.sub(r"\$\{local\.(\w+)\}", lambda r: local_str(text, r.group(1)), m.group(1).replace("\\\\", "\\"))


def spans(text: str, header: str) -> list:
    """(start, end) of every brace-balanced block opened by `header {`."""
    return [(m.start(), m.start() + len(balanced(text, m.start()))) for m in re.finditer(rf"\b{header}\s*\{{", text)]


def regex_statements(rule: str, waf: str) -> list:
    """Each regex_match_statement of a rule, in order:
    (regex with locals resolved, field, transforms, inside a NOT, inside the OR)."""
    nots, ors = spans(rule, "not_statement"), spans(rule, "or_statement")
    out = []
    for start, end in spans(rule, "regex_match_statement"):
        body = rule[start:end]
        quoted = re.search(r'regex_string\s*=\s*"((?:[^"\\]|\\.)*)"', body)
        bare = re.search(r"regex_string\s*=\s*local\.(\w+)\s*$", body, re.M)  # terraform fmt unwraps "${local.x}"
        assert quoted or bare, f"regex_string is neither a string nor a bare local: {body[:120]}"
        if quoted:
            rx = re.sub(r"\$\{local\.(\w+)\}", lambda m: local_str(waf, m.group(1)), quoted.group(1).replace("\\\\", "\\"))
        else:
            rx = local_str(waf, bare.group(1))
        ftm = balanced(body, body.index("field_to_match"))
        if "uri_path" in ftm:
            field = "uri_path"
        elif "single_query_argument" in ftm:
            field = "arg:" + re.search(r'name\s*=\s*"([^"]+)"', ftm).group(1)
        elif "query_string" in ftm:
            field = "query"
        elif re.search(r"\bbody\s*\{", ftm):
            field = "body:" + re.search(r'oversize_handling\s*=\s*"([^"]+)"', ftm).group(1)
        else:
            raise AssertionError(f"unexpected field_to_match: {ftm}")
        dyn = re.search(r'dynamic "text_transformation"\s*\{\s*for_each\s*=\s*local\.(\w+)', body)
        transforms = dyn.group(1) if dyn else ",".join(re.findall(r'type\s*=\s*"([^"]+)"', body))
        inside = lambda blocks: any(a < start and end <= b for a, b in blocks)
        out.append((rx, field, transforms, inside(nots), inside(ors)))
    return out


def decoded(text: str) -> str:
    """URL_DECODE then COMPRESS_WHITE_SPACE, as local.decode_transforms."""
    return re.sub(r"[\t\n\r\f\v ]+", " ", urllib.parse.unquote_plus(text))


def labelled(stmts: list, path: str, payload: str, oversize: bool = False) -> bool:
    """Evaluate a label rule's statements (their order is asserted by the
    caller) over one request, the way AWS WAF combines them."""
    def hit(rx: str, field: str, transforms: str) -> bool:
        if field == "uri_path":
            return bool(re.search(rx, path.lower()))
        if field.startswith("arg:"):
            name = field[4:]
            values = [v for k, _, v in (kv.partition("=") for kv in payload.split("&")) if urllib.parse.unquote_plus(k) == name]
            return any(re.search(rx, urllib.parse.unquote_plus(v)) for v in values)
        if field.startswith("body:") and oversize:
            return field == "body:MATCH"
        text = decoded(payload) if transforms == "decode_transforms" else urllib.parse.unquote_plus(payload)
        return bool(re.search(rx, text))
    results = [hit(rx, field, transforms) != negated for rx, field, transforms, negated, _ in stmts]
    in_or = [r for r, st in zip(results, stmts) if st[4]]
    plain = [r for r, st in zip(results, stmts) if not st[4]]
    return all(plain) and any(in_or)


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
    for sig in ("Log4JRCE", "JavaDeserializationRCE"):
        assert not any(sig in o for o in overridden), f"{sig} must keep its managed Block action"
        assert sig not in acl, f"{sig} must not be named anywhere in the ACL (an override or a label exemption)"
    assert "rule_action_override" not in R["AWSManagedRulesKnownBadInputsRuleSet"], "KnownBadInputs takes no overrides"
    ok("Log4J / Java deserialization keep blocking everywhere")

    # 2b. metadata SSRF: counted for query + body only, re-blocked unless labelled a loopback sign-in
    assert not any("EC2MetaDataSSRF" in o for o in overridden), \
        "EC2MetaDataSSRF belongs in ssrf_label only (not crs_count / content_label: those have no re-block for it)"
    ssrf = local_list(waf, "ssrf_label")
    assert sorted(ssrf) == ["EC2MetaDataSSRF_BODY", "EC2MetaDataSSRF_QUERYARGUMENTS"], \
        f"only the query-argument and body SSRF rules may be counted (cookie and URI path keep blocking), got {ssrf}"
    crs = R["AWSManagedRulesCommonRuleSet"]
    each = re.search(r"for_each\s*=\s*concat\(([^)]*)\)", crs)
    assert each and "local.ssrf_label" in each.group(1), "the common rule set must count local.ssrf_label"
    assert not re.search(r"override_action\s*\{\s*count", crs), "the common rule set must not be counted as a whole"
    mentions = {n for n, b in R.items() if "EC2MetaDataSSRF" in b}
    assert mentions == {"SsrfQueryLabelBlock", "SsrfBodyLabelBlock"}, \
        f"the SSRF labels may be named by the two re-block rules only, got {sorted(mentions)}"

    def action(body: str) -> str:
        m = re.search(r"^    action\s*\{\s*(\w+)\s*\{\}", body, re.M)
        assert m, "rule without an action"
        return m.group(1)

    loopback, loopback_ip, resource, param, stray, quad = (
        local_str(waf, n) for n in ("oauth_loopback_uri", "oauth_loopback_ip_uri", "oauth_resource_uri", "oauth_param", "oauth_stray_char", "ipv4_quad"))
    # The patterns verbatim. The cases below show what they must and must not
    # match; this pins the rest (a character added to a class that no case
    # happens to exercise is still a change to what the edge lets through).
    assert loopback == r"http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?", f"oauth_loopback_uri changed: {loopback}"
    assert loopback_ip == r"http://127\.0\.0\.1(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?", f"oauth_loopback_ip_uri changed: {loopback_ip}"
    assert resource == r"https://([a-z0-9-]+\.)+[a-z]{2,}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?", f"oauth_resource_uri changed: {resource}"
    assert param == r"[a-z_]+=[A-Za-z0-9._~ +-]*", f"oauth_param changed: {param}"
    assert stray == r"[^A-Za-z0-9._~ +&=:/-]", f"oauth_stray_char changed: {stray}"
    assert quad == r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", f"ipv4_quad changed: {quad}"
    grammar, second_equals = local_str(waf, "oauth_param_list"), local_str(waf, "oauth_second_equals")
    assert grammar == f"^(&?(redirect_uri={loopback}|resource={resource}|{param}))+$", f"oauth_param_list changed: {grammar}"
    assert second_equals == "=[^&]*=", f"oauth_second_equals changed: {second_equals}"
    # AWS WAF refuses an anchor inside a group (UpdateWebACL fails at deploy
    # time; nothing offline shows it - see check_waf_regex_syntax.py). The
    # module must not grow one back.
    for name in ("oauth_loopback_uri", "oauth_loopback_ip_uri", "oauth_resource_uri", "oauth_param", "oauth_param_list",
                 "oauth_second_equals", "oauth_stray_char", "ipv4_quad"):
        body = local_str(waf, name)
        assert not re.search(r"\(\^|\|\^|\$\||\$\)\S", body.replace("(&|$)", "")), f"{name}: an anchor inside a group: {body}"
    # "&?" alone would let two parameters run together: every form holds one
    # "=", so the second-equals statement is what makes the list "&"-separated.
    assert all(form.count("=") == 1 for form in (f"redirect_uri={loopback}", f"resource={resource}", param)), \
        "each parameter form must hold exactly one '=': the second-equals statement relies on it"
    assert local_list(waf, "decode_transforms") == ["URL_DECODE", "COMPRESS_WHITE_SPACE"], \
        "decode once, as the origin does, then fold line breaks (so the quad count spans them)"
    PAIRS = (
        # block rule, SSRF label it re-blocks, label rule, its label, endpoint, where the request's parameters are
        ("SsrfQueryLabelBlock", "EC2MetaDataSSRF_QueryArguments", "OAuthLoopbackAuthorize", "agent-platform:oauth-loopback-authorize", "auth", "query"),
        ("SsrfBodyLabelBlock", "EC2MetaDataSSRF_Body", "OAuthLoopbackToken", "agent-platform:oauth-loopback-token", "token", "body"),
    )
    S = {}
    for block_name, ssrf_key, label_name, label, endpoint, where in PAIRS:
        # the re-block: SSRF label AND NOT the sign-in label; no path, no regex, nothing else to loosen
        blk = R[block_name]
        assert action(blk) == "block", f"{block_name} must block"
        assert "regex_match_statement" not in blk and "uri_path" not in blk and "or_statement" not in blk, \
            f"{block_name}: SSRF label AND NOT the sign-in label - no path exemption, no alternative"
        keys = re.findall(r'key\s*=\s*"([^"]+)"', blk)
        assert keys == ["${local.crs_label}:" + ssrf_key, label], f"{block_name} must match {ssrf_key} unless {label}, got {keys}"
        (ns, ne), = spans(blk, "not_statement")
        assert label in blk[ns:ne] and ssrf_key not in blk[ns:ne], f"{block_name}: the NOT wraps the sign-in label, not the SSRF label"
        # the label rule: tags only, and its label is used by its own block rule alone
        lab = R[label_name]
        assert action(lab) == "count", f"{label_name} only labels (count): it must never allow or block"
        assert re.findall(r'rule_label\s*\{\s*name\s*=\s*"([^"]+)"', lab) == [label], f"{label_name} must add exactly the label {label}"
        assert sorted(n for n, b in R.items() if label in b) == sorted([block_name, label_name]), \
            f"{label} may be set by {label_name} and read by {block_name} only"
        assert len(spans(lab, "or_statement")) == 1 and len(spans(lab, "and_statement")) == 1, f"{label_name}: one AND holding one OR"
        assert len(re.findall(r"_statement\s*\{", lab)) - len(re.findall(r"\b(and|or|not)_statement\s*\{", lab)) == 8, \
            f"{label_name}: eight regex statements and no other kind of match"
        if where == "query":
            redirect_field = "arg:redirect_uri"
            whole = counted = "query"
            want_redirect, want_redirect_ip = f"^{loopback}$", f"^{loopback_ip}$"
        else:
            redirect_field = whole = "body:NO_MATCH"   # a body not inspected in full matches nothing positive
            counted = "body:MATCH"                      # ... and counts as holding dotted quads
            want_redirect, want_redirect_ip = f"(^|&)redirect_uri={loopback}(&|$)", f"(^|&)redirect_uri={loopback_ip}(&|$)"
        expected = [
            (f"^/realms/[^/]+/protocol/openid-connect/{endpoint}$", "uri_path", "path_transforms", False, False),
            (want_redirect, redirect_field, "URL_DECODE", False, False),
            (grammar, whole, "URL_DECODE", False, False),
            # ... and no parameter with a second "=": two forms run together without "&"
            (second_equals, counted, "URL_DECODE", True, False),
            # independent of how the engine anchors "$": no character outside the allowlist, anywhere
            (stray, counted, "URL_DECODE", True, False),
            (f"{quad}.*{quad}", counted, "decode_transforms", True, False),
            (quad, counted, "decode_transforms", True, True),
            (want_redirect_ip, redirect_field, "URL_DECODE", False, True),
        ]
        got = regex_statements(lab, waf)
        assert got == expected, f"{label_name}: statements differ from the allowlist\n  got      {got}\n  expected {expected}"
        S[where] = got
    ok("metadata SSRF counted for query + body only; re-blocked on every path unless labelled a loopback sign-in")
    ok("the sign-in label is an allowlist: endpoint, loopback redirect, every parameter matched, no stray character, one dotted quad at most, oversize fails closed")

    # ... and the allowlist does what it says, evaluated with the module's own regexes
    AUTH, TOKEN = "/realms/agent-platform/protocol/openid-connect/auth", "/realms/agent-platform/protocol/openid-connect/token"
    q = urllib.parse.quote
    cb, gw = q("http://localhost:5173/callback", safe=""), q("https://gw-abc123.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp", safe="")
    base_q = "response_type=code&client_id=portal-web&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&state=x_Y-z&scope=openid+profile"
    base_b = "grant_type=authorization_code&code=5b0c.1f2e.9a7d&client_id=portal-web&code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    meta = q("http://169.254.169.254/latest/meta-data/iam/security-credentials/", safe="")
    legit = [
        ("authorize, loopback redirect", "query", AUTH, f"{base_q}&redirect_uri={cb}"),
        ("authorize, loopback redirect + resource indicator", "query", AUTH, f"{base_q}&redirect_uri={cb}&resource={gw}"),
        ("authorize, 127.0.0.1 without a path", "query", AUTH, f"{base_q}&redirect_uri={q('http://127.0.0.1:49152', safe='')}"),
        ("authorize, another realm", "query", "/realms/agent-platform-staging/protocol/openid-connect/auth", f"redirect_uri={cb}&{base_q}"),
        # the parameters and their order as Claude Code 2.1.322 sends them (captured from `claude mcp login --no-browser`)
        ("authorize, as Claude Code's MCP sign-in sends it", "query", AUTH,
         f"response_type=code&client_id=portal-web&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
         f"&code_challenge_method=S256&redirect_uri={cb}&state=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk&resource={gw}"),
        ("token, loopback redirect", "body", TOKEN, f"{base_b}&redirect_uri={cb}"),
        ("token, loopback redirect + resource indicator", "body", TOKEN, f"redirect_uri={cb}&{base_b}&resource={gw}"),
    ]
    for name, where, path, payload in legit:
        assert labelled(S[where], path, payload), f"a real sign-in must be labelled: {name}"
    hostile = [
        ("redirect to the metadata address", f"{base_q}&redirect_uri={meta}"),
        ("loopback redirect plus a request_uri at the metadata address", f"{base_q}&redirect_uri={cb}&request_uri={meta}"),
        ("... as a decimal host", f"{base_q}&redirect_uri={cb}&request_uri={q('http://2852039166/latest/', safe='')}"),
        ("... as a hex host", f"{base_q}&redirect_uri={cb}&request_uri={q('http://0xA9FEA9FE/', safe='')}"),
        ("... as an IPv6-mapped host", f"{base_q}&redirect_uri={cb}&request_uri={q('http://[::ffff:169.254.169.254]/', safe='')}"),
        ("... as the IMDS IPv6 address", f"{base_q}&redirect_uri={cb}&request_uri={q('http://[fd00:ec2::254]/', safe='')}"),
        ("... double-encoded", f"{base_q}&redirect_uri={cb}&request_uri={q(meta, safe='')}"),
        ("... scheme-relative", f"{base_q}&redirect_uri={cb}&request_uri={q('//169.254.169.254/latest/', safe='')}"),
        ("... with backslashes", f"{base_q}&redirect_uri={cb}&request_uri={q('http:' + chr(92) * 2 + '169.254.169.254', safe='')}"),
        ("... next to a valid resource indicator", f"{base_q}&redirect_uri={cb}&resource={gw}&request_uri={meta}"),
        ("a second redirect_uri", f"{base_q}&redirect_uri={cb}&redirect_uri={meta}"),
        ("a second redirect_uri next to a valid resource indicator", f"{base_q}&redirect_uri={cb}&redirect_uri={meta}&resource={gw}"),
        ("resource indicator at an IP literal", f"{base_q}&redirect_uri={cb}&resource={q('https://169.254.169.254/', safe='')}"),
        ("resource indicator at a decimal host", f"{base_q}&redirect_uri={cb}&resource={q('https://2852039166/', safe='')}"),
        ("resource indicator over http", f"{base_q}&redirect_uri={cb}&resource={q('http://example.com/', safe='')}"),
        ("two resource indicators, one hostile", f"{base_q}&redirect_uri={cb}&resource={gw}&resource={meta}"),
        ("loopback host as userinfo", f"{base_q}&redirect_uri={q('http://localhost:5173@169.254.169.254/', safe='')}"),
        ("loopback host as a subdomain", f"{base_q}&redirect_uri={q('http://localhost.evil.example/', safe='')}"),
        ("loopback redirect with a query of its own", f"{base_q}&redirect_uri={q('http://localhost:5173/cb?u=' + meta, safe='')}"),
        ("an encoded line break hiding a second URL", f"{base_q}&redirect_uri={cb}&x=a%0A{meta}"),
        # no scheme at all: nothing here carries :// or //
        ("request_uri at the bare metadata address", f"{base_q}&redirect_uri={cb}&request_uri=169.254.169.254/latest/meta-data/iam/security-credentials/"),
        ("request_uri at the bare metadata address, no path", f"{base_q}&redirect_uri={cb}&request_uri=169.254.169.254"),
        ("the bare metadata address in an unknown parameter", f"{base_q}&redirect_uri={cb}&x=169.254.169.254"),
        ("the bare metadata address appended to a known parameter", f"{base_q}.169.254.169.254&redirect_uri={cb}"),
        ("the bare container-credentials address", f"{base_q}&redirect_uri={cb}&x=169.254.170.2"),
        ("a bare address with a port", f"{base_q}&redirect_uri={cb}&x=169.254.169.254:80"),
        ("a bare IPv6 address", f"{base_q}&redirect_uri={cb}&x=fd00:ec2::254"),
        ("a bare path", f"{base_q}&redirect_uri={cb}&x=latest/meta-data"),
        ("userinfo without a scheme", f"{base_q}&redirect_uri={cb}&x=a@169.254.169.254"),
        ("a second dotted quad beside a 127.0.0.1 redirect", f"{base_q}&redirect_uri={q('http://127.0.0.1:5173/cb', safe='')}&x=169.254.169.254"),
        ("a dotted quad inside the resource indicator's path", f"{base_q}&redirect_uri={cb}&resource={q('https://example.com/169.254.169.254', safe='')}"),
        ("a parameter name with a slash", f"{base_q}&redirect_uri={cb}&a/b=c"),
        ("a value-less trailing ampersand run", f"{base_q}&redirect_uri={cb}&&x=1"),
        # two parameters run together without "&": the origin reads ONE parameter whose value holds the second
        ("a parameter glued to the redirect_uri", f"{base_q}&x=aredirect_uri={cb}"),
        ("a redirect_uri glued after another parameter's value", f"{base_q}&redirect_uri={cb}state=x"),
        ("two plain parameters glued", f"{base_q}&redirect_uri={cb}&a=bc=d"),
        ("a resource indicator glued into a value", f"{base_q}&redirect_uri={cb}&x=aresource={gw}"),
    ]
    for name, payload in hostile:
        assert not labelled(S["query"], AUTH, payload), f"authorization request must not be labelled: {name}"
        body = payload.replace(base_q, base_b)
        assert not labelled(S["body"], TOKEN, body), f"token request must not be labelled: {name}"
    smuggled = f"{base_b}&x=a%26redirect_uri%3Dhttp://localhost:5173/callback&redirect_uri={meta}"
    assert not labelled(S["body"], TOKEN, smuggled), "a redirect_uri smuggled behind an encoded ampersand must not carry a second URL through"
    assert not labelled(S["body"], TOKEN, f"{base_b}&redirect_uri={cb}", oversize=True), \
        "a body WAF could not inspect in full is never labelled, however it starts"
    for name, where, _, payload in legit:
        for path in ("/realms/agent-platform/protocol/openid-connect/auth/device", "/realms/a/b/protocol/openid-connect/auth",
                     "/realms/agent-platform/protocol/openid-connect/userinfo", "/realms/agent-platform/login-actions/authenticate",
                     "/admin/realms/agent-platform/clients", "/api/v1/agents", "/mcp", "/",
                     TOKEN if where == "query" else AUTH):
            assert not labelled(S[where], path, payload), f"{name}: no label off its own endpoint ({path})"
    ok(f"{len(legit)} real sign-in requests are labelled; {2 * len(hostile) + 2} smuggling attempts and every other path are not")

    # The list is "&"-separated and nothing else: every way of gluing two
    # parameters of a real request together is refused, at every position.
    for where, path, base in (("query", AUTH, f"{base_q}&redirect_uri={cb}&resource={gw}"), ("body", TOKEN, f"{base_b}&redirect_uri={cb}&resource={gw}")):
        parts = base.split("&")
        for i in range(1, len(parts)):
            glued = "&".join(parts[:i - 1] + [parts[i - 1] + parts[i]] + parts[i + 1:])
            assert not labelled(S[where], path, glued), f"{where}: parameters {i - 1} and {i} run together must not be labelled"
        assert not labelled(S[where], path, base + "&"), f"{where}: a trailing ampersand must not be labelled"
        assert not labelled(S[where], path, "&" + base.replace("&", "&&", 1)), f"{where}: a doubled ampersand must not be labelled"
    ok("the parameter list is ampersand-separated only: no two parameters of a real request can be run together")

    # The property the cases above only sample: in any parameter other than
    # redirect_uri and resource, no character a URL, a path or an encoding
    # needs can appear - anywhere in the value, alone or beside ordinary text.
    for where, path, base in (("query", AUTH, f"{base_q}&redirect_uri={cb}"), ("body", TOKEN, f"{base_b}&redirect_uri={cb}")):
        assert labelled(S[where], path, base + "&x=plain.value-1_2~3"), "a plain extra parameter is still a sign-in"
        for ch in "/:\\@%#?[]{}<>|^`\"'();,!$*=\t\n\r\x00":
            for value in (ch, "a" + ch, ch + "a", "a" + ch + "b"):
                extra = "&x=" + q(value, safe="")
                assert not labelled(S[where], path, base + extra), f"{where}: {ch!r} in another parameter must not be labelled"
                assert not labelled(S[where], path, extra[1:] + "&" + base), f"{where}: {ch!r} in a leading parameter must not be labelled"
        assert not labelled(S[where], path, base + "&x=1.2.3.4"), f"{where}: a dotted quad in another parameter must not be labelled"
        assert not labelled(S[where], path, base.replace("client_id=portal-web", "client_id=1.2.3.4")), f"{where}: a dotted quad in a known parameter must not be labelled"
    ok("no other parameter can hold a URL character (/ : \\ @ % and 22 more, in any position) or a dotted quad")

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
    assert max(prio["OAuthLoopbackAuthorize"], prio["OAuthLoopbackToken"]) < min(prio["SsrfQueryLabelBlock"], prio["SsrfBodyLabelBlock"]), \
        "the sign-in labels must be set before the re-block rules read them"
    for n in ("ContentLabelBlock", "ContentLabelBlockWebhook", "ExtensionLabelBlock",
              "SsrfQueryLabelBlock", "SsrfBodyLabelBlock", "OAuthLoopbackAuthorize", "OAuthLoopbackToken"):
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
    all_rx = [st for body in R.values() for st in regex_statements(body, waf)]
    assert all_rx, "no regexes found"
    for rx, field, transforms, _, _ in all_rx:
        assert not re.search(r"\(\?[=!<]", rx), f"{rx}: AWS WAF regex has no lookaround"
        assert len(rx) <= 512, f"{rx}: AWS WAF regex strings are at most 512 characters"
    paths = [st for st in all_rx if st[1] == "uri_path"]
    for rx, _, transforms, _, _ in paths:
        assert rx.startswith("^/"), f"{rx}: path regexes anchor at ^/"
        assert transforms == "path_transforms", f"{rx}: a path match must normalize with local.path_transforms"
    payload_rules = {n for n, body in R.items() if any(st[1] != "uri_path" for st in regex_statements(body, waf))}
    assert payload_rules == {"OAuthLoopbackAuthorize", "OAuthLoopbackToken"}, \
        f"only the two sign-in label rules may match on anything but the URI path, got {sorted(payload_rules)}"
    n_regex = len(paths)
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
