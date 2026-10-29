"""Render a module's aws_iam_policy_document data sources offline.

Copies the module's .tf files into a scratch root, drops every `resource`
block and the caller-identity/region lookups (replaced by fixed locals), adds
one output per requested document, and runs `terraform plan` with the real AWS
provider on dummy credentials: aws_iam_policy_document is computed locally,
nothing reaches AWS. Providers come from terraform/.terraform when the root
is initialised (no download). Used by the adversarial tests that assert on policy
content and size.

    render(module_dir, ["bedrock_direct", "interactive"], {"kernel_repos": "{}"})
    -> {"bedrock_direct": {...policy JSON...}, ...}
"""

import json
import os
import re
import subprocess  # nosec B404 - fixed argv
import tempfile

TF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT = "ACCOUNTID001"
REGION = "ap-northeast-1"

PROVIDER = f'''
terraform {{
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = ">= 6.50.0, < 7.0.0" }}
  }}
}}
provider "aws" {{
  region                      = "{REGION}"
  access_key                  = "render"
  secret_key                  = "render"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
}}
'''


def _drop_blocks(text: str, pattern: str) -> str:
    """Remove every top-level block whose header matches `pattern`."""
    out, pos = [], 0
    for m in re.finditer(pattern, text, re.M):
        if m.start() < pos:
            continue
        depth, i = 0, text.index("{", m.start())
        while True:
            depth += text[i] == "{"
            depth -= text[i] == "}"
            i += 1
            if depth == 0:
                break
        out.append(text[pos:m.start()])
        pos = i
    out.append(text[pos:])
    return "".join(out)


def render(module_dir: str, documents: list, variables: dict, plugin_cache: str = "") -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        for name in sorted(os.listdir(module_dir)):
            if not name.endswith(".tf") or name in ("outputs.tf", "versions.tf"):
                continue
            text = open(os.path.join(module_dir, name), encoding="utf-8").read()
            text = _drop_blocks(text, r'^resource "')
            text = _drop_blocks(text, r'^data "aws_(caller_identity|region|partition)"')
            text = text.replace("data.aws_caller_identity.current.account_id", f'"{ACCOUNT}"')
            text = text.replace("data.aws_region.current.region", f'"{REGION}"')
            text = text.replace("data.aws_region.current.name", f'"{REGION}"')
            open(os.path.join(tmp, name), "w", encoding="utf-8").write(text)
        with open(os.path.join(tmp, "zz_render.tf"), "w", encoding="utf-8") as f:
            f.write(PROVIDER)
            for d in documents:
                f.write(f'output "{d}" {{ value = data.aws_iam_policy_document.{d}.json }}\n')
        env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        if plugin_cache:
            env["TF_PLUGIN_CACHE_DIR"] = plugin_cache
        init_args = ["terraform", f"-chdir={tmp}", "init", "-backend=false", "-no-color"]
        # Reuse the providers the foundation root already installed (verified
        # against its lock file) instead of downloading the ~800 MB AWS
        # provider again for every render.
        installed = os.path.join(TF_ROOT, ".terraform", "providers")
        if os.path.isdir(os.path.join(installed, "registry.terraform.io", "hashicorp", "aws")):
            init_args.append(f"-plugin-dir={installed}")
        init = subprocess.run(init_args, capture_output=True, text=True, timeout=600, env=env, check=False)  # nosec B603
        if init.returncode != 0:
            raise RuntimeError(f"init failed:\n{init.stderr[-1500:]}")
        args = ["terraform", f"-chdir={tmp}", "plan", "-no-color", "-out=r.plan"]
        for k, v in variables.items():
            args.append(f"-var={k}={v}")
        plan = subprocess.run(args, capture_output=True, text=True, timeout=600, env=env, check=False)  # nosec B603
        if plan.returncode != 0:
            raise RuntimeError(f"plan failed:\n{plan.stderr[-2500:]}")
        show = subprocess.run(["terraform", f"-chdir={tmp}", "show", "-json", "r.plan"],
                              capture_output=True, text=True, timeout=300, env=env, check=False)  # nosec B603
        outputs = json.loads(show.stdout)["planned_values"]["outputs"]
        return {d: json.loads(outputs[d]["value"]) for d in documents}
