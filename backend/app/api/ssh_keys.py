"""
SSH Keys management API.

Each user owns a personal library of SSH keys (public + optionally encrypted
private key). Keys can be referenced by id when deploying a VM/LXC instance.

* Regular users can manage only their own keys and may pick only their own
  keys when deploying.
* Admins (`users.manage` permission) can manage SSH keys of any user and may
  pick any user's key (typically their own + the new instance owner's) when
  deploying through the admin flow.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_client_ip, get_current_user, PermissionChecker
from ..db import get_db
from ..logging_service import LoggingService
from ..models import User, UserSSHKey

router = APIRouter()


# ==================== Schemas ====================


class SSHKeyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    public_key: str = Field(..., min_length=20, max_length=10000)
    private_key: Optional[str] = Field(None, max_length=20000)
    comment: Optional[str] = Field(None, max_length=255)


class SSHKeyCreate(SSHKeyBase):
    pass


class SSHKeyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    public_key: Optional[str] = Field(None, min_length=20, max_length=10000)
    private_key: Optional[str] = Field(None, max_length=20000)
    comment: Optional[str] = Field(None, max_length=255)


class SSHKeyResponse(BaseModel):
    id: int
    user_id: int
    name: str
    public_key: str
    fingerprint: Optional[str] = None
    comment: Optional[str] = None
    has_private_key: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ==================== Helpers ====================


def _normalize_public_key(value: str) -> str:
    value = (value or "").strip()
    # Keys are written verbatim into authorized_keys / cloud-init sshkeys: an
    # embedded newline would add an extra, unvalidated entry (e.g. with
    # command=/from= options) next to the one shown in the UI.
    if "\n" in value or "\r" in value:
        raise HTTPException(status_code=400, detail="SSH public key must be a single line")
    return value


def _compute_fingerprint(public_key: str) -> Optional[str]:
    """SHA256 fingerprint in OpenSSH format (SHA256:base64...)."""
    try:
        parts = public_key.strip().split()
        if len(parts) < 2:
            return None
        key_b64 = parts[1]
        raw = base64.b64decode(key_b64)
        digest = hashlib.sha256(raw).digest()
        b64 = base64.b64encode(digest).decode().rstrip("=")
        return f"SHA256:{b64}"
    except Exception:
        return None


def _to_response(key: UserSSHKey) -> dict:
    return {
        "id": key.id,
        "user_id": key.user_id,
        "name": key.name,
        "public_key": key.public_key,
        "fingerprint": key.fingerprint,
        "comment": key.comment,
        "has_private_key": bool(key.private_key),
        "created_at": key.created_at,
    }


def _ensure_target_user(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ==================== Self-service endpoints ====================


@router.get("/api/ssh-keys/my", response_model=List[SSHKeyResponse])
async def list_my_ssh_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List current user's SSH keys."""
    keys = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.user_id == current_user.id)
        .order_by(UserSSHKey.created_at.desc())
        .all()
    )
    return [_to_response(k) for k in keys]


@router.post("/api/ssh-keys/my", response_model=SSHKeyResponse, status_code=201)
async def create_my_ssh_key(
    data: SSHKeyCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add an SSH key to the current user's library."""
    return _create_key_for_user(db, request, current_user, current_user.id, data)


@router.put("/api/ssh-keys/my/{key_id}", response_model=SSHKeyResponse)
async def update_my_ssh_key(
    key_id: int,
    data: SSHKeyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _update_key(db, request, current_user, current_user.id, key_id, data)


@router.delete("/api/ssh-keys/my/{key_id}")
async def delete_my_ssh_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _delete_key(db, request, current_user, current_user.id, key_id)


@router.get("/api/ssh-keys/my/{key_id}/private")
async def download_my_private_key(
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _read_private_key(db, current_user.id, key_id)


# ==================== Admin endpoints (per user) ====================


@router.get("/api/ssh-keys/users/{user_id}", response_model=List[SSHKeyResponse])
async def list_user_ssh_keys(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:manage")),
):
    """Admin: list a user's SSH keys."""
    _ensure_target_user(db, user_id)
    keys = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.user_id == user_id)
        .order_by(UserSSHKey.created_at.desc())
        .all()
    )
    return [_to_response(k) for k in keys]


@router.post(
    "/api/ssh-keys/users/{user_id}",
    response_model=SSHKeyResponse,
    status_code=201,
)
async def admin_create_user_ssh_key(
    user_id: int,
    data: SSHKeyCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:manage")),
):
    target = _ensure_target_user(db, user_id)
    return _create_key_for_user(db, request, current_user, target.id, data)


@router.put(
    "/api/ssh-keys/users/{user_id}/{key_id}",
    response_model=SSHKeyResponse,
)
async def admin_update_user_ssh_key(
    user_id: int,
    key_id: int,
    data: SSHKeyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:manage")),
):
    _ensure_target_user(db, user_id)
    return _update_key(db, request, current_user, user_id, key_id, data)


@router.delete("/api/ssh-keys/users/{user_id}/{key_id}")
async def admin_delete_user_ssh_key(
    user_id: int,
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:manage")),
):
    _ensure_target_user(db, user_id)
    return _delete_key(db, request, current_user, user_id, key_id)


@router.get("/api/ssh-keys/users/{user_id}/{key_id}/private")
async def admin_download_user_private_key(
    user_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:manage")),
):
    _ensure_target_user(db, user_id)
    return _read_private_key(db, user_id, key_id)


# ==================== Internal helpers ====================


def _create_key_for_user(
    db: Session,
    request: Request,
    current_user: User,
    target_user_id: int,
    data: SSHKeyCreate,
) -> dict:
    public_key = _normalize_public_key(data.public_key)
    if not public_key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise HTTPException(
            status_code=400,
            detail="Invalid SSH public key format",
        )

    fingerprint = _compute_fingerprint(public_key)
    private_key = data.private_key.strip() if data.private_key else None
    if private_key and "PRIVATE KEY" not in private_key:
        raise HTTPException(
            status_code=400,
            detail="Invalid SSH private key format (PEM expected)",
        )

    key = UserSSHKey(
        user_id=target_user_id,
        name=data.name.strip(),
        public_key=public_key,
        private_key=private_key,
        fingerprint=fingerprint,
        comment=(data.comment or None),
    )
    db.add(key)
    db.commit()
    db.refresh(key)

    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="ssh_key_added",
        message=f"SSH key '{key.name}' added to user_id={target_user_id} by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="ssh_key",
        resource_id=str(key.id),
        resource_name=key.name,
    )

    return _to_response(key)


