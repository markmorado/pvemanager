"""
Authorization for /ws/tasks channel subscriptions and on-demand RRD requests.

Channel names are chosen by the client, so every subscription has to be
checked: without it any logged-in user could stream live metrics of other
tenants' guests (``instance_metrics:*``), of servers outside their workspace
(``server_metrics:*``), or the whole instance list. Subscribing to arbitrary
names also made the broadcaster poll Proxmox for whatever server/vmid the
client invented.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Callable, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import joinedload

ChannelFilter = Callable[[dict], dict]

# get_visible_server_ids reads the X-Active-Workspace header; a WebSocket
# subscription has no per-request header, so evaluate it without one.
_NO_HEADERS = SimpleNamespace(headers={})

# How long a tenant's owned-guest snapshot is reused by the list filter.
_OWNED_TTL = 30.0


def _load_user(db, user_id: int):
    from ..models import User
    return (
        db.query(User)
        .options(joinedload(User.role))
        .filter(User.id == user_id, User.is_active == True)  # noqa: E712
        .first()
    )


def _owned_pairs(db, user) -> set:
    from ..models import VMInstance
    rows = db.query(VMInstance.server_id, VMInstance.vmid).filter(
        VMInstance.owner_id == user.id,
        VMInstance.deleted_at.is_(None),
    ).all()
    return {(int(s), int(v)) for s, v in rows}


def _instances_list_filter(db_factory, user_id: int, owned: set) -> ChannelFilter:
    """Filter for ``instances_list_metrics`` payloads: keep the tenant's guests."""
    state = {"owned": owned, "at": time.monotonic()}

    def _filter(payload: dict) -> dict:
        if time.monotonic() - state["at"] > _OWNED_TTL:
            db = db_factory()
            try:
                user = _load_user(db, user_id)
                state["owned"] = _owned_pairs(db, user) if user else set()
            except Exception as exc:  # keep the last snapshot on DB hiccups
                logger.debug(f"[ws_access] owned refresh failed for user {user_id}: {exc}")
            finally:
                db.close()
                state["at"] = time.monotonic()
        data = payload.get("data") or {}
        items = [
            it for it in (data.get("items") or [])
            if (it.get("server_id"), it.get("vmid")) in state["owned"]
        ]
        return {**payload, "data": {**data, "items": items}}

    return _filter


def authorize_channel(db_factory, user_id: int, channel: str) -> Tuple[bool, Optional[ChannelFilter]]:
    """Return (allowed, payload_filter) for a channel subscription."""
    from ..rbac import PermissionEngine
    from ..api.proxmox._helpers import (
        can_view_all_instances, check_vm_access, get_visible_server_ids,
    )

    db = db_factory()
    try:
        user = _load_user(db, user_id)
        if not user:
            return False, None

        def has(perm: str) -> bool:
            return PermissionEngine.has_permission(user, perm)

        if channel == "dashboard_metrics":
            return has("dashboard:view"), None

        if channel == "instances_list_metrics":
            if not has("vm:view"):
                return False, None
            if can_view_all_instances(user):
                return True, None
            return True, _instances_list_filter(db_factory, user_id, _owned_pairs(db, user))

        if channel.startswith("server_metrics:"):
            # server_metrics:{server_id}_{node}
            sid_s, _, node = channel.split(":", 1)[1].partition("_")
            if not node or not (has("server:view") or has("node:view")):
                return False, None
            visible = get_visible_server_ids(_NO_HEADERS, db, user)
            return visible is None or int(sid_s) in visible, None

        if channel.startswith("instance_metrics:"):
            # instance_metrics:{server_id}_{vmid}_{type}_{node}
            parts = channel.split(":", 1)[1].split("_", 3)
            if len(parts) != 4 or parts[2] not in ("qemu", "lxc"):
                return False, None
            return has("vm:view") and check_vm_access(db, user, int(parts[0]), int(parts[1])), None

        return False, None
    except (ValueError, TypeError):
        return False, None
    finally:
        db.close()


def authorize_rrd_request(db_factory, user_id: int, action: str, params: dict) -> bool:
    """Whether the user may fetch RRD data for the requested node/guest."""
    from ..rbac import PermissionEngine
    from ..api.proxmox._helpers import check_vm_access, get_visible_server_ids

    db = db_factory()
    try:
        user = _load_user(db, user_id)
        if not user:
            return False
        server_id = int(params.get("server_id", 0))
        if action == "get_rrd":
            return (
                PermissionEngine.has_permission(user, "vm:view")
                and check_vm_access(db, user, server_id, int(params.get("vmid", 0)))
            )
        if action == "get_node_rrd":
            if not (PermissionEngine.has_permission(user, "server:view")
                    or PermissionEngine.has_permission(user, "node:view")):
                return False
            visible = get_visible_server_ids(_NO_HEADERS, db, user)
            return visible is None or server_id in visible
        return False
    except (ValueError, TypeError):
        return False
    finally:
        db.close()
