"""Static validation of infrastructure files with what is available without Docker,
Terraform or Prometheus binaries. This is syntax and cross-reference checking only:
it is NOT `terraform validate`, `promtool check rules`, or a Render plan.

    cd services/backend && uv run python ../../scripts/validate-infra.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import hcl2
import promql_parser
import yaml

ROOT = Path(__file__).resolve().parents[1]
problems: list[str] = []
report: dict[str, object] = {}

# --- Prometheus alert rules and Grafana queries: every expression must parse as PromQL
rules = yaml.safe_load((ROOT / "infra/observability/alerts.yml").read_text())
alerts = [r for g in rules["groups"] for r in g["rules"]]
for r in alerts:
    for key in ("alert", "expr", "labels", "annotations"):
        if key not in r:
            problems.append(f"alert {r.get('alert')}: missing {key}")
    try:
        promql_parser.parse(r["expr"])
    except Exception as exc:
        problems.append(f"alert {r['alert']}: PromQL error: {exc}")
    runbook = r.get("annotations", {}).get("runbook", "")
    if not (ROOT / runbook).is_file():
        problems.append(f"alert {r['alert']}: runbook {runbook} does not exist")
    if r.get("labels", {}).get("severity") not in ("page", "ticket"):
        problems.append(f"alert {r['alert']}: severity must be page or ticket")
dash = json.loads((ROOT / "infra/observability/grafana/dashboards/storypanel-overview.json").read_text())
exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
for e in exprs:
    try:
        promql_parser.parse(e)
    except Exception as exc:
        problems.append(f"dashboard query does not parse: {e[:60]}: {exc}")
report["prometheus"] = {"alerts": len(alerts), "dashboard_queries": len(exprs)}

# --- Terraform: HCL must parse; expected resources present; bucket is private and encrypted
tf = hcl2.loads((ROOT / "infra/terraform/main.tf").read_text())
unq = lambda x: str(x).strip('"')  # python-hcl2 keeps the quotes of labels and strings  # noqa: E731
resources = {f"{unq(k)}.{unq(n)}" for block in tf.get("resource", []) for k, v in block.items() for n in v}
for need in (
    "aws_s3_bucket.assets",
    "aws_s3_bucket_public_access_block.assets",
    "aws_s3_bucket_server_side_encryption_configuration.assets",
    "aws_s3_bucket_versioning.assets",
    "aws_s3_bucket_lifecycle_configuration.assets",
    "aws_s3_bucket_policy.assets",
    "aws_s3_bucket_cors_configuration.assets",
    "aws_iam_user_policy.app",
):
    if need not in resources:
        problems.append(f"terraform: resource {need} missing")
for name in ("staging", "production"):
    v = hcl2.loads((ROOT / f"infra/terraform/{name}.tfvars").read_text())
    if unq(v.get("environment")) != name:
        problems.append(f"terraform: {name}.tfvars sets environment={v.get('environment')}")
report["terraform"] = {"resources": sorted(resources)}

# --- Render blueprint and Compose: YAML parses; referenced files and commands exist
bp = yaml.safe_load((ROOT / "infra/render/render.yaml").read_text())
for svc in bp["services"]:
    df = svc.get("dockerfilePath")
    if df and not (ROOT / df).is_file():
        problems.append(f"render: {svc['name']} dockerfile {df} missing")
settings_src = (ROOT / "services/backend/src/storypanel/config.py").read_text()
fields = set(re.findall(r"^    ([a-z_0-9]+): ", settings_src, re.M))
env_keys = {e["key"] for g in bp["envVarGroups"] for e in g["envVars"]} | {
    e["key"] for s in bp["services"] for e in s.get("envVars", []) if "key" in e
}
for key in sorted(k for k in env_keys if k.startswith("SP_")):
    if key[3:].lower() not in fields:
        problems.append(f"render: {key} is not a Settings field")
example = set(re.findall(r"^(SP_[A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
for key in sorted(example):
    if key[3:].lower() not in fields:
        problems.append(f".env.example: {key} is not a Settings field")
compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
for name, svc in compose["services"].items():
    build = svc.get("build")
    if isinstance(build, dict) and not (ROOT / build["dockerfile"]).is_file():
        problems.append(f"compose: {name} dockerfile missing")
for df in ("backend", "web"):
    text = (ROOT / f"infra/docker/{df}.Dockerfile").read_text()
    if "USER 10001" not in text or text.count("FROM ") < 2:
        problems.append(f"{df}.Dockerfile must be multi-stage and non-root")
    for src in re.findall(r"^COPY (?!--from)(\S+) ", text, re.M):
        if not (ROOT / src).exists():
            problems.append(f"{df}.Dockerfile copies missing path {src}")
ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
report["render"] = {"services": [s["name"] for s in bp["services"]], "env_keys_checked": len(env_keys)}
report["compose"] = {"services": sorted(compose["services"])}
report["ci_jobs"] = sorted(ci["jobs"])
report["problems"] = problems
out = ROOT / "docs/evidence/infra-static-validation.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=1))
print(json.dumps({k: v for k, v in report.items() if k != "terraform"}, indent=1))
sys.exit(1 if problems else 0)