def _update_key(
    db: Session,
    request: Request,
    current_user: User,
    target_user_id: int,
    key_id: int,
    data: SSHKeyUpdate,
) -> dict:
    key = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.id == key_id, UserSSHKey.user_id == target_user_id)
        .first()
    )
    if not key:
        raise HTTPException(status_code=404, detail="SSH key not found")

    if data.name is not None:
        key.name = data.name.strip()
    if data.public_key is not None:
        public_key = _normalize_public_key(data.public_key)
        if not public_key.startswith(("ssh-", "ecdsa-", "sk-")):
            raise HTTPException(status_code=400, detail="Invalid SSH public key format")
        key.public_key = public_key
        key.fingerprint = _compute_fingerprint(public_key)
    if data.private_key is not None:
        pk = data.private_key.strip()
        if pk:
            if "PRIVATE KEY" not in pk:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid SSH private key format (PEM expected)",
                )
            key.private_key = pk
        else:
            key.private_key = None
    if data.comment is not None:
        key.comment = data.comment or None

    db.commit()
    db.refresh(key)

    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="ssh_key_updated",
        message=f"SSH key #{key.id} ('{key.name}') updated by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="ssh_key",
        resource_id=str(key.id),
        resource_name=key.name,
    )

    return _to_response(key)


def _delete_key(
    db: Session,
    request: Request,
    current_user: User,
    target_user_id: int,
    key_id: int,
) -> dict:
    key = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.id == key_id, UserSSHKey.user_id == target_user_id)
        .first()
    )
    if not key:
        raise HTTPException(status_code=404, detail="SSH key not found")

    name = key.name
    db.delete(key)
    db.commit()

    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="ssh_key_deleted",
        message=f"SSH key '{name}' (id={key_id}) deleted by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="ssh_key",
        resource_id=str(key_id),
        resource_name=name,
    )

    return {"success": True, "message": "SSH key deleted"}


def _read_private_key(db: Session, target_user_id: int, key_id: int) -> dict:
    key = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.id == key_id, UserSSHKey.user_id == target_user_id)
        .first()
    )
    if not key:
        raise HTTPException(status_code=404, detail="SSH key not found")
    if not key.private_key:
        raise HTTPException(status_code=404, detail="No private key stored for this entry")
    return {
        "id": key.id,
        "name": key.name,
        "private_key": key.private_key,
    }


# ==================== Internal API (used by deploy flow) ====================


def resolve_ssh_keys_for_deploy(
    db: Session,
    requester: User,
    key_ids: List[int],
    instance_owner_id: Optional[int] = None,
) -> str:
    """Resolve a list of SSH key ids into a newline-joined block of public keys.

    Authorization rules:
    * Regular users may use only their own keys.
    * Admins (`users.manage` permission) may use their own keys and the keys
      of the designated `instance_owner_id` user.
    """
    if not key_ids:
        return ""

    allowed_owners = {requester.id}
    is_admin = requester.has_permission("user:manage") or requester.is_admin
    if is_admin and instance_owner_id:
        allowed_owners.add(instance_owner_id)

    keys = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.id.in_(key_ids))
        .all()
    )
    if len(keys) != len(set(key_ids)):
        raise HTTPException(status_code=404, detail="One or more SSH keys not found")

    for k in keys:
        if k.user_id not in allowed_owners:
            logger.warning(
                f"User {requester.username} tried to use ssh key #{k.id} "
                f"belonging to user_id={k.user_id}"
            )
            raise HTTPException(
                status_code=403,
                detail="You are not allowed to use one of the selected SSH keys",
            )

    return "\n".join(k.public_key.strip() for k in keys if k.public_key).strip()


def build_deploy_ssh_keys(
    db: Session,
    requester: User,
    instance_owner_id: int,
    key_ids: Optional[List[int]] = None,
    raw_keys: Optional[str] = None,
) -> str:
    """Собрать итоговый блок public-ключей для cloud-init / LXC.

    Объединяет вставленные вручную ключи, выбранные из библиотеки ключи
    (с проверкой прав) и профильный ключ **владельца** инстанса.

    Профильный ключ берётся именно у владельца, а не у того, кто нажал
    «Создать»: иначе VM, созданная админом для пользователя, получала ключ
    админа, а ключ самого пользователя на неё не попадал.
    """
    parts: List[str] = []

    if raw_keys:
        parts.append(raw_keys)

    if key_ids:
        parts.append(
            resolve_ssh_keys_for_deploy(db, requester, key_ids, instance_owner_id)
        )

    owner = (
        requester
        if instance_owner_id == requester.id
        else db.query(User).filter(User.id == instance_owner_id).first()
    )
    if owner and owner.ssh_public_key:
        parts.append(owner.ssh_public_key)

    seen: set = set()
    uniq: List[str] = []
    for block in parts:
        for line in (block or "").splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                uniq.append(line)
    return "\n".join(uniq)
