#!/usr/bin/env python3
"""Static guard on the trust boundary.

Parses platform/rbac/ as YAML and asserts the properties that, if broken,
silently invalidate every experiment run afterwards. Structural rather than
grep-based, so the file's own explanatory comments do not trip it.

Run by `make lint` and by CI. Needs no cluster -- the runtime equivalent is
`make verify`, which asserts the same properties against a live API server.
"""
from __future__ import annotations

import glob
import sys

import yaml

AGENT = "rca-agent"
FORBIDDEN_API_GROUP = "chaos-mesh.org"
CHAOS_NS = "chaos-mesh"

errors: list[str] = []
docs: list[dict] = []

for path in sorted(glob.glob("platform/rbac/*.yaml")):
    for doc in yaml.safe_load_all(open(path)):
        if doc:
            doc["__file"] = path
            docs.append(doc)

if not docs:
    print("  FAIL no RBAC documents found under platform/rbac/")
    sys.exit(1)


def subjects_name_agent(doc: dict) -> bool:
    return any(
        s.get("kind") == "ServiceAccount" and s.get("name") == AGENT
        for s in doc.get("subjects") or []
    )


for doc in docs:
    kind = doc.get("kind", "")
    name = doc.get("metadata", {}).get("name", "")
    ns = doc.get("metadata", {}).get("namespace")
    where = f'{doc["__file"]}:{kind}/{name}'

    # 1. The agent must never hold cluster-scoped permissions.
    if kind == "ClusterRole" and AGENT in name:
        errors.append(f"{where}: ClusterRole for the agent -- must be namespaced")
    if kind == "ClusterRoleBinding" and (AGENT in name or subjects_name_agent(doc)):
        errors.append(f"{where}: ClusterRoleBinding references the agent")

    # 2. No binding of any kind inside the chaos namespace.
    if kind in ("Role", "RoleBinding") and ns == CHAOS_NS:
        errors.append(f"{where}: {kind} in the chaos namespace -- agent must not see it")

    # 3. No rule may reference the chaos API group, or list namespaces.
    for rule in doc.get("rules") or []:
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        if FORBIDDEN_API_GROUP in groups:
            errors.append(f"{where}: rule references apiGroup {FORBIDDEN_API_GROUP}")
        if "*" in groups:
            errors.append(f"{where}: wildcard apiGroup grants chaos access implicitly")
        if "namespaces" in resources:
            errors.append(f"{where}: rule lists namespaces -- agent could discover chaos-mesh")
        if "*" in resources:
            errors.append(f"{where}: wildcard resource")
        # exec is a side channel to injected tc rules and stress processes.
        if "pods/exec" in resources:
            errors.append(f"{where}: pods/exec granted -- side channel to injected faults")

    # 4. Positive control: the agent must still be able to read logs in `app`.
if not any(
    d.get("kind") == "Role"
    and d.get("metadata", {}).get("namespace") == "app"
    and any("pods/log" in (r.get("resources") or []) for r in d.get("rules") or [])
    for d in docs
):
    errors.append(
        "platform/rbac/: agent has no pods/log access in `app` -- "
        "no evidence to reason over, which invalidates the experiment too"
    )

if errors:
    print("  TRUST BOUNDARY VIOLATION")
    for e in errors:
        print(f"    - {e}")
    sys.exit(1)

print(f"  trust boundary intact ({len(docs)} RBAC documents checked)")
