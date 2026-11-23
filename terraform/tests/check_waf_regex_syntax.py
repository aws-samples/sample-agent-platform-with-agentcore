#!/usr/bin/env python3
"""Ask AWS WAF itself whether it accepts every regex in the edge web ACL.

Why this exists: AWS WAF supports a subset of PCRE and publishes no complete
list of what it leaves out; "the AWS WAF API reports a failure" is the
documented way to find out. Nothing offline finds out earlier:

  - terraform validate / plan do not look inside regex_string;
  - wafv2 check-capacity returns a capacity for a pattern with an unbalanced
    parenthesis, so it validates nothing about the pattern;
  - test_edge_waf_adversarial.py evaluates the patterns with Python's engine,
    which accepts constructs WAF refuses.

A refused pattern therefore surfaces at UpdateWebACL, in a deploy job, after
the merge (2026-11-16: "(^|&)" inside a repeated group, refused with
WAFInvalidParameterException; the deploy failed and main stayed red).

What it does: for each distinct regex_string of the module, with locals
resolved, it creates a regex pattern set holding that one pattern and deletes
it again at once. Creating a pattern set runs the validator; the set is
attached to nothing, so no traffic is affected, and a set that fails to
create leaves nothing behind. The scratch name is fixed, so a run that died
between create and delete is cleaned up by the next one.

Needs AWS credentials with wafv2:CreateRegexPatternSet, DeleteRegexPatternSet
and ListRegexPatternSets (CLOUDFRONT scope, us-east-1). Run it before pushing
a change to any regex of modules/edge_waf:

    python3 terraform/tests/check_waf_regex_syntax.py

Exit code 0 when WAF accepts every pattern, 1 when it refuses any, 2 when the
check could not run (no credentials, no boto3).
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SCRATCH = "edge-waf-regex-syntax-check-scratch"
SCOPE, REGION = "CLOUDFRONT", "us-east-1"


def patterns() -> list:
    """Every distinct regex of the web ACL, locals resolved, in file order."""
    import test_edge_waf_adversarial as t

    waf = t.read(t.WAF)
    acl = t.block(waf, r'resource "aws_wafv2_web_acl" "edge"')
    seen, out = set(), []
    for name, body in t.rules(acl).items():
        for rx, *_ in t.regex_statements(body, waf):
            if rx not in seen:
                seen.add(rx)
                out.append((name, rx))
    return out


def main() -> int:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("check_waf_regex_syntax: boto3 is not installed", file=sys.stderr)
        return 2
    todo = patterns()
    if not todo:
        print("check_waf_regex_syntax: no regex found in the module", file=sys.stderr)
        return 2
    waf = boto3.client("wafv2", region_name=REGION)

    def cleanup() -> None:
        for s in waf.list_regex_pattern_sets(Scope=SCOPE, Limit=100).get("RegexPatternSets", []):
            if s["Name"] == SCRATCH:
                waf.delete_regex_pattern_set(Name=SCRATCH, Scope=SCOPE, Id=s["Id"], LockToken=s["LockToken"])

    refused = []
    try:
        cleanup()  # a previous run that died between create and delete
        for rule, rx in todo:
            try:
                made = waf.create_regex_pattern_set(
                    Name=SCRATCH, Scope=SCOPE, RegularExpressionList=[{"RegexString": rx}],
                    Description="throwaway: validates one regex of the edge web ACL, deleted at once")["Summary"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "WAFInvalidParameterException":
                    raise
                refused.append((rule, rx))
                print(f"  REFUSED  {rule}: {rx}")
                continue
            waf.delete_regex_pattern_set(Name=SCRATCH, Scope=SCOPE, Id=made["Id"], LockToken=made["LockToken"])
            print(f"  ok       {rule}: {rx[:90]}{'...' if len(rx) > 90 else ''}")
    except (BotoCoreError, ClientError) as exc:
        print(f"check_waf_regex_syntax: could not run: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - best effort; the next run cleans up
            pass
    if refused:
        print(f"AWS WAF refuses {len(refused)} of {len(todo)} patterns: UpdateWebACL would fail at deploy time")
        return 1
    print(f"AWS WAF accepts all {len(todo)} distinct patterns of the edge web ACL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
