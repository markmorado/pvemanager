"""
API endpoints for OS Templates management
Allows creating VM templates, groups, and deploying VMs from templates
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger
from typing import List, Optional

from ..db import get_db, SessionLocal
from ..models import ProxmoxServer, OSTemplateGroup, OSTemplate, VMInstance, User, DeployTask
from ..schemas import (
    OSTemplateGroupCreate, OSTemplateGroupUpdate, OSTemplateGroupResponse,
    OSTemplateCreate, OSTemplateUpdate, OSTemplateResponse, OSTemplateWithGroup,
    VMDeployRequest, VMDeployResponse
)
from ..proxmox import ProxmoxClient
from ..api.proxmox import get_next_vmid, save_vm_instance, get_vm_instance
from ..api.proxmox._helpers import enforce_server_visibility, get_visible_server_ids, _NO_WORKSPACE_HEADER
from ..auth import get_current_user, PermissionChecker
from ..ipam_service import IPAMService
from ..logging_service import LoggingService
from ..websocket_manager import broadcast_task_update

router = APIRouter()

# Thread pool for blocking deploy operations
_deploy_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deploy")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Deploy Task response schema ──────────────────────────────────────────────

class DeployTaskStartResponse(BaseModel):
    task_id: int
    status: str
    name: str


class DeployTaskStatusResponse(BaseModel):
    id: int
    status: str
    step: Optional[str]
    progress: int
    name: str
    vmid: Optional[int]
    node: Optional[str]
    error_message: Optional[str]
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    kind: Optional[str] = 'deploy'


# ==================== Template Groups CRUD ====================

@router.get("/api/groups", response_model=List[OSTemplateGroupResponse])
def list_template_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """Получить список всех групп шаблонов"""
    groups = db.query(OSTemplateGroup).order_by(OSTemplateGroup.sort_order).all()
    return groups


@router.post("/api/groups", response_model=OSTemplateGroupResponse, status_code=status.HTTP_201_CREATED)
def create_template_group(
    group_data: OSTemplateGroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Создать новую группу шаблонов"""
    # Check if group with same name exists
    existing = db.query(OSTemplateGroup).filter(OSTemplateGroup.name == group_data.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Группа с именем '{group_data.name}' уже существует"
        )
    
    group = OSTemplateGroup(**group_data.model_dump())
    db.add(group)
    db.commit()
    db.refresh(group)
    
    logger.info(f"User {current_user.username} created template group: {group.name}")
    return group


@router.get("/api/groups/{group_id}", response_model=OSTemplateGroupResponse)
def get_template_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """Получить группу шаблонов по ID"""
    group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа шаблонов не найдена")
    return group


@router.put("/api/groups/{group_id}", response_model=OSTemplateGroupResponse)
def update_template_group(
    group_id: int,
    group_data: OSTemplateGroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Обновить группу шаблонов"""
    group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа шаблонов не найдена")
    
    update_data = group_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(group, field, value)
    
    db.commit()
    db.refresh(group)
    
    logger.info(f"User {current_user.username} updated template group: {group.name}")
    return group


@router.delete("/api/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Удалить группу шаблонов"""
    group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа шаблонов не найдена")
    
    # Check if there are templates in this group
    templates_count = db.query(OSTemplate).filter(OSTemplate.group_id == group_id).count()
    if templates_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Невозможно удалить группу: в ней {templates_count} шаблон(ов)"
        )
    
    logger.info(f"User {current_user.username} deleted template group: {group.name}")
    db.delete(group)
    db.commit()
    return None


# ==================== OS Templates CRUD ====================

@router.get("/api/templates", response_model=List[OSTemplateWithGroup])
def list_os_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view")),
    group_id: int = None,
    server_id: int = None,
    vm_type: Optional[str] = None,
    active_only: bool = True
):
    """Получить список всех OS шаблонов с информацией о группе"""
    query = db.query(OSTemplate)
    
    if group_id:
        query = query.filter(OSTemplate.group_id == group_id)
    if server_id:
        query = query.filter(OSTemplate.server_id == server_id)
    if vm_type in ('qemu', 'lxc'):
        query = query.filter(OSTemplate.vm_type == vm_type)
    if active_only:
        query = query.filter(OSTemplate.is_active == True)
    # Tenants only see templates of servers they can deploy to (see deploy).
    visible = get_visible_server_ids(_NO_WORKSPACE_HEADER, db, current_user)
    if visible is not None:
        query = query.filter(OSTemplate.server_id.in_(visible))
    
    os_templates = query.order_by(OSTemplate.sort_order).all()
    
    # Enrich with group and server info (two queries, not two per template)
    group_ids = {t.group_id for t in os_templates if t.group_id is not None}
    server_ids = {t.server_id for t in os_templates if t.server_id is not None}
    groups = {g.id: g for g in db.query(OSTemplateGroup).filter(OSTemplateGroup.id.in_(group_ids))} if group_ids else {}
    servers = {s.id: s for s in db.query(ProxmoxServer).filter(ProxmoxServer.id.in_(server_ids))} if server_ids else {}
    result = []
    for template in os_templates:
        group = groups.get(template.group_id)
        server = servers.get(template.server_id)
        
        template_dict = {
            **template.__dict__,
            'group_name': group.name if group else None,
            'group_icon': group.icon if group else None,
            'server_name': server.name if server else None,
        }
        # Remove SQLAlchemy internal state
        template_dict.pop('_sa_instance_state', None)
        result.append(template_dict)
    
    return result


