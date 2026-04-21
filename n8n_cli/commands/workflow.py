"""`n8n-cli workflow *` — core workflow read/list/export/import.

Phase 1 scope: list, get, structure, export, import, add.
Editing (patch/archive/publish/delete/execute/copy/move/link) lands in
Phases 3-5 via stubs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from n8n_cli.api.errors import UserError
from n8n_cli.api.public import PublicApi
from n8n_cli.api.transport import Transport
from n8n_cli.config import store
from n8n_cli.output.jsonout import emit

app = typer.Typer(help="Work with n8n workflows.", no_args_is_help=True)

InstanceOpt = Annotated[
    str | None,
    typer.Option("--instance", help="Instance name (defaults to current)."),
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Log HTTP calls to stderr.")]
HumanOpt = Annotated[bool, typer.Option("--human", help="Render human-friendly output.")]


# Editable fields allowed on PUT /workflows/{id} per the OpenAPI spec.
# Read-only fields (id, active, createdAt, updatedAt, versionId, ...) are
# dropped before we send the payload back to n8n.
_WRITABLE_WORKFLOW_FIELDS = frozenset(
    {"name", "nodes", "connections", "settings", "staticData", "pinData"}
)

# Keys the public API accepts inside `settings`. Anything else (e.g.
# `binaryMode`, returned on GET but rejected on POST/PUT) must be stripped
# or n8n answers 400 "must NOT have additional properties".
_WRITABLE_SETTINGS_FIELDS = frozenset(
    {
        "saveExecutionProgress",
        "saveManualExecutions",
        "saveDataErrorExecution",
        "saveDataSuccessExecution",
        "executionTimeout",
        "errorWorkflow",
        "timezone",
        "executionOrder",
        "callerPolicy",
        "callerIds",
        "timeSavedPerExecution",
        "availableInMCP",
    }
)


def _strip_readonly(workflow: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in workflow.items() if k in _WRITABLE_WORKFLOW_FIELDS}
    settings = out.get("settings")
    if isinstance(settings, dict):
        out["settings"] = {k: v for k, v in settings.items() if k in _WRITABLE_SETTINGS_FIELDS}
    return out


def _summary_row(wf: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": wf.get("id"),
        "name": wf.get("name"),
        "active": wf.get("active"),
        "isArchived": wf.get("isArchived", False),
        "tags": [t.get("name") for t in wf.get("tags") or []],
        "updatedAt": wf.get("updatedAt"),
    }


def _structure(wf: dict[str, Any]) -> dict[str, Any]:
    """Lightweight graph view — everything an LLM needs to navigate a workflow."""
    nodes = [
        {
            "name": n.get("name"),
            "type": n.get("type"),
            "typeVersion": n.get("typeVersion"),
            "disabled": n.get("disabled", False),
            "position": n.get("position"),
        }
        for n in wf.get("nodes") or []
    ]
    conns: list[dict[str, Any]] = []
    for src_name, buckets in (wf.get("connections") or {}).items():
        for conn_type, outputs in (buckets or {}).items():
            for out_idx, targets in enumerate(outputs or []):
                for t in targets or []:
                    conns.append(
                        {
                            "from": src_name,
                            "fromOutput": out_idx,
                            "to": t.get("node"),
                            "toInput": t.get("index", 0),
                            "type": conn_type,
                        }
                    )
    pin_nodes = sorted((wf.get("pinData") or {}).keys())
    return {
        "id": wf.get("id"),
        "name": wf.get("name"),
        "active": wf.get("active"),
        "isArchived": wf.get("isArchived", False),
        "nodes": nodes,
        "connections": conns,
        "pinnedNodes": pin_nodes,
    }


@app.command("list")
def list_(
    instance_name: InstanceOpt = None,
    active: Annotated[
        bool | None, typer.Option("--active/--inactive", help="Filter by activation state.")
    ] = None,
    archived: Annotated[
        bool, typer.Option("--archived", help="Show archived workflows too.")
    ] = False,
    tag: Annotated[str | None, typer.Option("--tag", help="Filter by tag name.")] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Filter by name substring (n8n-side).")
    ] = None,
    project: Annotated[str | None, typer.Option("--project", help="Filter by project id.")] = None,
    folder: Annotated[
        str | None, typer.Option("--folder", help="Filter to workflows in this folder id.")
    ] = None,
    folder_path: Annotated[
        str | None,
        typer.Option(
            "--folder-path", help="Filter to workflows in this folder path (frontend API)."
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max number of results.")] = 100,
    verbose: VerboseOpt = False,
    human: HumanOpt = False,
) -> None:
    """List workflows. Default: public API; folder filters switch to frontend API."""
    if folder is not None and folder_path is not None:
        raise UserError("pass either --folder or --folder-path, not both")

    _, inst = store.resolve_active(instance_name)
    results: list[dict[str, Any]] = []

    # Folder filter → frontend API (public doesn't expose parentFolderId).
    if folder is not None or folder_path is not None:
        from n8n_cli.api.frontend import FrontendApi
        from n8n_cli.core.paths import FolderPathResolver

        name_key, _inst = store.resolve_active(instance_name)
        with Transport(_inst, instance_name=name_key, verbose=verbose) as t:
            fapi = FrontendApi(t)
            pid = project or fapi.personal_project_id()
            target_folder = folder
            if folder_path is not None:
                target_folder = FolderPathResolver(fapi, pid).resolve_path(folder_path)
            filter_obj: dict[str, Any] = {}
            if not archived:
                filter_obj["isArchived"] = False
            if active is not None:
                filter_obj["active"] = active
            if name is not None:
                filter_obj["name"] = name
            if tag is not None:
                filter_obj["tags"] = [tag]
            rows = fapi.list_workflows_frontend(
                parent_folder_id=target_folder, filter_json=filter_obj or None, take=limit
            )
            # Frontend endpoint can mix folders in when includeFolders=true;
            # folders carry `workflowCount`. Drop those defensively.
            for wf in rows:
                if "workflowCount" in wf:
                    continue
                results.append(_summary_row(wf))
                if len(results) >= limit:
                    break
        emit(results, human=human, human_formatter=_list_human)
        return

    # Default path: public API.
    with Transport(inst, verbose=verbose) as t:
        api = PublicApi(t)
        for wf in api.list_workflows(
            active=active, tags=tag, name=name, project_id=project, limit=limit
        ):
            if not archived and wf.get("isArchived"):
                continue
            results.append(_summary_row(wf))
            if len(results) >= limit:
                break
    emit(results, human=human, human_formatter=_list_human)


def _list_human(payload: Any) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title="workflows")
    for col in ("id", "name", "active", "isArchived", "tags", "updatedAt"):
        table.add_column(col)
    for row in payload:
        table.add_row(
            str(row.get("id", "")),
            str(row.get("name", "")),
            str(row.get("active", "")),
            str(row.get("isArchived", "")),
            ",".join(row.get("tags") or []),
            str(row.get("updatedAt", "")),
        )
    Console().print(table)


@app.command("get")
def get(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    full: Annotated[bool, typer.Option("--full", help="Return the raw workflow JSON.")] = False,
    structure: Annotated[
        bool, typer.Option("--structure", help="Return lightweight graph view (default).")
    ] = False,
    exclude_pin_data: Annotated[
        bool, typer.Option("--exclude-pin-data", help="Drop pinData from response.")
    ] = False,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Fetch one workflow. Default: structure view; use --full for raw JSON."""
    if full and structure:
        raise UserError("--full and --structure are mutually exclusive")
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        wf = PublicApi(t).get_workflow(workflow_id, exclude_pin_data=exclude_pin_data)
    if full:
        emit(wf)
        return
    # Default & --structure behave the same — we lean AI-first.
    emit(_structure(wf))


