"""`n8n-cli node *` — list/get/add/patch/delete/enable/disable.

Every mutation goes through `WorkflowPatcher`: fetch workflow, mutate
in-memory, PUT back. Rename cascades through connections and pin-data.
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
from n8n_cli.core.patcher import WorkflowPatcher
from n8n_cli.output.jsonout import emit

app = typer.Typer(help="Manage workflow nodes.", no_args_is_help=True)

WorkflowOpt = Annotated[str, typer.Option("--workflow", help="Workflow ID.")]
InstanceOpt = Annotated[
    str | None, typer.Option("--instance", help="Instance name (defaults to current).")
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Log HTTP calls to stderr.")]


def _node_row(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": n.get("id"),
        "name": n.get("name"),
        "type": n.get("type"),
        "typeVersion": n.get("typeVersion"),
        "disabled": n.get("disabled", False),
        "position": n.get("position"),
    }


def _parse_set_ops(raw: list[str] | None) -> dict[str, str]:
    """`--set key=value` → dict. Value is the raw RHS; patcher parses JSON."""
    out: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise UserError(f"--set expected key=value; got {item!r}")
        k, v = item.split("=", 1)
        if not k:
            raise UserError(f"--set requires a non-empty key; got {item!r}")
        out[k] = v
    return out


@app.command("list")
def list_(
    workflow: WorkflowOpt,
    node_type: Annotated[
        str | None, typer.Option("--type", help="Filter by node type (exact match).")
    ] = None,
    disabled: Annotated[
        bool | None,
        typer.Option("--disabled/--enabled", help="Filter by disabled state."),
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """List nodes in a workflow."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        wf = PublicApi(t).get_workflow(workflow)
    rows = [_node_row(n) for n in wf.get("nodes") or []]
    if node_type is not None:
        rows = [r for r in rows if r["type"] == node_type]
    if disabled is not None:
        rows = [r for r in rows if bool(r["disabled"]) == disabled]
    emit(rows)