@router.post("/api/templates", response_model=OSTemplateResponse, status_code=status.HTTP_201_CREATED)
def create_os_template(
    template_data: OSTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Создать новый OS шаблон"""
    # Validate group exists
    group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == template_data.group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа шаблонов не найдена")
    
    # Validate server exists
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template_data.server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    
    # Check if template with same VMID on same server exists
    existing = db.query(OSTemplate).filter(
        OSTemplate.server_id == template_data.server_id,
        OSTemplate.vmid == template_data.vmid
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Шаблон с VMID {template_data.vmid} уже существует для этого сервера"
        )
    
    template = OSTemplate(**template_data.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    
    logger.info(f"User {current_user.username} created OS template: {template.name} (VMID: {template.vmid})")
    return template


@router.get("/api/templates/{template_id}", response_model=OSTemplateWithGroup)
def get_os_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """Получить OS шаблон по ID"""
    template = db.query(OSTemplate).filter(OSTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    
    group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == template.group_id).first()
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template.server_id).first()
    
    template_dict = {
        **template.__dict__,
        'group_name': group.name if group else None,
        'group_icon': group.icon if group else None,
        'server_name': server.name if server else None,
    }
    template_dict.pop('_sa_instance_state', None)
    
    return template_dict


@router.put("/api/templates/{template_id}", response_model=OSTemplateResponse)
def update_os_template(
    template_id: int,
    template_data: OSTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Обновить OS шаблон"""
    template = db.query(OSTemplate).filter(OSTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    
    update_data = template_data.model_dump(exclude_unset=True)
    
    # Validate group if changing
    if 'group_id' in update_data:
        group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == update_data['group_id']).first()
        if not group:
            raise HTTPException(status_code=404, detail="Группа шаблонов не найдена")
    
    # Validate server if changing
    if 'server_id' in update_data:
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == update_data['server_id']).first()
        if not server:
            raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    
    for field, value in update_data.items():
        setattr(template, field, value)
    
    db.commit()
    db.refresh(template)
    
    logger.info(f"User {current_user.username} updated OS template: {template.name}")
    return template