@app.command("structure")
def structure_cmd(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Alias of `get <id> --structure`."""
    get(
        workflow_id=workflow_id,
        full=False,
        structure=True,
        exclude_pin_data=False,
        instance_name=instance_name,
        verbose=verbose,
    )


@app.command("export")
def export(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    file: Annotated[Path, typer.Option("--file", help="Output path.")],
    include_pin_data: Annotated[
        bool, typer.Option("--include-pin-data", help="Keep pinData in export (default: off).")
    ] = False,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Write workflow JSON to disk (pretty-printed, UTF-8)."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        wf = PublicApi(t).get_workflow(workflow_id, exclude_pin_data=not include_pin_data)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    emit({"exported": str(file), "id": wf.get("id"), "name": wf.get("name")})


@app.command("import")
def import_(
    file: Annotated[Path, typer.Option("--file", help="JSON file to upload.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Override workflow name before upload.")
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Create a new workflow from a JSON file (stripped of read-only fields)."""
    if not file.exists():
        raise UserError(f"file not found: {file}")
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UserError(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise UserError("workflow file must contain a JSON object at the top level")
    payload = _strip_readonly(raw)
    if name is not None:
        payload["name"] = name
    # n8n requires a `settings` object even if empty.
    payload.setdefault("settings", {})
    payload.setdefault("connections", {})
    payload.setdefault("nodes", [])
    if "name" not in payload:
        raise UserError("workflow JSON must include a `name` field (or pass --name)")
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        created = PublicApi(t).create_workflow(payload)
    emit(
        {
            "id": created.get("id"),
            "name": created.get("name"),
            "url": f"{str(inst.url).rstrip('/')}/workflow/{created.get('id')}",
        }
    )


@app.command("add")
def add(
    name: Annotated[str, typer.Option("--name", help="Workflow name.")],
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Optional JSON starter (same shape as import). Omit for an empty workflow.",
        ),
    ] = None,
    folder: Annotated[
        str | None,
        typer.Option("--folder", help="Place the workflow in this folder id (frontend API)."),
    ] = None,
    folder_path: Annotated[
        str | None,
        typer.Option("--folder-path", help="Place the workflow in this folder path ('A/B/C')."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project id (default: personal) for folder resolution."),
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Create a new workflow, optionally seeded from a file and placed in a folder."""
    if folder is not None and folder_path is not None:
        raise UserError("pass either --folder or --folder-path, not both")

    name_key, inst = store.resolve_active(instance_name)
    with Transport(inst, instance_name=name_key, verbose=verbose) as t:
        papi = PublicApi(t)
        if file is not None:
            if not file.exists():
                raise UserError(f"file not found: {file}")
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise UserError(f"not valid JSON: {exc}") from exc
            if not isinstance(raw, dict):
                raise UserError("workflow file must contain a JSON object")
            payload = _strip_readonly(raw)
            payload["name"] = name
            payload.setdefault("settings", {})
            payload.setdefault("connections", {})
            payload.setdefault("nodes", [])
        else:
            payload = {"name": name, "nodes": [], "connections": {}, "settings": {}}

        created = papi.create_workflow(payload)

        if folder is not None or folder_path is not None:
            from n8n_cli.api.frontend import FrontendApi
            from n8n_cli.core.paths import FolderPathResolver

            fapi = FrontendApi(t)
            target = folder
            if folder_path is not None:
                pid = project or fapi.personal_project_id()
                target = FolderPathResolver(fapi, pid).resolve_path(folder_path)
            fapi.move_workflow(str(created["id"]), parent_folder_id=target)

    emit(
        {
            "id": created.get("id"),
            "name": created.get("name"),
            "url": f"{str(inst.url).rstrip('/')}/workflow/{created.get('id')}",
            "folder": folder or folder_path,
        }
    )


@app.command("patch")
def patch(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set", help="Dot-notation assignment (e.g. name='...', settings.timezone=UTC)."
        ),
    ] = None,
    json_: Annotated[
        str | None,
        typer.Option("--json", help="JSON merge patch (applied to the workflow object)."),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Full-replace: PUT this file's contents (read-only fields stripped).",
        ),
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Update workflow metadata (name, settings) or full-replace from a file."""
    # We can't import WorkflowPatcher at module top without a circular import
    # risk since patcher imports from output/ — and patcher touches api.public.
    # Import here is cheap and keeps layering clean.
    from n8n_cli.core.dotset import apply_json_merge as _json_merge
    from n8n_cli.core.patcher import WorkflowPatcher

    if file is not None and (set_ or json_):
        raise UserError("--file is mutually exclusive with --set/--json")

    set_ops: dict[str, str] = {}
    for item in set_ or []:
        if "=" not in item:
            raise UserError(f"--set expected key=value; got {item!r}")
        k, v = item.split("=", 1)
        set_ops[k] = v

    json_obj: dict[str, Any] | None = None
    if json_ is not None:
        try:
            json_obj = json.loads(json_)
        except json.JSONDecodeError as exc:
            raise UserError(f"--json is not valid JSON: {exc}") from exc
        if not isinstance(json_obj, dict):
            raise UserError("--json must be a JSON object")

    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        api = PublicApi(t)
        if file is not None:
            # Full replace path — reuse import_'s strip logic then PUT.
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise UserError(f"cannot read --file: {exc}") from exc
            if not isinstance(raw, dict):
                raise UserError("--file must contain a JSON object")
            payload = _strip_readonly(raw)
            payload.setdefault("nodes", [])
            payload.setdefault("connections", {})
            payload.setdefault("settings", {})
            if "name" not in payload:
                raise UserError("workflow JSON must include a `name` field")
            result = api.update_workflow(workflow_id, payload)
            emit({"id": result.get("id"), "name": result.get("name")})
            return

        patcher = WorkflowPatcher(api, workflow_id)
        name_override = set_ops.pop("name", None)
        settings_set: dict[str, str] = {
            k[len("settings.") :]: v for k, v in set_ops.items() if k.startswith("settings.")
        }
        other_ops = {k: v for k, v in set_ops.items() if not k.startswith("settings.")}
        if other_ops:
            raise UserError(
                f"--set on workflow currently supports only `name=...` and `settings.*=...`; "
                f"rejected keys: {sorted(other_ops)}"
            )
        if name_override is not None:
            # Trim surrounding quotes if the user wrote --set name='"..."'.
            if name_override.startswith('"') and name_override.endswith('"'):
                name_override = name_override[1:-1]
            patcher.set_workflow_fields(name=name_override)
        if settings_set:
            patcher.set_workflow_fields(settings_set=settings_set)
        if json_obj:
            _json_merge(patcher.wf, json_obj)
            patcher._dirty = True
        result = patcher.commit()
    emit({"id": result.get("id"), "name": result.get("name")})


@app.command("archive")
def archive(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Soft-delete: set isArchived=true."""
    _set_archived(workflow_id, True, instance_name=instance_name, verbose=verbose)


@app.command("unarchive")
def unarchive(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Undo archive: set isArchived=false."""
    _set_archived(workflow_id, False, instance_name=instance_name, verbose=verbose)


def _set_archived(
    workflow_id: str, value: bool, *, instance_name: str | None, verbose: bool
) -> None:
    from n8n_cli.core.patcher import WorkflowPatcher

    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        patcher = WorkflowPatcher(PublicApi(t), workflow_id)
        patcher.set_archived(value)
        patcher.commit()
    emit({"id": workflow_id, "isArchived": value})


@app.command("publish")
def publish(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Activate a workflow (maps to POST /workflows/:id/activate)."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        PublicApi(t).activate_workflow(workflow_id)
    emit({"id": workflow_id, "active": True})


@app.command("unpublish")
def unpublish(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Deactivate a workflow (maps to POST /workflows/:id/deactivate)."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        PublicApi(t).deactivate_workflow(workflow_id)
    emit({"id": workflow_id, "active": False})


@app.command("delete")
def delete(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Required. Our convention is `archive`; hard-delete is destructive.",
        ),
    ] = False,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Hard-delete (DESTRUCTIVE). Prefer `workflow archive` unless you really mean this."""
    if not force:
        raise UserError(
            "refusing to hard-delete without --force",
            hint="workflow archive <id> is usually what you want.",
        )
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        PublicApi(t).delete_workflow(workflow_id)
    emit({"deleted": workflow_id})


@app.command("move")
def move(
    workflow_id: Annotated[str | None, typer.Argument(help="Workflow ID (or pass --id).")] = None,
    id_opt: Annotated[
        str | None, typer.Option("--id", help="Workflow ID (alternative to positional arg).")
    ] = None,
    folder: Annotated[str | None, typer.Option("--folder", help="Destination folder id.")] = None,
    folder_path: Annotated[
        str | None, typer.Option("--folder-path", help="Destination folder path ('A/B/C').")
    ] = None,
    to_folder: Annotated[
        str | None, typer.Option("--to-folder", help="Alias for --folder.")
    ] = None,
    to_path: Annotated[
        str | None, typer.Option("--to-path", help="Alias for --folder-path.")
    ] = None,
    to_root: Annotated[
        bool, typer.Option("--to-root", help="Move workflow to the project root.")
    ] = False,
    project: Annotated[
        str | None, typer.Option("--project", help="Project id (default: personal).")
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Move a workflow between folders (frontend API).

    Exactly one of --folder/--to-folder, --folder-path/--to-path, or --to-root must be given.
    Accepts workflow id either as positional arg or via --id.
    """
    from n8n_cli.api.frontend import FrontendApi
    from n8n_cli.core.paths import FolderPathResolver

    # Reconcile positional vs --id.
    wf_id = workflow_id or id_opt
    if wf_id is None:
        raise UserError("workflow id is required (positional or --id)")
    if workflow_id and id_opt and workflow_id != id_opt:
        raise UserError("positional workflow id conflicts with --id")
    workflow_id = wf_id

    # Reconcile aliases.
    folder = folder or to_folder
    folder_path = folder_path or to_path

    chosen = [x for x in (folder, folder_path, to_root or None) if x]
    if len(chosen) != 1:
        raise UserError(
            "exactly one of --folder/--to-folder, --folder-path/--to-path, --to-root is required"
        )

    name, inst = store.resolve_active(instance_name)
    with Transport(inst, instance_name=name, verbose=verbose) as t:
        fapi = FrontendApi(t)
        pid = project or fapi.personal_project_id()
        target: str | None
        if to_root:
            target = None
        elif folder is not None:
            target = folder
        else:
            target = FolderPathResolver(fapi, pid).resolve_path(folder_path or "")
        fapi.move_workflow(workflow_id, parent_folder_id=target)

    emit(
        {
            "id": workflow_id,
            "moved_to": "<root>" if target is None else target,
        }
    )


@app.command("execute")
def execute(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID.")],
    wait: Annotated[
        bool, typer.Option("--wait", help="Poll for completion before returning.")
    ] = False,
    timeout: Annotated[int, typer.Option("--timeout", help="Seconds to wait (with --wait).")] = 60,
    trigger: Annotated[
        str | None,
        typer.Option("--trigger", help="Name of the trigger node to start from (auto-detected)."),
    ] = None,
    input_: Annotated[
        str | None,
        typer.Option(
            "--input",
            help="JSON object for runData (per-trigger pinned items), e.g. '{\"Start\": [{...}]}'.",
        ),
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Fire a manual execution via the frontend API. Optional --wait polls for completion."""
    import time

    from n8n_cli.api.frontend import FrontendApi

    run_data: dict[str, Any] | None = None
    if input_ is not None:
        try:
            parsed = json.loads(input_)
        except json.JSONDecodeError as exc:
            raise UserError(f"--input is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise UserError("--input must be a JSON object keyed by trigger node name")
        run_data = parsed

    name, inst = store.resolve_active(instance_name)
    with Transport(inst, instance_name=name, verbose=verbose) as t:
        papi = PublicApi(t)
        fapi = FrontendApi(t)
        full = papi.get_workflow(workflow_id)
        result = fapi.run_workflow(
            workflow_id, full_workflow=full, trigger_name=trigger, run_data=run_data
        )
        execution_id = result.get("executionId")
        if execution_id is None:
            raise UserError(f"n8n did not return an executionId; got {result!r}")

        if not wait:
            emit({"workflow_id": workflow_id, "execution_id": execution_id, "status": "running"})
            return

        deadline = time.monotonic() + timeout
        final_status: str | None = None
        last_body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_body = papi.get_execution(execution_id)
            final_status = last_body.get("status")
            if final_status in {"success", "error", "crashed", "canceled"}:
                break
            time.sleep(1.0)
    emit(
        {
            "workflow_id": workflow_id,
            "execution_id": execution_id,
            "status": final_status or "timeout",
            "finished": last_body.get("finished", False),
            "startedAt": last_body.get("startedAt"),
            "stoppedAt": last_body.get("stoppedAt"),
        }
    )


@app.command("copy")
def copy(
    workflow_id: Annotated[str, typer.Argument(help="Source workflow ID.")],
    from_instance: Annotated[str, typer.Option("--from", help="Source instance name.")],
    to_instance: Annotated[str, typer.Option("--to", help="Destination instance name.")],
    new_name: Annotated[
        str | None, typer.Option("--name", help="Override the name on the destination.")
    ] = None,
    folder_path: Annotated[
        str | None,
        typer.Option("--folder-path", help="Place the copy in this folder on the destination."),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """GET workflow from --from, POST to --to. Optionally rename + place in a folder."""
    from n8n_cli.api.frontend import FrontendApi
    from n8n_cli.core.paths import FolderPathResolver

    src_inst = store.get_instance(from_instance)
    dst_inst = store.get_instance(to_instance)
    with Transport(src_inst, instance_name=from_instance, verbose=verbose) as src_t:
        full = PublicApi(src_t).get_workflow(workflow_id)

    payload = _strip_readonly(full)
    if new_name:
        payload["name"] = new_name
    payload.setdefault("settings", {})
    payload.setdefault("connections", {})
    payload.setdefault("nodes", [])

    with Transport(dst_inst, instance_name=to_instance, verbose=verbose) as dst_t:
        created = PublicApi(dst_t).create_workflow(payload)
        if folder_path:
            fapi = FrontendApi(dst_t)
            pid = fapi.personal_project_id()
            target = FolderPathResolver(fapi, pid).resolve_path(folder_path)
            fapi.move_workflow(created["id"], parent_folder_id=target)
    emit(
        {
            "source": {"instance": from_instance, "id": workflow_id},
            "target": {
                "instance": to_instance,
                "id": created.get("id"),
                "name": created.get("name"),
                "url": f"{str(dst_inst.url).rstrip('/')}/workflow/{created.get('id')}",
            },
        }
    )


@app.command("link")
def link(
    workflow_id: Annotated[str, typer.Option("--id", help="Workflow ID.")],
    project: Annotated[str, typer.Option("--project", help="Destination project ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Transfer a workflow to another project (public API uses PUT /transfer)."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        t.put(
            f"/api/v1/workflows/{workflow_id}/transfer",
            json={"destinationProjectId": project},
        )
    emit({"id": workflow_id, "project": project, "linked": True})


@app.command("unlink")
def unlink(
    workflow_id: Annotated[str, typer.Option("--id", help="Workflow ID.")],
    project: Annotated[str, typer.Option("--project", help="Project to detach from.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Detach a workflow from a project.

    Public n8n supports a single `projectId` per workflow — unlinking is only
    meaningful on multi-project licenses. We surface a clear gated error on
    community installs.
    """
    from n8n_cli.api.errors import CapabilityError

    _ = instance_name, workflow_id, project, verbose  # silence unused-arg warnings
    raise CapabilityError(
        "workflow.unlink requires multi-project membership (enterprise license).",
        hint="on community/single-team use `workflow link --id ... --project ...` to transfer.",
    )


@app.command("projects")
def projects(
    workflow_id: Annotated[str, typer.Option("--id", help="Workflow ID.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """List projects a workflow belongs to. On single-team, returns the home project only."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        wf = PublicApi(t).get_workflow(workflow_id)
    # Public workflow object may include `shared[]` with projects. Fall back
    # to frontend-only fields if present.
    shared = wf.get("shared") or []
    project_ids: list[str] = []
    for entry in shared:
        pid = (entry.get("project") or {}).get("id") if isinstance(entry, dict) else None
        if isinstance(pid, str):
            project_ids.append(pid)
    emit({"id": workflow_id, "projects": project_ids})
