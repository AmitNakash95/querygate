"""HA / DR chart invariants (TODO.md item 56).

These render the actual Helm chart with `helm template` and assert the
high-availability guarantees the DR runbook (`deploy/HA_DR.md`) documents are
real properties of the manifests, not prose:

* zero-downtime rollouts (`updateStrategy.maxUnavailable: 0`);
* a config-checksum pod annotation, so a `helm upgrade` to config rolls every
  replica (the multi-replica-correct, zero-downtime config-reload path) and the
  checksum actually *changes* when config changes;
* the HA overlay (`values-ha.yaml`) enabling a PDB and cross-zone topology
  spread, and keeping Redis-backed concurrency on;
* the optional shared config-governance PVC being ReadWriteMany when enabled.

`helm` is optional in a dev environment, so the whole module skips cleanly when
it is not installed — CI images that ship helm exercise it for real.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "querygate"
_HA_VALUES = _CHART / "values-ha.yaml"

_helm = shutil.which("helm")
requires_helm = pytest.mark.skipif(_helm is None, reason="helm not installed")


def _template(*extra_args: str) -> list[dict]:
    """Render the chart and return the parsed manifests (non-empty docs)."""
    cmd = [_helm, "template", "qg", str(_CHART), *extra_args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _deployment(manifests: list[dict]) -> dict:
    for m in manifests:
        if m.get("kind") == "Deployment":
            return m
    raise AssertionError("no Deployment rendered")


@requires_helm
def test_chart_lints_clean_with_and_without_ha_overlay():
    for args in ([], ["-f", str(_HA_VALUES)]):
        result = subprocess.run([_helm, "lint", str(_CHART), *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


@requires_helm
def test_zero_downtime_rolling_strategy_is_default():
    dep = _deployment(_template())
    strategy = dep["spec"]["strategy"]
    assert strategy["type"] == "RollingUpdate"
    # maxUnavailable: 0 is the guarantee that capacity never drops during a roll.
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0
    assert strategy["rollingUpdate"]["maxSurge"] == 1


@requires_helm
def test_config_checksum_annotation_present_and_change_sensitive():
    base = _deployment(_template())
    ann = base["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/config" in ann, "config checksum annotation drives the reload roll"

    # Changing rendered config must change the checksum, or a `helm upgrade` to
    # policy/connections would silently NOT roll the pods (stale config on live
    # replicas — the exact multi-replica bug item 56 guards against).
    changed = _deployment(_template("--set", "config.policyYaml=default:\n  enabled: true\n"))
    changed_ann = changed["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert changed_ann != ann["checksum/config"]


@requires_helm
def test_ha_overlay_enables_pdb_and_zone_spread():
    manifests = _template("-f", str(_HA_VALUES))
    dep = _deployment(manifests)

    kinds = {m.get("kind") for m in manifests}
    assert "PodDisruptionBudget" in kinds, "HA overlay must ship a PDB"
    assert "HorizontalPodAutoscaler" in kinds, "HA overlay must enable autoscaling"

    spread = dep["spec"]["template"]["spec"]["topologySpreadConstraints"]
    topo_keys = {c["topologyKey"] for c in spread}
    assert "topology.kubernetes.io/zone" in topo_keys
    assert "kubernetes.io/hostname" in topo_keys

    # Redis-backed concurrency must stay on under multiple replicas, else the
    # concurrency cap multiplies per replica (shared-state matrix, HA_DR.md).
    env = {
        e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env.get("CONCURRENCY_BACKEND") == "redis"
    assert "CONCURRENCY_REDIS_URL" in env


@requires_helm
def test_config_governance_pvc_is_readwritemany_when_enabled():
    manifests = _template("--set", "configGovernance.enabled=true")
    pvcs = [m for m in manifests if m.get("kind") == "PersistentVolumeClaim"]
    gov = [m for m in pvcs if m["metadata"]["name"].endswith("config-governance")]
    assert gov, "enabling configGovernance must render its PVC"
    # Shared governance history across replicas requires RWX; ROX/RWO would give
    # each replica a divergent copy — the bug the HA_DR matrix calls out.
    assert gov[0]["spec"]["accessModes"] == ["ReadWriteMany"]

    dep = _deployment(manifests)
    env = {
        e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env.get("CONFIG_GOVERNANCE_DIR") == "/app/var/config_versions"


@requires_helm
def test_config_governance_pvc_absent_by_default():
    manifests = _template()
    pvcs = [
        m
        for m in manifests
        if m.get("kind") == "PersistentVolumeClaim"
        and m["metadata"]["name"].endswith("config-governance")
    ]
    assert not pvcs, "governance PVC must be opt-in (GitOps is the default path)"
