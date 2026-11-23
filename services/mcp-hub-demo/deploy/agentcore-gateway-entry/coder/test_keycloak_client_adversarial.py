#!/usr/bin/env python3
"""keycloak_client.py keeps the Coder client secret in the Kubernetes Secret
and nowhere else. The real main() runs against a stand-in Keycloak and a
stand-in kubectl on PATH that records its arguments and its stdin.

  1. a run against a working Keycloak and kubectl exits 0, prints nothing on
     stdout and never the secret on stderr, calls kubectl exactly once as
     `--context <ctx> -n <ns> apply -f -` with the secret in no argument, and
     the manifest on its stdin is the Secret coder-oidc of <ns> whose only key
     client-secret decodes to the client secret (for an existing client and a
     new one)
  2. it refuses, without calling kubectl: an empty or missing secret from
     Keycloak, the old one-argument form (whose caller expected the secret on
     stdout), an issuer or a coder_url that is not https; a failing kubectl
     makes the run fail, still without printing the secret
  3. the source: no print() or write() call takes the secret, the manifest or
     the value; the Secret name and key are the ones values.yaml.tpl reads;
     deploy.sh passes the cluster context and the namespace, discards stdout,
     and no longer writes the secret to a file or builds the Secret itself

Run: python3 services/mcp-hub-demo/deploy/agentcore-gateway-entry/coder/test_keycloak_client_adversarial.py
"""

from __future__ import annotations

import ast
import atexit
import base64
import importlib.util
import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[PASS] {name}")
    else:
        failures.append(name)
        print(f"[FAIL] {name}" + (f": {detail}" if detail else ""))


# ------------------------------------------------------------------ the stand-ins
fake_boto3 = types.ModuleType("boto3")


class _SecretsManager:
    def get_secret_value(self, SecretId):  # noqa: N803 - the boto3 keyword
        return {"SecretString": json.dumps({"username": "admin", "password": "admin-pw"})}


fake_boto3.client = lambda service, region_name=None: _SecretsManager()
sys.modules["boto3"] = fake_boto3
spec = importlib.util.spec_from_file_location("keycloak_client", HERE / "keycloak_client.py")
kc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kc)

work = Path(tempfile.mkdtemp(prefix="kc-test-"))
atexit.register(shutil.rmtree, work, True)
bin_dir = work / "bin"
bin_dir.mkdir()
kubectl_log = work / "kubectl.jsonl"
(bin_dir / "kubectl").write_text(
    f"#!{sys.executable}\n"
    "import json, os, sys\n"
    "data = sys.stdin.read()\n"
    "with open(os.environ['FAKE_KUBECTL_LOG'], 'a') as fh:\n"
    "    fh.write(json.dumps({'argv': sys.argv[1:], 'stdin': data}) + '\\n')\n"
    "print('secret/coder-oidc configured')\n"
    "sys.exit(int(os.environ.get('FAKE_KUBECTL_RC', '0')))\n"
)
(bin_dir / "kubectl").chmod(0o755)
os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
os.environ["FAKE_KUBECTL_LOG"] = str(kubectl_log)

TF = {
    "oidc_issuer_url": "https://idp.example.com/realms/agent-platform",
    "coder_url": "https://coder.example.com",
    "region": "ap-northeast-1",
    "oidc_audience": "agent-platform",
    "team_claim": "team",
}


def keycloak(secret_reply, existing: bool):
    """A stand-in for the admin API: (_post_form, _api, the calls made)."""
    calls = []
    state = {"exists": existing}

    def post_form(url, data):
        calls.append(("POST-FORM", url))
        return {"access_token": "admin-token"}

    def api(url, token, method="GET", payload=None):
        calls.append((method, url))
        if url.endswith("/client-secret"):
            return secret_reply
        if "/clients?clientId=" in url:
            return [{"id": "uuid-1", "clientId": kc.CLIENT_ID, "attributes": {}}] if state["exists"] else []
        if method == "POST" and url.endswith("/clients"):
            state["exists"] = True
        if url.endswith("/protocol-mappers/models") and method == "GET":
            return []
        return None

    return post_form, api, calls


def run(args, secret_reply, existing=True, tf=None, kubectl_rc=0):
    """main() as deploy.sh runs it: (exit code, stdout, stderr, kubectl calls, keycloak calls)."""
    kubectl_log.write_text("")
    os.environ["FAKE_KUBECTL_RC"] = str(kubectl_rc)
    tf_file = work / "tf.json"
    tf_file.write_text(json.dumps({k: {"value": v} for k, v in (tf or TF).items()}))
    kc._post_form, kc._api, calls = keycloak(secret_reply, existing)
    out, err = io.StringIO(), io.StringIO()
    sys.argv = ["keycloak_client.py", *[str(tf_file) if a == "TF" else a for a in args]]
    with redirect_stdout(out), redirect_stderr(err):
        code = kc.main()
    kube = [json.loads(ln) for ln in kubectl_log.read_text().splitlines() if ln.strip()]
    return code, out.getvalue(), err.getvalue(), kube, calls


