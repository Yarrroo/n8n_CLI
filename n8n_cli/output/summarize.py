"""Execution-data summarizer — the core AI-productivity feature.

Turns a raw list of n8n node items (potentially multi-megabyte) into a
compact dict that fits under a configurable byte budget (default 1 KB):

    {
      "item_count":       <int>,
      "total_size_bytes": <int>,
      "schema":           <inferred shape>,
      "sample":           [<first item>, ...],
      "truncated":        <bool>,
    }

Six flags control behavior:
  - sample: N        → take N sample items (default 1)
  - head: N          → first N items (overrides sample when set)
  - path: jsonpath   → extract matching subtree instead of summarizing
  - schema_only      → no sample, just item_count + schema
  - full             → pass-through (escape hatch)
  - max_bytes        → budget; sample is trimmed (then dropped) to fit

Binary payloads (`binary: {key: {data: "<base64>", mimeType, fileName,
fileSize, ...}}`) are replaced with metadata-only descriptors before any
size math happens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse

from n8n_cli.output.schema_infer import infer_schema

_DEFAULT_MAX_BYTES = 1024
_MAX_STRING_LEN = 200  # truncate long strings inside sample items
_BINARY_MARKER_KEY = "_binary"


@dataclass
class SummarizeOptions:
    sample: int = 1
    head: int | None = None
    path: str | None = None
    schema_only: bool = False
    full: bool = False
    max_bytes: int = _DEFAULT_MAX_BYTES


@dataclass
class Summary:
    item_count: int
    total_size_bytes: int
    schema: Any
    sample: list[Any] = field(default_factory=list)
    truncated: bool = False
    # Set when `--full` or `--path` bypass the normal structure.
    raw: Any = None
    extracted: Any = None

    def to_dict(self) -> dict[str, Any]:
        if self.raw is not None:
            return {"full": self.raw}
        if self.extracted is not None:
            return {
                "item_count": self.item_count,
                "total_size_bytes": self.total_size_bytes,
                "extracted": self.extracted,
            }
        return {
            "item_count": self.item_count,
            "total_size_bytes": self.total_size_bytes,
            "schema": self.schema,
            "sample": self.sample,
            "truncated": self.truncated,
        }


def summarize_items(items: list[Any], opts: SummarizeOptions | None = None) -> dict[str, Any]:
    """Summarize a list of items according to `opts` (defaults if None)."""
    opts = opts or SummarizeOptions()

    # Escape hatch: caller wants the raw bytes.
    if opts.full:
        return Summary(
            item_count=len(items), total_size_bytes=_byte_size(items), schema=None, raw=items
        ).to_dict()

    total_bytes = _byte_size(items)

    # Power path: extract a JSONPath subtree and return it verbatim.
    if opts.path:
        expr = jsonpath_parse(opts.path)
        matches = [m.value for m in expr.find(items)]
        extracted: Any = matches[0] if len(matches) == 1 else matches
        return Summary(
            item_count=len(items),
            total_size_bytes=total_bytes,
            schema=None,
            extracted=extracted,
        ).to_dict()

    schema = infer_schema(items)

    if opts.schema_only or len(items) == 0:
        return Summary(
            item_count=len(items),
            total_size_bytes=total_bytes,
            schema=schema,
            sample=[],
            truncated=len(items) > 0,
        ).to_dict()

    # Sample size: --head overrides --sample.
    n_sample = opts.head if opts.head is not None else opts.sample
    n_sample = max(0, min(n_sample, len(items)))
    raw_sample = items[:n_sample]

    # Clean binary + truncate long strings in-place on a deep copy.
    cleaned_sample = [_sanitize(x) for x in raw_sample]

    summary = Summary(
        item_count=len(items),
        total_size_bytes=total_bytes,
        schema=schema,
        sample=cleaned_sample,
        truncated=len(items) > n_sample,
    )

    # Enforce byte budget: if still too big, progressively drop sample items
    # until we fit. Schema is cheap and stays.
    _enforce_budget(summary, opts.max_bytes)
    return summary.to_dict()


# --- helpers ---


def _byte_size(obj: Any) -> int:
    """Approximate JSON byte size — fast enough for multi-MB payloads."""
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _sanitize(value: Any) -> Any:
    """Recursively: replace binary blobs with metadata, truncate long strings."""
    if isinstance(value, dict):
        # n8n item shape: {"json": {...}, "binary": {...}, "pairedItem": ...}
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "binary" and isinstance(v, dict):
                out[k] = {bk: _binary_meta(bv) for bk, bv in v.items()}
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(value, list):
        return [_sanitize(x) for x in value]
    if isinstance(value, str) and len(value) > _MAX_STRING_LEN:
        return value[:_MAX_STRING_LEN] + f"…({len(value) - _MAX_STRING_LEN} more chars)"
    return value


def _binary_meta(blob: Any) -> dict[str, Any]:
    """Reduce a binary payload to metadata — never echo base64."""
    if not isinstance(blob, dict):
        return {_BINARY_MARKER_KEY: True}
    meta = {
        "mime_type": blob.get("mimeType"),
        "file_name": blob.get("fileName"),
        "size_bytes": blob.get("fileSize"),
    }
    # Preserve any extra descriptive fields but NEVER the `data` key.
    for k, v in blob.items():
        if k in ("data", "mimeType", "fileName", "fileSize"):
            continue
        meta[k] = v
    meta[_BINARY_MARKER_KEY] = True
    return meta


def _enforce_budget(summary: Summary, max_bytes: int) -> None:
    """Trim `summary.sample`, then collapse `summary.schema` if still over budget.

    Strategy:
      1. Drop sample items one-by-one (schema is usually cheap, sample is big).
      2. If still over budget after sample is empty, replace the schema with
         a placeholder that preserves top-level field names but hides leaf
         types (or drops schema entirely if top-level itself is massive).
    """
    while _byte_size(summary.to_dict()) > max_bytes and summary.sample:
        summary.sample.pop()
        summary.truncated = True

    if _byte_size(summary.to_dict()) <= max_bytes:
        return

    # Sample is empty and we're still over budget → schema is the problem.
    # This happens on pathologically heterogeneous payloads (e.g. 22 deeply
    # nested HTTP responses with varying shapes). The CLI must not bleed
    # megabytes into the caller's context, so we hard-collapse the schema.
    schema_bytes = _byte_size(summary.schema)
    summary.schema = _collapse_schema(summary.schema, schema_bytes)
    summary.truncated = True


def _collapse_schema(schema: Any, original_bytes: int) -> Any:
    """Reduce a huge schema to a compact placeholder.

    Preserves top-level field names where possible so the caller still
    knows what the payload looked like, and always emits a marker with
    the original size so downstream code can note it needs ``--path`` or
    ``--head`` to inspect further.
    """
    marker = {
        "_schema_elided": True,
        "_original_bytes": original_bytes,
        "hint": "schema too large to show; use --path or --head 1 to inspect",
    }
    if isinstance(schema, dict) and "oneOf" not in schema:
        # Keep top-level keys only, replace leaves with "…".
        top: dict[str, Any] = dict.fromkeys(list(schema.keys())[:20], "…")
        if len(schema) > 20:
            top["_more_keys"] = f"{len(schema) - 20} additional keys truncated"
        return {**marker, "top_level_keys": top}
    return marker
