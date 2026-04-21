"""Latest-version resolution for n8n node types.

When `n8n-cli node add` doesn't get an explicit ``--type-version``, we need
a sensible default. The stock fallback of ``1`` is wrong for most nodes in
recent n8n builds — e.g. ``n8n-nodes-base.httpRequest`` v1 uses a completely
different parameter shape than v4.x and silently serves 404s.

Strategy:
  1. Built-in static map covers the nodes the CLI and its skill touch most
     often, so the sensible default is available even offline.
  2. ``FrontendApi.fetch_node_types_catalog()`` hits ``/types/nodes.json``
     on the live instance and is authoritative; we cache the distilled
     ``{type: latest_version}`` map per-instance on disk so subsequent calls
     are free.
  3. The resolver merges (2) over (1) — instance data always wins.

Cache lives at ``<config-dir>/cache/node-types-<instance>.yaml``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from n8n_cli.config.store import config_dir as config_root

if TYPE_CHECKING:
    from n8n_cli.api.frontend import FrontendApi

# Latest versions observed on n8n 1.x as of 2026-04. Keep this conservative —
# only nodes that are highly likely to be used via the CLI / skill. Unknown
# nodes fall back to the instance catalog or ``1``.
BUILTIN_LATEST: dict[str, float] = {
    "n8n-nodes-base.httpRequest": 4.2,
    "n8n-nodes-base.webhook": 2.1,
    "n8n-nodes-base.respondToWebhook": 1.1,
    "n8n-nodes-base.set": 3.4,
    "n8n-nodes-base.code": 2,
    "n8n-nodes-base.if": 2.2,
    "n8n-nodes-base.switch": 3.2,
    "n8n-nodes-base.merge": 3.2,
    "n8n-nodes-base.splitInBatches": 3,
    "n8n-nodes-base.wait": 1.1,
    "n8n-nodes-base.noOp": 1,
    "n8n-nodes-base.stickyNote": 1,
    "n8n-nodes-base.manualTrigger": 1,
    "n8n-nodes-base.scheduleTrigger": 1.2,
    "n8n-nodes-base.executeWorkflow": 1.2,
    "n8n-nodes-base.executeWorkflowTrigger": 1.1,
    "n8n-nodes-base.emailReadImap": 2,
    "n8n-nodes-base.emailSend": 2.1,
}

_CACHE_TTL_SECONDS = 24 * 3600
_PROCESS_CACHE: dict[str, dict[str, float]] = {}


def _cache_path(instance_name: str) -> Path:
    return config_root() / "cache" / f"node-types-{instance_name}.yaml"


def load_cached_map(instance_name: str) -> dict[str, float] | None:
    """Read the on-disk cached map. Returns None if missing or stale."""
    path = _cache_path(instance_name)
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    ts = raw.get("_cached_at")
    mapping = raw.get("map")
    if not isinstance(ts, (int, float)) or not isinstance(mapping, dict):
        return None
    if time.time() - float(ts) > _CACHE_TTL_SECONDS:
        return None
    return {k: float(v) for k, v in mapping.items() if isinstance(v, (int, float))}


def save_cached_map(instance_name: str, mapping: dict[str, float]) -> None:
    path = _cache_path(instance_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"_cached_at": time.time(), "map": mapping}, default_flow_style=False),
        encoding="utf-8",
    )


def resolve_latest_version(
    node_type: str,
    *,
    fapi: FrontendApi | None = None,
    instance_name: str | None = None,
    refresh: bool = False,
) -> float:
    """Return the highest known version for ``node_type``.

    Resolution order:
      1. process-level cache for ``instance_name`` (cleared on refresh),
      2. disk cache,
      3. live ``/types/nodes.json`` via ``fapi`` (if provided),
      4. ``BUILTIN_LATEST`` static map,
      5. ``1`` as last-resort fallback.
    """
    key = instance_name or "_local"
    if refresh:
        _PROCESS_CACHE.pop(key, None)

    mapping: dict[str, float] | None = _PROCESS_CACHE.get(key)
    if mapping is None and instance_name is not None:
        mapping = load_cached_map(instance_name)
        if mapping is not None:
            _PROCESS_CACHE[key] = mapping

    if mapping is None and fapi is not None:
        try:
            from n8n_cli.api.frontend import latest_node_versions

            catalog = fapi.fetch_node_types_catalog()
            mapping = latest_node_versions(catalog)
            _PROCESS_CACHE[key] = mapping
            if instance_name is not None:
                save_cached_map(instance_name, mapping)
        except Exception:
            # Silent fallback — CLI should not break because of a catalog
            # fetch hiccup. BUILTIN_LATEST covers the important cases.
            mapping = None

    if mapping and node_type in mapping:
        return mapping[node_type]
    if node_type in BUILTIN_LATEST:
        return BUILTIN_LATEST[node_type]
    return 1.0