# ------------------------------------------------------------------ 1. where the secret goes
for existing in (True, False):
    which = "an existing client" if existing else "a new client"
    secret = "kc-" + secrets.token_hex(16)
    code, out, err, kube, _ = run(["TF", "caller-ctx", "coder"], {"type": "secret", "value": secret}, existing)
    check(f"{which}: the run exits 0", code == 0, err)
    check(f"{which}: nothing on stdout", out == "", repr(out))
    check(f"{which}: the secret is not on stderr", secret not in err)
    check(f"{which}: kubectl is called once, as --context caller-ctx -n coder apply -f -",
          [k["argv"] for k in kube] == [["--context", "caller-ctx", "-n", "coder", "apply", "-f", "-"]], str(kube))
    check(f"{which}: the secret is in no kubectl argument", all(secret not in " ".join(k["argv"]) for k in kube))
    try:
        m = json.loads(kube[0]["stdin"])
    except (IndexError, ValueError):
        m = {}
    check(f"{which}: kubectl's stdin is the Secret coder-oidc of namespace coder, type Opaque",
          (m.get("apiVersion"), m.get("kind"), m.get("type"), m.get("metadata")) == ("v1", "Secret", "Opaque", {"name": "coder-oidc", "namespace": "coder"}),
          str({k: v for k, v in m.items() if k != "data"}))
    data = m.get("data") or {}
    check(f"{which}: its only key client-secret decodes to the client secret",
          list(data) == ["client-secret"] and base64.b64decode(data["client-secret"]).decode() == secret)

# ------------------------------------------------------------------ 2. what it refuses
for why, reply in (("an empty secret", {"type": "secret", "value": ""}), ("no value", {"type": "secret"}), ("no reply", None)):
    code, out, err, kube, _ = run(["TF", "caller-ctx", "coder"], reply)
    check(f"Keycloak returning {why}: exit 1 and kubectl is never called", code == 1 and kube == [], f"{code} {kube}")
secret = "kc-" + secrets.token_hex(16)
code, out, err, kube, _ = run(["TF", "caller-ctx", "coder"], {"type": "secret", "value": secret}, kubectl_rc=1)
check("a failing kubectl fails the run", code == 1, str(code))
check("... and the secret is printed nowhere", secret not in out + err and out == "")
for why, args in (("the old one-argument form (its caller read the secret from stdout)", ["TF"]),
                  ("no namespace", ["TF", "caller-ctx"]), ("an extra argument", ["TF", "caller-ctx", "coder", "x"])):
    code, out, err, kube, calls = run(args, {"type": "secret", "value": secret})
    check(f"{why}: exit 2, no Keycloak or kubectl call", code == 2 and kube == [] and calls == [], f"{code} {calls} {kube}")
for why, tf in (("an http issuer", {**TF, "oidc_issuer_url": "http://idp.example.com/realms/agent-platform"}),
                ("an issuer without a realm", {**TF, "oidc_issuer_url": "https://idp.example.com/"}),
                ("an http coder_url", {**TF, "coder_url": "http://coder.example.com"})):
    code, out, err, kube, calls = run(["TF", "caller-ctx", "coder"], {"type": "secret", "value": secret}, tf=tf)
    check(f"{why} is refused before any Keycloak or kubectl call", code == 1 and kube == [] and calls == [], f"{code} {calls} {kube}")

# ------------------------------------------------------------------ 3. the source and its callers
src = (HERE / "keycloak_client.py").read_text()
leaky = []
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Call) and (getattr(node.func, "id", None) == "print" or getattr(node.func, "attr", None) == "write"):
        names = {n.id for a in [*node.args, *(k.value for k in node.keywords)] for n in ast.walk(a) if isinstance(n, ast.Name)}
        if names & {"secret", "value", "manifest", "admin", "token"}:
            leaky.append(ast.unparse(node))
check("no print() or write() in keycloak_client.py takes the secret, the manifest, the admin credentials or a token", not leaky, str(leaky))
values = (HERE / "values.yaml.tpl").read_text()
check("the Secret name and key are the ones values.yaml.tpl reads",
      (kc.K8S_SECRET, kc.K8S_SECRET_KEY) == ("coder-oidc", "client-secret")
      and values.count("secretKeyRef: { name: coder-oidc, key: client-secret }") == 2)
deploy = (HERE / "deploy.sh").read_text()
call = [ln for ln in deploy.splitlines() if "keycloak_client.py" in ln and not ln.lstrip().startswith("#")]
check("deploy.sh runs keycloak_client.py once, with the cluster context and the namespace, stdout discarded",
      len(call) == 1 and call[0].rstrip().endswith('"$tmp/tf.json" "$CLUSTER" "$CODER_NS" >/dev/null'), str(call))
check("deploy.sh no longer writes the client secret to a file or builds coder-oidc itself",
      "oidc-secret" not in deploy and "secret generic coder-oidc" not in deploy)

print("\nkeycloak client: " + ("FAILED: " + ", ".join(failures) if failures else "all passed"))
assert not failures, f"keycloak client checks failed: {failures}"