@router.delete("/api/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_os_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Удалить OS шаблон"""
    template = db.query(OSTemplate).filter(OSTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    
    logger.info(f"User {current_user.username} deleted OS template: {template.name} (VMID: {template.vmid})")
    db.delete(template)
    db.commit()
    return None


# ==================== Proxmox Templates Discovery ====================

@router.get("/api/discover/{server_id}", dependencies=[Depends(enforce_server_visibility)])
def discover_proxmox_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """
    Обнаружить все VM-шаблоны на Proxmox сервере.
    Возвращает список шаблонов, которые можно добавить.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Не удалось подключиться к Proxmox серверу")
        
        # Get templates - фильтруем по hostname ноды если это кластер
        node_filter = None
        if server.hostname and server.hostname != server.ip_address:
            node_filter = server.hostname
        
        proxmox_templates = client.get_templates(node=node_filter)
        
        # Get already added templates для ЭТОГО сервера
        existing = db.query(OSTemplate).filter(OSTemplate.server_id == server_id).all()
        existing_vmids = set(t.vmid for t in existing if t.vmid is not None)
        existing_volids = set(t.volid for t in existing if t.volid)

        result = []
        for tpl in proxmox_templates:
            volid = tpl.get('volid')
            vmid = tpl.get('vmid')
            already = (volid is not None and volid in existing_volids) or (vmid is not None and vmid in existing_vmids)
            result.append({
                'vmid': vmid,
                'volid': volid,
                'name': tpl.get('name', f"Template-{vmid or volid}"),
                'node': tpl.get('node'),
                'status': tpl.get('status'),
                'vm_type': tpl.get('type', 'qemu'),
                'is_file_template': tpl.get('is_file_template', False),
                'maxmem': tpl.get('maxmem', 0),
                'maxdisk': tpl.get('maxdisk', 0),
                'already_added': already,
            })
        
        return JSONResponse(content={
            'server_id': server_id,
            'server_name': server.name,
            'templates': result
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error discovering templates on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка обнаружения шаблонов: {str(e)}")


# ==================== Auto-Import Templates ====================

# Иконки используют CSS классы для адаптации к теме
_UBUNTU = {'name': 'Ubuntu', 'icon': '<i class="fa-brands fa-ubuntu os-icon os-icon-ubuntu"></i>', 'order': 1}
_DEBIAN = {'name': 'Debian', 'icon': '<i class="fa-brands fa-debian os-icon os-icon-debian"></i>', 'order': 2}
_CENTOS = {'name': 'CentOS', 'icon': '<i class="fa-brands fa-centos os-icon os-icon-centos"></i>', 'order': 3}
_ROCKY = {'name': 'Rocky Linux', 'icon': '<span class="os-icon-rocky-svg"></span>', 'order': 4}
_ALMA = {'name': 'AlmaLinux', 'icon': '<span class="os-icon-alma-svg"></span>', 'order': 5}
_FEDORA = {'name': 'Fedora', 'icon': '<i class="fa-brands fa-fedora os-icon os-icon-fedora"></i>', 'order': 6}
_RHEL = {'name': 'RHEL', 'icon': '<i class="fa-brands fa-redhat os-icon os-icon-rhel"></i>', 'order': 7}
_ORACLE = {'name': 'Oracle Linux', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-oracle"></i>', 'order': 8}
_SUSE = {'name': 'openSUSE', 'icon': '<i class="fa-brands fa-suse os-icon os-icon-suse"></i>', 'order': 9}
_ARCH = {'name': 'Arch Linux', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-arch"></i>', 'order': 10}
_GENTOO = {'name': 'Gentoo', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-gentoo"></i>', 'order': 11}
_ALPINE = {'name': 'Alpine', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-alpine"></i>', 'order': 12}
_KALI = {'name': 'Kali Linux', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-kali"></i>', 'order': 13}
_MINT = {'name': 'Linux Mint', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-mint"></i>', 'order': 14}
_WINDOWS = {'name': 'Windows', 'icon': '<i class="fa-brands fa-windows os-icon os-icon-windows"></i>', 'order': 20}
_FREEBSD = {'name': 'FreeBSD', 'icon': '<i class="fa-brands fa-freebsd os-icon os-icon-freebsd"></i>', 'order': 30}
_OPENBSD = {'name': 'OpenBSD', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-openbsd"></i>', 'order': 31}
_OTHER = {'name': 'Other', 'icon': '<i class="fa-solid fa-compact-disc os-icon os-icon-other"></i>', 'order': 99}

# Маппинг ключевых слов на группы. Порядок важен — более специфичные/длинные
# ключи должны идти раньше (напр. 'oraclelinux' перед 'oracle', 'rockylinux'
# перед 'rocky'), чтобы не перехватывались более общими. Включены частые
# опечатки и варианты написания (alama/allma, redhat/red hat, sles/leap/...).
_OS_KEYWORD_GROUPS: list[tuple[tuple[str, ...], dict]] = [
    (('ubuntu',), _UBUNTU),
    (('debian',), _DEBIAN),
    (('linuxmint', 'mint'), _MINT),
    (('centos',), _CENTOS),
    (('rockylinux', 'rocky'), _ROCKY),
    (('almalinux', 'alma', 'alama', 'allma', 'alamalinux'), _ALMA),
    (('fedora',), _FEDORA),
    (('oraclelinux', 'oracle'), _ORACLE),
    (('rhel', 'redhat', 'red hat', 'red-hat'), _RHEL),
    (('opensuse', 'sles', 'suse', 'leap', 'tumbleweed'), _SUSE),
    (('archlinux', 'arch'), _ARCH),
    (('gentoo',), _GENTOO),
    (('alpine',), _ALPINE),
    (('kali',), _KALI),
    (('windows', 'win2', 'win10', 'win11', 'winserver', 'win7', 'win8'), _WINDOWS),
    (('freebsd',), _FREEBSD),
    (('openbsd',), _OPENBSD),
]

# Обратная совместимость: плоский маппинг keyword → group_info.
OS_GROUP_MAPPING = {kw: info for keys, info in _OS_KEYWORD_GROUPS for kw in keys}
OS_GROUP_MAPPING['other'] = _OTHER


def detect_os_group(template_name: str) -> dict:
    """Определить группу ОС по имени шаблона (с учётом вариантов и опечаток)."""
    name_lower = (template_name or '').lower()

    for keys, group_info in _OS_KEYWORD_GROUPS:
        if any(kw in name_lower for kw in keys):
            return group_info

    return _OTHER


def _do_auto_import(server_id: int, username: str):
    """
    Background task: import Proxmox templates into DB.
    Opens its own DB session so it can run after the request completes.
    """
    db = SessionLocal()
    try:
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
        if not server:
            logger.error(f"Auto-import: server {server_id} not found")
            return

        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )

        if not client.is_connected():
            logger.error(f"Auto-import: cannot connect to server {server.name}")
            return

        node_filter = None
        if server.hostname and server.hostname != server.ip_address:
            node_filter = server.hostname

        proxmox_templates = client.get_templates(node=node_filter)

        existing = db.query(OSTemplate).filter(OSTemplate.server_id == server_id).all()
        existing_vmids = set(t.vmid for t in existing if t.vmid is not None)
        existing_volids = set(t.volid for t in existing if t.volid)

        groups_cache = {g.name: g for g in db.query(OSTemplateGroup).all()}

        templates_by_group = {}
        for tpl in proxmox_templates:
            volid = tpl.get('volid')
            vmid = tpl.get('vmid')
            if volid and volid in existing_volids:
                continue
            if vmid is not None and vmid in existing_vmids:
                continue
            template_name = tpl.get('name', f"Template-{vmid or volid}")
            group_info = detect_os_group(template_name)
            group_name = group_info['name']
            if group_name not in templates_by_group:
                templates_by_group[group_name] = {'info': group_info, 'templates': []}
            templates_by_group[group_name]['templates'].append({
                'vmid': vmid,
                'volid': volid,
                'name': template_name,
                'node': tpl.get('node'),
                'vm_type': tpl.get('type', 'qemu'),
                'maxmem': tpl.get('maxmem', 0),
                'maxdisk': tpl.get('maxdisk', 0),
            })

        imported = []
        global_sort_order = 0
        sorted_groups = sorted(templates_by_group.items(), key=lambda x: x[1]['info']['order'])

        for group_name, group_data in sorted_groups:
            group_info = group_data['info']
            group_templates = group_data['templates']

            if group_name not in groups_cache:
                new_group = OSTemplateGroup(
                    name=group_name,
                    icon=group_info['icon'],
                    description=f"Шаблоны {group_name}",
                    sort_order=group_info['order'],
                    is_active=True
                )
                db.add(new_group)
                db.flush()
                groups_cache[group_name] = new_group
                logger.info(f"Auto-import: created group {group_name}")

            group = groups_cache[group_name]
            group_templates.sort(key=lambda x: x['name'])

            for tpl in group_templates:
                default_memory = max(1024, (tpl.get('maxmem', 0) // (1024 * 1024)) or 1024)
                default_disk = max(10, (tpl.get('maxdisk', 0) // (1024 * 1024 * 1024)) or 10)
                template = OSTemplate(
                    group_id=group.id,
                    server_id=server_id,
                    name=tpl['name'],
                    vmid=tpl.get('vmid'),
                    volid=tpl.get('volid'),
                    vm_type=tpl.get('vm_type', 'qemu'),
                    node=tpl.get('node'),
                    source_node=tpl.get('node'),
                    default_cores=2,
                    default_memory=default_memory,
                    default_disk=default_disk,
                    min_cores=1,
                    min_memory=512,
                    min_disk=5,
                    is_active=True,
                    sort_order=global_sort_order
                )
                db.add(template)
                imported.append({'vmid': tpl.get('vmid'), 'volid': tpl.get('volid'), 'name': tpl['name'], 'group': group_name})
                global_sort_order += 1

        db.commit()
        logger.info(f"Auto-import by {username}: imported {len(imported)} templates from {server.name}")

    except Exception as e:
        logger.error(f"Auto-import background task failed for server {server_id}: {e}")
        db.rollback()
    finally:
        db.close()


@router.post("/api/auto-import/{server_id}", dependencies=[Depends(enforce_server_visibility)])
def auto_import_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """
    Синхронный импорт шаблонов с Proxmox сервера.
    Возвращает список импортированных шаблонов и их количество.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")

    if server.use_password:
        client = ProxmoxClient(
            host=server.ip_address,
            user=server.api_user,
            password=server.password,
            verify_ssl=server.verify_ssl
        )
    else:
        client = ProxmoxClient(
            host=server.ip_address,
            user=server.api_user,
            token_name=server.api_token_name,
            token_value=server.api_token_value,
            verify_ssl=server.verify_ssl
        )

    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Не удалось подключиться к Proxmox серверу")

    node_filter = None
    if server.hostname and server.hostname != server.ip_address:
        node_filter = server.hostname

    proxmox_templates = client.get_templates(node=node_filter)

    existing = db.query(OSTemplate).filter(OSTemplate.server_id == server_id).all()
    existing_vmids = set(t.vmid for t in existing if t.vmid is not None)
    existing_volids = set(t.volid for t in existing if t.volid)
    groups_cache = {g.name: g for g in db.query(OSTemplateGroup).all()}

    templates_by_group = {}
    for tpl in proxmox_templates:
        volid = tpl.get('volid')
        vmid = tpl.get('vmid')
        if volid and volid in existing_volids:
            continue
        if vmid is not None and vmid in existing_vmids:
            continue
        template_name = tpl.get('name', f"Template-{vmid or volid}")
        group_info = detect_os_group(template_name)
        group_name = group_info['name']
        if group_name not in templates_by_group:
            templates_by_group[group_name] = {'info': group_info, 'templates': []}
        templates_by_group[group_name]['templates'].append({
            'vmid': vmid,
            'volid': volid,
            'name': template_name,
            'node': tpl.get('node'),
            'vm_type': tpl.get('type', 'qemu'),
            'maxmem': tpl.get('maxmem', 0),
            'maxdisk': tpl.get('maxdisk', 0),
        })

    imported = []
    global_sort_order = 0
    sorted_groups = sorted(templates_by_group.items(), key=lambda x: x[1]['info']['order'])

    try:
        for group_name, group_data in sorted_groups:
            group_info = group_data['info']
            group_templates = group_data['templates']

            if group_name not in groups_cache:
                new_group = OSTemplateGroup(
                    name=group_name,
                    icon=group_info['icon'],
                    description=f"Шаблоны {group_name}",
                    sort_order=group_info['order'],
                    is_active=True
                )
                db.add(new_group)
                db.flush()
                groups_cache[group_name] = new_group

            group = groups_cache[group_name]
            group_templates.sort(key=lambda x: x['name'])

            for tpl in group_templates:
                default_memory = max(1024, (tpl.get('maxmem', 0) // (1024 * 1024)) or 1024)
                default_disk = max(10, (tpl.get('maxdisk', 0) // (1024 * 1024 * 1024)) or 10)
                template = OSTemplate(
                    group_id=group.id,
                    server_id=server_id,
                    name=tpl['name'],
                    vmid=tpl.get('vmid'),
                    volid=tpl.get('volid'),
                    vm_type=tpl.get('vm_type', 'qemu'),
                    node=tpl.get('node'),
                    source_node=tpl.get('node'),
                    default_cores=2,
                    default_memory=default_memory,
                    default_disk=default_disk,
                    min_cores=1,
                    min_memory=512,
                    min_disk=5,
                    is_active=True,
                    sort_order=global_sort_order
                )
                db.add(template)
                imported.append({
                    'vmid': tpl.get('vmid'),
                    'volid': tpl.get('volid'),
                    'name': tpl['name'],
                    'group': group_name,
                    'sort_order': global_sort_order
                })
                global_sort_order += 1

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Auto-import failed for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка импорта: {str(e)}")

    logger.info(f"User {current_user.username} imported {len(imported)} templates from {server.name}")

    return JSONResponse(content={
        'success': True,
        'server_id': server_id,
        'server_name': server.name,
        'imported_count': len(imported),
        'templates': imported
    })


# ==================== VM Deployment ====================

def _update_deploy_task(task_id: int, status: str, step: str, progress: int,
                        vmid: int = None, node: str = None, error: str = None):
    """Update deploy task state in DB and broadcast via WebSocket."""
    db = SessionLocal()
    try:
        task = db.query(DeployTask).filter(DeployTask.id == task_id).first()
        if not task:
            return
        task.status = status
        task.step = step
        task.progress = progress
        if vmid is not None:
            task.vmid = vmid
        if node is not None:
            task.node = node
        if error is not None:
            task.error_message = error
        if status == 'running' and not task.started_at:
            task.started_at = _utcnow()
        if status in ('completed', 'failed'):
            task.completed_at = _utcnow()
        db.commit()
        try:
            broadcast_task_update(task.user_id, "deploy_task_update", task.to_dict())
        except Exception:
            pass
    finally:
        db.close()


def _do_deploy_sync(task_id: int, deploy_data: VMDeployRequest,
                    user_id: int, username: str,
                    owner_id: int = None):
    """Blocking deploy logic — runs in a thread pool."""
    db = SessionLocal()
    server = None
    template = None
    deploy_node = None
    new_vmid = None
    try:
        _update_deploy_task(task_id, 'running', 'Подготовка...', 5)

        template = db.query(OSTemplate).filter(OSTemplate.id == deploy_data.template_id).first()
        if not template:
            _update_deploy_task(task_id, 'failed', 'Шаблон не найден', 0, error='Шаблон не найден')
            return

        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template.server_id).first()
        if not server:
            _update_deploy_task(task_id, 'failed', 'Сервер не найден', 0, error='Сервер Proxmox не найден')
            return

        cores = deploy_data.cores or template.default_cores
        memory = deploy_data.memory or template.default_memory
        disk = deploy_data.disk or template.default_disk

        # Connect
        _update_deploy_task(task_id, 'running', 'Подключение к Proxmox...', 10)
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address, user=server.api_user,
                password=server.password, verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address, user=server.api_user,
                token_name=server.api_token_name, token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        if not client.is_connected():
            _update_deploy_task(task_id, 'failed', 'Ошибка подключения к Proxmox', 10,
                                error='Не удалось подключиться к Proxmox серверу')
            return

        # Locate template
        _update_deploy_task(task_id, 'running', 'Поиск шаблона...', 20)
        source_node = template.get_source_node()
        target_node = deploy_data.target_node or source_node
        template_vmid = template.vmid

        template_status = client.get_vm_status(source_node, template_vmid)
        if not template_status:
            found = client.find_template_on_nodes(template_vmid)
            if found:
                source_node = found['node']
            else:
                _update_deploy_task(task_id, 'failed', 'Шаблон не найден на Proxmox', 20,
                                    error=f'VMID {template_vmid} не найден на сервере')
                return

        # Allocate VMID
        _update_deploy_task(task_id, 'running', 'Генерация VMID...', 25)
        if deploy_data.vmid:
            new_vmid = deploy_data.vmid
        else:
            new_vmid = get_next_vmid(db, server.id)

        # Clone
        _update_deploy_task(task_id, 'running', f'Клонирование шаблона → VMID {new_vmid}...', 30)
        clone_upid = client.clone_vm_from_template(
            node=source_node,
            template_vmid=template_vmid,
            new_vmid=new_vmid,
            name=deploy_data.name,
            full_clone=True,
            description=f"Deployed from template {template.name} by {username}",
            target_storage=deploy_data.target_storage,
            target_node=target_node if target_node != source_node else None
        )
        if not clone_upid:
            _update_deploy_task(task_id, 'failed', 'Ошибка клонирования', 30,
                                error='Proxmox не вернул UPID задачи клонирования')
            return

        # Wait for clone — progress from 30 to 65
        _update_deploy_task(task_id, 'running', 'Клонирование диска...', 45, vmid=new_vmid)
        clone_success = client.wait_for_task(source_node, clone_upid, timeout=300)
        if not clone_success:
            _update_deploy_task(task_id, 'failed', 'Таймаут клонирования', 45,
                                error='Клонирование заняло слишком долго (>5 мин)')
            return

        deploy_node = target_node
        _update_deploy_task(task_id, 'running', 'Клонирование завершено', 65, vmid=new_vmid, node=deploy_node)

        # Клон может ещё держать lock (особенно cross-node) — config.put упадёт
        _update_deploy_task(task_id, 'running', 'Ожидание разблокировки VM...', 66, vmid=new_vmid, node=deploy_node)
        client.wait_for_vm_unlock(deploy_node, new_vmid, timeout=120)

        # IPAM
        allocated_ip = deploy_data.ip_address
        ipam_allocation = None
        if deploy_data.ipam_network_id and not deploy_data.ip_address:
            _update_deploy_task(task_id, 'running', 'Выделение IP-адреса (IPAM)...', 68, vmid=new_vmid, node=deploy_node)
            ipam = IPAMService(db)
            ipam_allocation, error = ipam.auto_allocate_ip(
                network_id=deploy_data.ipam_network_id,
                pool_id=deploy_data.ipam_pool_id,
                resource_type='vm',
                resource_name=deploy_data.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=deploy_node,
                allocated_by=username,
                notes=f"Auto-allocated for VM deployment from template {template.name}"
            )
            if not error:
                allocated_ip = ipam_allocation.ip_address
                if not deploy_data.gateway:
                    from ..models import IPAMNetwork
                    net = db.query(IPAMNetwork).filter(IPAMNetwork.id == deploy_data.ipam_network_id).first()
                    if net and net.gateway:
                        deploy_data.gateway = net.gateway
        elif deploy_data.ip_address and deploy_data.ipam_network_id:
            ipam = IPAMService(db)
            ipam_allocation, _ = ipam.allocate_ip(
                ip_address=deploy_data.ip_address,
                network_id=deploy_data.ipam_network_id,
                pool_id=deploy_data.ipam_pool_id,
                resource_type='vm',
                resource_name=deploy_data.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=deploy_node,
                allocated_by=username,
                notes=f"Manually assigned for VM deployment from template {template.name}"
            )

        # Determine network mask & gateway
        network_mask = "24"
        gateway = deploy_data.gateway
        if allocated_ip and (deploy_data.ipam_network_id or ipam_allocation):
            try:
                from ..models import IPAMNetwork
                network_id = deploy_data.ipam_network_id
                if ipam_allocation and hasattr(ipam_allocation, 'network_id'):
                    network_id = ipam_allocation.network_id
                if network_id:
                    net = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
                    if net:
                        if net.network and '/' in net.network:
                            network_mask = net.network.split('/')[1]
                        if not gateway and net.gateway:
                            gateway = net.gateway
            except Exception as e:
                logger.warning(f"Could not get network info from IPAM: {e}")

        ip_config = None
        if allocated_ip:
            ip_config = f"ip={allocated_ip}/{network_mask},gw={gateway}" if gateway else f"ip={allocated_ip}/{network_mask}"
        else:
            ip_config = "ip=dhcp"

        # Configure VM
        _update_deploy_task(task_id, 'running', 'Настройка VM (CPU, RAM, Cloud-Init)...', 72, vmid=new_vmid, node=deploy_node)
        # Ключи уже собраны и дедуплицированы в эндпоинте
        # (build_deploy_ssh_keys): вставленные вручную + выбранные из
        # библиотеки + профильный ключ владельца инстанса.
        ssh_keys = deploy_data.ssh_keys or ""

        config_result = client.configure_vm(
            node=deploy_node,
            vmid=new_vmid,
            cores=cores,
            memory=memory,
            disk_size=disk,
            network_bridge=deploy_data.network_bridge,
            cloud_init_user=deploy_data.cloud_init_user,
            cloud_init_password=deploy_data.cloud_init_password,
            ssh_keys=ssh_keys if ssh_keys else None,
            ip_config=ip_config,
            onboot=deploy_data.onboot
        )
        config_warnings = None
        if not config_result.get('ok'):
            config_warnings = '; '.join(
                f'{group}: {msg}' for group, msg in config_result.get('errors', {}).items()
            )
            logger.warning(f"VM {new_vmid} created but some settings failed: {config_warnings}")

        # Start
        if deploy_data.start_after_create:
            _update_deploy_task(task_id, 'running', 'Запуск VM...', 88, vmid=new_vmid, node=deploy_node)
            client.start_vm(deploy_node, new_vmid)

        # HA
        if deploy_data.enable_ha:
            try:
                if client.is_cluster():
                    client.add_to_ha(vmid=new_vmid, vm_type='vm', max_restart=3, max_relocate=3)
            except Exception as ha_error:
                logger.error(f"HA error for VM {new_vmid}: {ha_error}")

        # Save to DB
        _update_deploy_task(task_id, 'running', 'Сохранение в базе данных...', 95, vmid=new_vmid, node=deploy_node)
        user = db.query(User).filter(User.id == user_id).first()
        save_vm_instance(
            db=db,
            server_id=server.id,
            vmid=new_vmid,
            node=deploy_node,
            vm_type='qemu',
            name=deploy_data.name,
            cores=cores,
            memory=memory,
            disk_size=disk,
            ip_address=allocated_ip,
            ip_prefix=int(network_mask),
            gateway=gateway,
            cloud_init_user=deploy_data.cloud_init_user,
            cloud_init_password=deploy_data.cloud_init_password,
            ssh_keys=deploy_data.ssh_keys,
            template_id=template.id,
            template_name=template.name,
            description=f"Deployed from template {template.name}",
            owner_id=owner_id or user_id
        )

        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="vm",
            resource_id=new_vmid,
            username=username,
            resource_name=deploy_data.name,
            server_id=server.id,
            server_name=server.name,
            node_name=deploy_node,
            details={"template_id": template.id, "template_name": template.name, "cores": cores,
                     "memory": memory, "disk": disk, "ip_address": allocated_ip},
            success=True
        )

        if config_warnings:
            _update_deploy_task(task_id, 'completed', 'VM создана с предупреждениями', 100,
                                vmid=new_vmid, node=deploy_node, error=config_warnings)
        else:
            _update_deploy_task(task_id, 'completed', 'VM успешно создана!', 100, vmid=new_vmid, node=deploy_node)
        logger.info(f"[DEPLOY] Task #{task_id}: VM {deploy_data.name} (VMID {new_vmid}) deployed by {username}")

    except Exception as e:
        err = str(e)
        logger.error(f"[DEPLOY] Task #{task_id} failed: {err}")
        # Log failed
        try:
            if server and template:
                LoggingService.log_proxmox_action(
                    db=db,
                    action="create",
                    resource_type="vm",
                    resource_id=deploy_data.vmid,
                    username=username,
                    resource_name=deploy_data.name,
                    server_id=server.id if server else None,
                    server_name=server.name if server else None,
                    node_name=deploy_node,
                    details={"template_id": template.id if template else None},
                    success=False,
                    error_message=err
                )
        except Exception:
            pass
        _update_deploy_task(task_id, 'failed', f'Ошибка: {err[:150]}', 0, error=err)
    finally:
        db.close()


@router.post("/api/deploy", response_model=DeployTaskStartResponse, status_code=202)
async def deploy_vm_from_template(
    deploy_data: VMDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:create"))
):
    """
    Асинхронный деплой VM из шаблона.
    Немедленно возвращает task_id, выполнение идёт в фоне.
    Статус можно отслеживать через GET /api/deploy/{task_id}.
    """
    # Fast validation before queuing
    template = db.query(OSTemplate).filter(OSTemplate.id == deploy_data.template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    if not template.is_active:
        raise HTTPException(status_code=400, detail="Шаблон неактивен")
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template.server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    # The body names a template, not a server, so the router-level server
    # check does not apply: a tenant could deploy onto any server.
    visible = get_visible_server_ids(_NO_WORKSPACE_HEADER, db, current_user)
    if visible is not None and server.id not in visible:
        raise HTTPException(status_code=404, detail="Шаблон не найден")

    cores = deploy_data.cores or template.default_cores
    memory = deploy_data.memory or template.default_memory
    disk = deploy_data.disk or template.default_disk
    if cores < template.min_cores:
        raise HTTPException(status_code=400, detail=f"Минимум {template.min_cores} ядер CPU")
    if memory < template.min_memory:
        raise HTTPException(status_code=400, detail=f"Минимум {template.min_memory} MB памяти")
    if disk < template.min_disk:
        raise HTTPException(status_code=400, detail=f"Минимум {template.min_disk} GB диска")

    # Resolve owner / SSH keys (admin may target another user)
    is_admin = current_user.has_permission("user:manage") or current_user.is_admin
    instance_owner_id = current_user.id
    if deploy_data.owner_id and deploy_data.owner_id != current_user.id:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admins can assign instance to another user")
        owner = db.query(User).filter(User.id == deploy_data.owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner user not found")
        instance_owner_id = owner.id

    from ..schemas import validate_instance_name
    try:
        deploy_data.name = validate_instance_name(deploy_data.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Enforce per-user resource quota (no-op if owner has no quota set)
    from ..services.quota_service import check_quota
    check_quota(db, instance_owner_id, add_cores=cores, add_memory_mb=memory, add_disk_gb=disk)

    from .ssh_keys import build_deploy_ssh_keys
    deploy_data.ssh_keys = build_deploy_ssh_keys(
        db, current_user, instance_owner_id,
        deploy_data.ssh_key_ids, deploy_data.ssh_keys,
    ) or None

    # Create deploy task record
    task = DeployTask(
        status='pending',
        step='В очереди...',
        progress=0,
        name=deploy_data.name,
        template_id=template.id,
        server_id=server.id,
        user_id=current_user.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id

    # Schedule blocking deploy in thread pool
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _deploy_executor,
        _do_deploy_sync,
        task_id,
        deploy_data,
        current_user.id,
        current_user.username,
        instance_owner_id,
    )

    logger.info(f"[DEPLOY] Task #{task_id} queued: {deploy_data.name} by {current_user.username}")
    return DeployTaskStartResponse(task_id=task_id, status='pending', name=deploy_data.name)


@router.get("/api/deploy/{task_id}", response_model=DeployTaskStatusResponse)
async def get_deploy_task_status(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Получить статус задачи деплоя."""
    task = db.query(DeployTask).filter(DeployTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Нет доступа")
    return DeployTaskStatusResponse(**task.to_dict())


# ==================== Quick Deploy (for UI) ====================

@router.get("/api/quick-deploy/{template_id}")
def get_quick_deploy_info(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """Получить информацию для быстрого развертывания VM"""
    template = db.query(OSTemplate).filter(OSTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    
    group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == template.group_id).first()
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template.server_id).first()
    
    return JSONResponse(content={
        'template': {
            'id': template.id,
            'name': template.name,
            'vmid': template.vmid,
            'node': template.node,
            'description': template.description,
            'default_cores': template.default_cores,
            'default_memory': template.default_memory,
            'default_disk': template.default_disk,
            'min_cores': template.min_cores,
            'min_memory': template.min_memory,
            'min_disk': template.min_disk,
        },
        'group': {
            'name': group.name if group else None,
            'icon': group.icon if group else None,
        },
        'server': {
            'id': server.id if server else None,
            'name': server.name if server else None,
        }
    })