@app.command("get")
def get(
    workflow: WorkflowOpt,
    name: Annotated[str, typer.Option("--name", help="Node name.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Show one node (full entity)."""
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        wf = PublicApi(t).get_workflow(workflow)
    for n in wf.get("nodes") or []:
        if n.get("name") == name:
            emit(n)
            return
    raise UserError(f"node {name!r} not found in workflow {workflow}")


@app.command("add")
def add(
    workflow: WorkflowOpt,
    node_type: Annotated[str, typer.Option("--type", help="Node type, e.g. n8n-nodes-base.set.")],
    name: Annotated[str, typer.Option("--name", help="Human-readable name.")],
    params: Annotated[
        str | None, typer.Option("--params", help="JSON object for node.parameters.")
    ] = None,
    after: Annotated[
        str | None,
        typer.Option("--after", help="Place after this node; auto-connects to its output 0."),
    ] = None,
    position: Annotated[
        str | None, typer.Option("--position", help="'x,y' pair; auto-computed when omitted.")
    ] = None,
    type_version: Annotated[
        float | None,
        typer.Option(
            "--type-version",
            help="Override typeVersion. When omitted, resolves the latest known version "
            "from the instance's node-type catalog (falling back to a built-in map).",
        ),
    ] = None,
    disabled: Annotated[
        bool, typer.Option("--disabled", help="Create node in disabled state.")
    ] = False,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Create a new node. Optionally chain it after an existing node."""
    params_obj: dict[str, Any] | None = None
    if params is not None:
        try:
            params_obj = json.loads(params)
        except json.JSONDecodeError as exc:
            raise UserError(f"--params is not valid JSON: {exc}") from exc
        if not isinstance(params_obj, dict):
            raise UserError("--params must be a JSON object")

    position_list: list[float] | None = None
    if position is not None:
        parts = position.replace(" ", "").split(",")
        if len(parts) != 2:
            raise UserError("--position expects 'x,y'")
        try:
            position_list = [float(parts[0]), float(parts[1])]
        except ValueError as exc:
            raise UserError(f"--position parse error: {exc}") from exc

    name_key, inst = store.resolve_active(instance_name)
    with Transport(inst, instance_name=name_key, verbose=verbose) as t:
        effective_tv = type_version
        if effective_tv is None:
            from n8n_cli.api.frontend import FrontendApi
            from n8n_cli.core.node_types import resolve_latest_version

            effective_tv = resolve_latest_version(
                node_type, fapi=FrontendApi(t), instance_name=name_key
            )

        patcher = WorkflowPatcher(PublicApi(t), workflow)
        new = patcher.add_node(
            node_type=node_type,
            name=name,
            parameters=params_obj,
            type_version=effective_tv,
            position=position_list,
            after=after,
            disabled=disabled,
        )
        patcher.commit()
    emit(_node_row(new))


@app.command("patch")
def patch(
    workflow: WorkflowOpt,
    name: Annotated[str, typer.Option("--name", help="Current node name.")],
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Dot-notation assignment, e.g. parameters.url=https://x."),
    ] = None,
    json_: Annotated[
        str | None,
        typer.Option("--json", help="JSON merge patch applied to the node object."),
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Replace the node body with this JSON file.")
    ] = None,
    rename: Annotated[
        str | None,
        typer.Option("--rename", help="New name; cascades through connections and pinData."),
    ] = None,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Update a node. Rename cascades; dry-run validation runs before PUT."""
    if file is not None and (set_ or json_):
        raise UserError("--file is mutually exclusive with --set/--json")

    replace_body: dict[str, Any] | None = None
    if file is not None:
        try:
            replace_body = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UserError(f"cannot read --file: {exc}") from exc
        if not isinstance(replace_body, dict):
            raise UserError("--file must contain a JSON object")

    json_obj: dict[str, Any] | None = None
    if json_ is not None:
        try:
            json_obj = json.loads(json_)
        except json.JSONDecodeError as exc:
            raise UserError(f"--json is not valid JSON: {exc}") from exc
        if not isinstance(json_obj, dict):
            raise UserError("--json must be a JSON object")

    set_ops = _parse_set_ops(set_)

    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        patcher = WorkflowPatcher(PublicApi(t), workflow)
        current_name = name
        if rename:
            patcher.rename_node(current_name, rename)
            current_name = rename
        if replace_body or set_ops or json_obj:
            patcher.update_node(
                current_name,
                set_ops=set_ops or None,
                json_merge=json_obj,
                replace=replace_body,
            )
        patcher.commit()
        updated = patcher.find_node(current_name)
    emit(_node_row(updated))


@app.command("delete")
def delete(
    workflow: WorkflowOpt,
    name: Annotated[str, typer.Option("--name", help="Node name to delete.")],
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation.")] = False,
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Remove a node + all its connections + pinData."""
    if not force:
        typer.confirm(f"Delete node {name!r} from {workflow}?", abort=True)
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        patcher = WorkflowPatcher(PublicApi(t), workflow)
        patcher.delete_node(name)
        patcher.commit()
    emit({"deleted": name, "workflow": workflow})


@app.command("enable")
def enable(
    workflow: WorkflowOpt,
    name: Annotated[str, typer.Option("--name", help="Node name.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Enable (un-disable) a node."""
    _toggle(workflow, name, enabled=True, instance_name=instance_name, verbose=verbose)


@app.command("disable")
def disable(
    workflow: WorkflowOpt,
    name: Annotated[str, typer.Option("--name", help="Node name.")],
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Disable a node (execution skips it)."""
    _toggle(workflow, name, enabled=False, instance_name=instance_name, verbose=verbose)


def _toggle(
    workflow: str, name: str, *, enabled: bool, instance_name: str | None, verbose: bool
) -> None:
    _, inst = store.resolve_active(instance_name)
    with Transport(inst, verbose=verbose) as t:
        patcher = WorkflowPatcher(PublicApi(t), workflow)
        patcher.enable_node(name, enabled)
        patcher.commit()
    emit({"node": name, "disabled": not enabled})
