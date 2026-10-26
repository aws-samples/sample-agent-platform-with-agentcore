#!/usr/bin/env python3
"""Adversarial checks on the CloudFront custom domains (portal, team_auth) and
the Keycloak database snapshot seed. Static; no credentials.

The custom domain becomes the OIDC issuer every token is checked against and
the origin the portal's CORS and API URL derive from; the snapshot seed brings
another account's realms and client secrets in. What must stay true:

  1. both distributions: no custom domain -> no aliases and the CloudFront
     default certificate, exactly as before the variable existed; a custom
     domain -> that one alias, its ACM certificate over SNI, and never a TLS
     floor below 1.2 (the default certificate's TLSv1 must not leak into it)
  2. the certificate is validated to be ACM in us-east-1, in both modules
  3. one hostname per module (local.public_host): the portal's portal_url and
     the facts' cloudfront_domain, Keycloak's base_url (so the issuer) all
     come from it; nothing else reads the distribution's own domain except
     the CNAME-target output
  4. the snapshot seed is create-time only (ignore_changes on
     snapshot_identifier and kms_key_id) and the managed password is still
     set, so the keycloak-db secret stays authoritative after a restore; the
     KMS key applies only together with a snapshot
  5. the staging overlay resets all four to null: staging inherits the
     production tfvars, CloudFront refuses an alias another distribution
     holds, and staging runs no team_auth (an inherited team_auth domain made
     custom_domain_targets index an empty module); the output guards that too
  6. the root passes each variable to its module and defaults all of them to
     null (a deployment that never sets them is unchanged)
  7. terraform validate passes in terraform/

Run: python3 terraform/tests/test_custom_domain_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULES = {"portal": "portal", "team_auth": "team_auth"}
DISTRIBUTION = {"portal": "portal", "team_auth": "team_auth"}


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def module_text(name: str) -> str:
    d = os.path.join(FOUNDATION, "modules", name)
    return "\n".join(read(d, f) for f in sorted(os.listdir(d)) if f.endswith(".tf"))


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def block(text: str, header: str) -> str:
    m = re.search(rf"^\s*{header}\s*\{{", text, re.M)
    assert m, f"no block matching {header!r}"
    depth, i = 0, m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[m.start():i + 1]
        i += 1
    raise AssertionError(f"unterminated block {header!r}")


def attr(text: str, name: str) -> str:
    m = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*(#.*)?$", text, re.M)
    assert m, f"no attribute {name}"
    return m.group(1)


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"\s+#.*$", "", ln) for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def check_distribution(mod: str) -> None:
    text = module_text(mod)
    dist = block(text, f'resource "aws_cloudfront_distribution" "{DISTRIBUTION[mod]}"')
    assert attr(dist, "aliases") == "var.custom_domain == null ? [] : [var.custom_domain.name]", f"{mod}: aliases"
    vc = block(dist, "viewer_certificate")
    assert attr(vc, "cloudfront_default_certificate") == "var.custom_domain == null", f"{mod}: default cert only without a domain"
    assert attr(vc, "acm_certificate_arn") == "try(var.custom_domain.certificate_arn, null)", f"{mod}: acm cert"
    assert attr(vc, "ssl_support_method") == 'var.custom_domain == null ? null : "sni-only"', f"{mod}: sni-only"
    tls = attr(vc, "minimum_protocol_version")
    m = re.fullmatch(r'var\.custom_domain == null \? "TLSv1" : "(TLSv1\.[23]_20\d\d)"', tls)
    assert m, f"{mod}: TLS floor with a custom domain must be TLSv1.2_<year> or newer, got {tls}"


def check_cert_validation(mod: str) -> None:
    v = block(read(FOUNDATION, "modules", mod, "variables.tf"), 'variable "custom_domain"')
    assert "default = null" in v.replace("  ", " "), f"{mod}: custom_domain defaults to null"
    assert 'var.custom_domain == null || can(regex("^arn:aws:acm:us-east-1:[0-9]{12}:certificate/", var.custom_domain.certificate_arn))' in v, \
        f"{mod}: certificate must be validated as ACM us-east-1"


def check_single_host() -> None:
    portal = strip_comments(module_text("portal"))
    assert re.search(r"public_host\s*=\s*try\(var\.custom_domain\.name, aws_cloudfront_distribution\.portal\.domain_name\)", portal)
    outs = read(FOUNDATION, "modules", "portal", "outputs.tf")
    assert attr(block(outs, 'output "portal_url"'), "value") == '"https://${local.public_host}"'
    assert attr(outs, "cloudfront_domain") == "local.public_host"
    # the distribution's own domain: public_host's fallback and the CNAME target output only
    uses = re.findall(r"aws_cloudfront_distribution\.portal\.domain_name", portal)
    assert len(uses) == 2, f"portal: distribution domain read {len(uses)}x (public_host fallback + distribution_domain only)"

    ta = strip_comments(module_text("team_auth"))
    assert re.search(r"public_host\s*=\s*try\(var\.custom_domain\.name, aws_cloudfront_distribution\.team_auth\.domain_name\)", ta)
    assert re.search(r'base_url\s*=\s*"https://\$\{local\.public_host\}"', ta)
    assert re.search(r'issuer_url\s*=\s*"\$\{local\.base_url\}/realms/', ta)
    uses = re.findall(r"aws_cloudfront_distribution\.team_auth\.domain_name", ta)
    assert len(uses) == 2, f"team_auth: distribution domain read {len(uses)}x (public_host fallback + distribution_domain only)"


def check_snapshot_seed() -> None:
    db = block(read(FOUNDATION, "modules", "team_auth", "rds.tf"), 'resource "aws_db_instance" "keycloak"')
    assert attr(db, "snapshot_identifier") == "var.db_snapshot_identifier"
    assert attr(db, "kms_key_id") == "var.db_snapshot_identifier == null ? null : var.db_kms_key_arn", "kms key only with a snapshot"
    assert attr(db, "password") == "random_password.keycloak_db.result", "the managed password must still be set on a restore"
    lc = block(db, "lifecycle")
    ign = attr(lc, "ignore_changes")
    assert set(re.findall(r"\w+", ign)) == {"snapshot_identifier", "kms_key_id"}, f"ignore_changes must be exactly the seed: {ign}"
    assert "password" not in ign


def check_staging_overlay() -> None:
    staging = strip_comments(read(FOUNDATION, "envs", "staging.tfvars"))
    # staging inherits terraform.tfvars: an unset name here is production's value
    for name in ("portal_custom_domain", "team_auth_custom_domain", "keycloak_db_snapshot_identifier", "keycloak_db_kms_key_arn"):
        assert re.search(rf"^{name}\s*=\s*null\s*$", staging, re.M), f"staging must reset {name} to null"
    assert re.search(r"^enable_team_auth\s*=\s*false\s*$", staging, re.M), "staging joins production's Keycloak"
    # and the CNAME-target output must not index a module that is off
    out = block(read(FOUNDATION, "outputs.tf"), 'output "custom_domain_targets"')
    assert "length(module.portal) == 0" in out and "length(module.team_auth) == 0" in out, \
        "custom_domain_targets must guard module.<m>[0] when the module is off"


def check_root_wiring() -> None:
    variables = read(FOUNDATION, "variables.tf")
    for name in ("portal_custom_domain", "team_auth_custom_domain", "keycloak_db_snapshot_identifier", "keycloak_db_kms_key_arn"):
        assert re.search(r"default\s*=\s*null", block(variables, f'variable "{name}"')), f"{name} must default to null"
    main = read(FOUNDATION, "main.tf")
    portal = block(main, 'module "portal"')
    assert attr(portal, "custom_domain") == "var.portal_custom_domain"
    ta = block(main, 'module "team_auth"')
    assert attr(ta, "custom_domain") == "var.team_auth_custom_domain"
    assert attr(ta, "db_snapshot_identifier") == "var.keycloak_db_snapshot_identifier"
    assert attr(ta, "db_kms_key_arn") == "var.keycloak_db_kms_key_arn"


def validate() -> None:
    if not shutil.which("terraform"):
        raise AssertionError("terraform not on PATH")
    for args in (["init", "-backend=false", "-input=false", "-no-color"], ["validate", "-no-color"]):
        p = subprocess.run(["terraform", f"-chdir={FOUNDATION}", *args], capture_output=True, text=True, timeout=600, check=False)  # nosec B603
        assert p.returncode == 0, f"terraform {args[0]} failed:\n{p.stdout}\n{p.stderr}"


def main() -> int:
    for mod in MODULES:
        check_distribution(mod)
    ok("both distributions: no domain = no aliases + default certificate; a domain = its alias, ACM over SNI, TLS >= 1.2")
    for mod in MODULES:
        check_cert_validation(mod)
    ok("custom_domain defaults to null and its certificate must be ACM in us-east-1 (both modules)")
    check_single_host()
    ok("one public host per module: portal_url, facts cloudfront_domain and Keycloak's issuer all derive from it")
    check_snapshot_seed()
    ok("snapshot seed is create-time only, the managed password still applies, the KMS key only with a snapshot")
    check_staging_overlay()
    ok("staging resets both domains and the snapshot seed; the CNAME-target output survives a module that is off")
    check_root_wiring()
    ok("root passes each variable to its module, all default to null")
    validate()
    ok("foundation validates")
    print("custom domain: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
