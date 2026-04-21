"""Frontend `/rest/*` API wrappers: session auth, folders, workflow moves.

See `claudedocs/research_n8n_frontend_api.md` for the endpoint specs this
module codifies.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from typing import Any, cast

import httpx

from n8n_cli.api.errors import ApiError, AuthError
from n8n_cli.api.transport import Transport, _extract_cookie
from n8n_cli.config import sessions


class FrontendApi:
    def __init__(self, transport: Transport) -> None:
        self.t = transport

    # --- session -------------------------------------------------------

    def login(self, email: str, password: str) -> dict[str, Any]:
        """POST /rest/login. Stores cookie + personal_project_id in the session file.

        Returns the decoded user record.
        """
        client = self.t._client  # direct — we need the Set-Cookie header
        try:
            resp = client.post(
                "/rest/login",
                json={"emailOrLdapLoginId": email, "password": password},
                headers={"content-type": "application/json", "accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"network error during login: {exc}", backend="frontend") from exc
        if resp.status_code == 401:
            raise AuthError("invalid email or password")
        if resp.status_code != 200:
            raise ApiError(
                f"frontend API {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                backend="frontend",
            )

        cookie_value = _extract_cookie(resp.headers.get("set-cookie") or "", "n8n-auth")
        if not cookie_value:
            raise AuthError("login succeeded but no n8n-auth cookie returned")
        self.t.refresh_session_cookie(f"n8n-auth={cookie_value}")

        user = cast(dict[str, Any], resp.json().get("data") or {})

        # One-extra round-trip to resolve personal project id — cached in session.
        personal_project_id: str | None = None
        try:
            pp = self.t.get("/rest/projects/personal")
            personal_project_id = (pp.get("data") or {}).get("id")
        except (ApiError, AuthError):
            personal_project_id = None

        if self.t.instance_name:
            sessions.save(
                self.t.instance_name,
                sessions.Session(
                    cookie=f"n8n-auth={cookie_value}",
                    user_id=user.get("id"),
                    personal_project_id=personal_project_id,
                ),
            )
        return user

    def logout(self) -> None:
        """POST /rest/logout. Best-effort; also wipes the local session file."""
        import contextlib

        # Server may already have killed the session — treat as success so
        # the local file still gets cleared.
        with contextlib.suppress(ApiError, AuthError):
            self.t.post("/rest/logout")
        if self.t.instance_name:
            sessions.clear(self.t.instance_name)

    def session_user(self) -> dict[str, Any] | None:
        """GET /rest/login — returns user record if authenticated, None on 401."""
        try:
            body = self.t.get("/rest/login")
        except AuthError:
            return None
        return cast(dict[str, Any], body.get("data") or {}) or None

    # --- projects ------------------------------------------------------

    def personal_project_id(self) -> str:
        """Resolve the current user's personal project id (cached in session file)."""
        if self.t.instance_name:
            sess = sessions.load(self.t.instance_name)
            if sess and sess.personal_project_id:
                return sess.personal_project_id
        body = self.t.get("/rest/projects/personal")
        pid = (body.get("data") or {}).get("id")
        if not isinstance(pid, str):
            raise ApiError("could not resolve personal project id", backend="frontend")
        # Update session if we have one.
        if self.t.instance_name:
            sess = sessions.load(self.t.instance_name)
            if sess is not None:
                sess.personal_project_id = pid
                sessions.save(self.t.instance_name, sess)
        return pid

    # --- folders -------------------------------------------------------

    def list_folders(self, project_id: str, *, take: int = 100) -> list[dict[str, Any]]:
        body = self.t.get(f"/rest/projects/{project_id}/folders", take=take)
        return cast(list[dict[str, Any]], body.get("data") or [])

    def get_folder_tree(self, project_id: str, folder_id: str) -> list[dict[str, Any]]:
        body = self.t.get(f"/rest/projects/{project_id}/folders/{folder_id}/tree")
        return cast(list[dict[str, Any]], body.get("data") or [])

    def get_folder_content(self, project_id: str, folder_id: str) -> dict[str, Any]:
        body = self.t.get(f"/rest/projects/{project_id}/folders/{folder_id}/content")
        return cast(dict[str, Any], body.get("data") or {})

    def create_folder(
        self, project_id: str, *, name: str, parent_folder_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if parent_folder_id:
            payload["parentFolderId"] = parent_folder_id
        body = self.t.post(f"/rest/projects/{project_id}/folders", json=payload)
        return cast(dict[str, Any], body.get("data") or body)

    def patch_folder(
        self,
        project_id: str,
        folder_id: str,
        *,
        name: str | None = None,
        tag_ids: list[str] | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if tag_ids is not None:
            payload["tagIds"] = tag_ids
        if not payload:
            return
        self.t.patch(f"/rest/projects/{project_id}/folders/{folder_id}", json=payload)

    def delete_folder(
        self, project_id: str, folder_id: str, *, transfer_to: str | None = None
    ) -> None:
        params: dict[str, Any] = {}
        if transfer_to:
            params["transferToFolderId"] = transfer_to
        self.t.delete(f"/rest/projects/{project_id}/folders/{folder_id}", **params)

    # --- workflow ⇄ folder -------------------------------------------

    def move_workflow(self, workflow_id: str, *, parent_folder_id: str | None) -> dict[str, Any]:
        """PATCH /rest/workflows/:id with `parentFolderId`. None → move to root.

        n8n rejects `null` — we translate None to the empty string which means
        "project root" on the frontend endpoint.
        """
        payload = {"parentFolderId": parent_folder_id if parent_folder_id else ""}
        body = self.t.patch(f"/rest/workflows/{workflow_id}", json=payload)
        return cast(dict[str, Any], body.get("data") or body)

    # --- credentials (frontend covers what public API lacks) -----

    def list_credentials(self, *, take: int = 200) -> list[dict[str, Any]]:
        """GET /rest/credentials. Response never carries `data` — safe for stdout."""
        body = self.t.get("/rest/credentials", take=take)
        return cast(list[dict[str, Any]], body.get("data") or [])

    def get_credential(self, credential_id: str) -> dict[str, Any]:
        body = self.t.get(f"/rest/credentials/{credential_id}")
        return cast(dict[str, Any], body.get("data") or {})

    def patch_credential(
        self, credential_id: str, *, name: str | None = None, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if data is not None:
            payload["data"] = data
        body = self.t.patch(f"/rest/credentials/{credential_id}", json=payload)
        return cast(dict[str, Any], body.get("data") or body)

    # --- workflow execute (manual trigger) -----------------------

    def run_workflow(
        self,
        workflow_id: str,
        *,
        full_workflow: dict[str, Any],
        trigger_name: str | None = None,
        run_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /rest/workflows/:id/run. Fires a manual execution, returns executionId.

        The UI uses the `triggerToStartFrom` shape — `startNodes` returns 500
        on current n8n. We auto-pick the first Trigger-typed node when
        `trigger_name` is None.
        """
        if trigger_name is None:
            nodes = full_workflow.get("nodes") or []
            trigger = next(
                (n for n in nodes if isinstance(n.get("type"), str) and "Trigger" in n["type"]),
                None,
            )
            if trigger is None and nodes:
                trigger = nodes[0]
            trigger_name = (trigger or {}).get("name")
        if trigger_name is None:
            raise ApiError("workflow has no nodes to trigger", backend="frontend")

        payload = {
            "workflowData": {
                "id": full_workflow.get("id"),
                "name": full_workflow.get("name"),
                "nodes": full_workflow.get("nodes") or [],
                "connections": full_workflow.get("connections") or {},
                "settings": full_workflow.get("settings") or {},
                "pinData": full_workflow.get("pinData") or {},
                "active": bool(full_workflow.get("active", False)),
            },
            "runData": run_data or {},
            "triggerToStartFrom": {"name": trigger_name},
        }
        body = self.t.post(f"/rest/workflows/{workflow_id}/run", json=payload)
        return cast(dict[str, Any], body.get("data") or body)

    def list_workflows_frontend(
        self,
        *,
        include_folders: bool = False,
        parent_folder_id: str | None = None,
        filter_json: dict[str, Any] | None = None,
        take: int = 50,
        sort_by: str = "updatedAt:desc",
    ) -> list[dict[str, Any]]:
        """Frontend `/rest/workflows` list.

        Note: the `parentFolderId` **query param** is silently ignored by n8n.
        Folder scoping must be expressed inside the `filter` JSON. We merge
        `parent_folder_id` into the filter here so callers can pass it
        naturally.
        """
        effective_filter = dict(filter_json or {})
        if parent_folder_id is not None:
            effective_filter["parentFolderId"] = parent_folder_id
        params: dict[str, Any] = {
            "includeFolders": include_folders,
            "take": take,
            "sortBy": sort_by,
        }
        if effective_filter:
            params["filter"] = _json.dumps(effective_filter)
        body = self.t.get("/rest/workflows", **params)
        return cast(list[dict[str, Any]], body.get("data") or [])

    # --- node-type catalog --------------------------------------------

    def fetch_node_types_catalog(self) -> list[dict[str, Any]]:
        """GET /types/nodes.json. Returns the full node-type catalog.

        The response is a list of node descriptor objects; each has a
        `name` (e.g. ``n8n-nodes-base.httpRequest``) and a `version`
        field that's either an int/float or a list of ints/floats.
        Multiple entries per node type may exist when n8n keeps legacy
        versions around.
        """
        # Transport treats /types/ as a non-standard path; use raw client.
        client = self.t._client
        import httpx as _httpx

        try:
            resp = client.get("/types/nodes.json")
        except _httpx.HTTPError as exc:
            raise ApiError(
                f"network error fetching node-types: {exc}", backend="frontend"
            ) from exc
        if resp.status_code != 200:
            raise ApiError(
                f"frontend /types/nodes.json returned {resp.status_code}",
                status_code=resp.status_code,
                backend="frontend",
            )
        data = resp.json()
        if not isinstance(data, list):
            raise ApiError(
                "frontend /types/nodes.json did not return a list", backend="frontend"
            )
        return cast(list[dict[str, Any]], data)


def latest_node_versions(catalog: list[dict[str, Any]]) -> dict[str, float]:
    """Collapse a node-type catalog to ``{node_type: latest_version}``.

    Handles descriptors that declare `version` as int, float, or list.
    When a node type appears multiple times, the highest version wins.
    """
    latest: dict[str, float] = {}
    for entry in catalog:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        version = entry.get("version")
        candidates: list[float] = []
        if isinstance(version, (int, float)):
            candidates.append(float(version))
        elif isinstance(version, list):
            for v in version:
                if isinstance(v, (int, float)):
                    candidates.append(float(v))
        if not candidates:
            continue
        best = max(candidates)
        prev = latest.get(name)
        if prev is None or best > prev:
            latest[name] = best
    return latest


def iter_folder_tree(trees: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (path, folder_node) across the subtrees returned by /tree.

    Useful for `folder path` and for the folder-path resolver.
    """

    def _walk(nodes: list[dict[str, Any]], prefix: str) -> Iterator[tuple[str, dict[str, Any]]]:
        for n in nodes:
            name = n.get("name", "")
            path = f"{prefix}/{name}" if prefix else name
            yield path, n
            children = n.get("children") or []
            if isinstance(children, list):
                yield from _walk(children, path)

    yield from _walk(trees, "")
