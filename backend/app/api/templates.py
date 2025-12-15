"""
API endpoints for OS Templates management
Allows creating VM templates, groups, and deploying VMs from templates
"""

from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from loguru import logger
from typing import List

from ..db import get_db
from ..models import ProxmoxServer, OSTemplateGroup, OSTemplate, VMInstance, User
from ..schemas import (
    OSTemplateGroupCreate, OSTemplateGroupUpdate, OSTemplateGroupResponse,
    OSTemplateCreate, OSTemplateUpdate, OSTemplateResponse, OSTemplateWithGroup,
    VMDeployRequest, VMDeployResponse
)
from ..proxmox_client import ProxmoxClient
from ..api.proxmox import get_next_vmid, save_vm_instance, get_vm_instance
from ..auth import get_current_user, PermissionChecker
from ..ipam_service import IPAMService
from ..logging_service import LoggingService
from ..template_helpers import add_i18n_context

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ==================== HTML Pages ====================

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def templates_page(request: Request, db: Session = Depends(get_db)):
    """Страница управления OS шаблонами"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    proxmox_servers = db.query(ProxmoxServer).all()
    template_groups = db.query(OSTemplateGroup).order_by(OSTemplateGroup.sort_order).all()
    os_templates = db.query(OSTemplate).order_by(OSTemplate.sort_order).all()
    
    context = {
        "request": request,
        "proxmox_servers": proxmox_servers,
        "template_groups": template_groups,
        "os_templates": os_templates,
        "page_title": t('nav_templates', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("os_templates.html", context)


# ==================== Template Groups CRUD ====================

@router.get("/api/groups", response_model=List[OSTemplateGroupResponse])
def list_template_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.view"))
):
    """Получить список всех групп шаблонов"""
    groups = db.query(OSTemplateGroup).order_by(OSTemplateGroup.sort_order).all()
    return groups


@router.post("/api/groups", response_model=OSTemplateGroupResponse, status_code=status.HTTP_201_CREATED)
def create_template_group(
    group_data: OSTemplateGroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.manage"))
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
    current_user: User = Depends(PermissionChecker("templates.view"))
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
    current_user: User = Depends(PermissionChecker("templates.manage"))
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
    current_user: User = Depends(PermissionChecker("templates.manage"))
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
    current_user: User = Depends(PermissionChecker("templates.view")),
    group_id: int = None,
    server_id: int = None,
    active_only: bool = True
):
    """Получить список всех OS шаблонов с информацией о группе"""
    query = db.query(OSTemplate)
    
    if group_id:
        query = query.filter(OSTemplate.group_id == group_id)
    if server_id:
        query = query.filter(OSTemplate.server_id == server_id)
    if active_only:
        query = query.filter(OSTemplate.is_active == True)
    
    os_templates = query.order_by(OSTemplate.sort_order).all()
    
    # Enrich with group and server info
    result = []
    for template in os_templates:
        group = db.query(OSTemplateGroup).filter(OSTemplateGroup.id == template.group_id).first()
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template.server_id).first()
        
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
    current_user: User = Depends(PermissionChecker("templates.manage"))
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
    current_user: User = Depends(PermissionChecker("templates.view"))
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
    current_user: User = Depends(PermissionChecker("templates.manage"))
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
    current_user: User = Depends(PermissionChecker("templates.manage"))
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

@router.get("/api/discover/{server_id}")
def discover_proxmox_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.view"))
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
        existing_vmids = set(
            t.vmid for t in db.query(OSTemplate).filter(OSTemplate.server_id == server_id).all()
        )
        
        result = []
        for tpl in proxmox_templates:
            result.append({
                'vmid': tpl.get('vmid'),
                'name': tpl.get('name', f"Template-{tpl.get('vmid')}"),
                'node': tpl.get('node'),
                'status': tpl.get('status'),
                'maxmem': tpl.get('maxmem', 0),
                'maxdisk': tpl.get('maxdisk', 0),
                'already_added': tpl.get('vmid') in existing_vmids
            })
        
        return JSONResponse(content={
            'server_id': server_id,
            'server_name': server.name,
            'templates': result
        })
        
    except Exception as e:
        logger.error(f"Error discovering templates on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка обнаружения шаблонов: {str(e)}")


# ==================== Auto-Import Templates ====================

# Маппинг ключевых слов на группы
# Иконки используют CSS классы для адаптации к теме
OS_GROUP_MAPPING = {
    'ubuntu': {'name': 'Ubuntu', 'icon': '<i class="fa-brands fa-ubuntu os-icon os-icon-ubuntu"></i>', 'order': 1},
    'debian': {'name': 'Debian', 'icon': '<i class="fa-brands fa-debian os-icon os-icon-debian"></i>', 'order': 2},
    'centos': {'name': 'CentOS', 'icon': '<i class="fa-brands fa-centos os-icon os-icon-centos"></i>', 'order': 3},
    'rocky': {'name': 'Rocky Linux', 'icon': '<span class="os-icon-rocky-svg"></span>', 'order': 4},
    'alma': {'name': 'AlmaLinux', 'icon': '<span class="os-icon-alma-svg"></span>', 'order': 5},
    'almalinux': {'name': 'AlmaLinux', 'icon': '<span class="os-icon-alma-svg"></span>', 'order': 5},
    'fedora': {'name': 'Fedora', 'icon': '<i class="fa-brands fa-fedora os-icon os-icon-fedora"></i>', 'order': 6},
    'rhel': {'name': 'RHEL', 'icon': '<i class="fa-brands fa-redhat os-icon os-icon-rhel"></i>', 'order': 7},
    'oracle': {'name': 'Oracle Linux', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-oracle"></i>', 'order': 8},
    'suse': {'name': 'openSUSE', 'icon': '<i class="fa-brands fa-suse os-icon os-icon-suse"></i>', 'order': 9},
    'opensuse': {'name': 'openSUSE', 'icon': '<i class="fa-brands fa-suse os-icon os-icon-suse"></i>', 'order': 9},
    'arch': {'name': 'Arch Linux', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-arch"></i>', 'order': 10},
    'gentoo': {'name': 'Gentoo', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-gentoo"></i>', 'order': 11},
    'alpine': {'name': 'Alpine', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-alpine"></i>', 'order': 12},
    'windows': {'name': 'Windows', 'icon': '<i class="fa-brands fa-windows os-icon os-icon-windows"></i>', 'order': 20},
    'win': {'name': 'Windows', 'icon': '<i class="fa-brands fa-windows os-icon os-icon-windows"></i>', 'order': 20},
    'freebsd': {'name': 'FreeBSD', 'icon': '<i class="fa-brands fa-freebsd os-icon os-icon-freebsd"></i>', 'order': 30},
    'openbsd': {'name': 'OpenBSD', 'icon': '<i class="fa-brands fa-linux os-icon os-icon-openbsd"></i>', 'order': 31},
    'other': {'name': 'Other', 'icon': '<i class="fa-solid fa-compact-disc os-icon os-icon-other"></i>', 'order': 99},
}


def detect_os_group(template_name: str) -> dict:
    """Определить группу ОС по имени шаблона"""
    name_lower = template_name.lower()
    
    for keyword, group_info in OS_GROUP_MAPPING.items():
        if keyword in name_lower:
            return group_info
    
    return OS_GROUP_MAPPING['other']


@router.post("/api/auto-import/{server_id}")
def auto_import_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.manage"))
):
    """
    Автоматический импорт шаблонов с Proxmox сервера.
    Автоматически определяет группу ОС и назначает порядок сортировки.
    Глобальная сортировка: Ubuntu 0-3, CentOS 4-5 и т.д.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    
    try:
        # Подключаемся к Proxmox
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
        
        # Фильтруем по ноде если это кластер
        node_filter = None
        if server.hostname and server.hostname != server.ip_address:
            node_filter = server.hostname
        
        # Получаем шаблоны только с этой ноды
        proxmox_templates = client.get_templates(node=node_filter)
        
        # Получаем уже добавленные шаблоны для ЭТОГО сервера
        existing_vmids = set(
            t.vmid for t in db.query(OSTemplate).filter(OSTemplate.server_id == server_id).all()
        )
        
        # Кеш групп: name -> group
        groups_cache = {}
        for group in db.query(OSTemplateGroup).all():
            groups_cache[group.name] = group
        
        # Группируем шаблоны по ОС
        templates_by_group = {}  # group_name -> [templates]
        
        for tpl in proxmox_templates:
            vmid = tpl.get('vmid')
            if vmid in existing_vmids:
                continue  # Пропускаем уже добавленные
            
            template_name = tpl.get('name', f"Template-{vmid}")
            group_info = detect_os_group(template_name)
            group_name = group_info['name']
            
            if group_name not in templates_by_group:
                templates_by_group[group_name] = {
                    'info': group_info,
                    'templates': []
                }
            
            templates_by_group[group_name]['templates'].append({
                'vmid': vmid,
                'name': template_name,
                'node': tpl.get('node'),
                'maxmem': tpl.get('maxmem', 0),
                'maxdisk': tpl.get('maxdisk', 0),
            })
        
        # Создаём группы и шаблоны с ГЛОБАЛЬНОЙ сортировкой
        imported = []
        global_sort_order = 0  # Глобальный счётчик для всех шаблонов
        
        # Сортируем группы по их порядку (Ubuntu=1, Debian=2, CentOS=3...)
        sorted_groups = sorted(
            templates_by_group.items(),
            key=lambda x: x[1]['info']['order']
        )
        
        for group_name, group_data in sorted_groups:
            group_info = group_data['info']
            group_templates = group_data['templates']
            
            # Получаем или создаём группу
            if group_name not in groups_cache:
                # Получаем максимальный sort_order для групп
                max_group_order = db.query(OSTemplateGroup).count()
                
                new_group = OSTemplateGroup(
                    name=group_name,
                    icon=group_info['icon'],
                    description=f"Шаблоны {group_name}",
                    sort_order=group_info['order'],  # Используем порядок из маппинга
                    is_active=True
                )
                db.add(new_group)
                db.flush()
                groups_cache[group_name] = new_group
                logger.info(f"Created new group: {group_name}")
            
            group = groups_cache[group_name]
            
            # Сортируем шаблоны по имени для консистентности
            group_templates.sort(key=lambda x: x['name'])
            
            # Добавляем шаблоны с ГЛОБАЛЬНЫМ sort_order
            for idx, tpl in enumerate(group_templates):
                # Определяем дефолтные значения из размера шаблона
                default_memory = max(1024, (tpl.get('maxmem', 0) // (1024 * 1024)) or 1024)
                default_disk = max(10, (tpl.get('maxdisk', 0) // (1024 * 1024 * 1024)) or 10)
                
                template = OSTemplate(
                    group_id=group.id,
                    server_id=server_id,
                    name=tpl['name'],
                    vmid=tpl['vmid'],
                    node=tpl.get('node'),
                    source_node=tpl.get('node'),
                    default_cores=2,
                    default_memory=default_memory,
                    default_disk=default_disk,
                    min_cores=1,
                    min_memory=512,
                    min_disk=5,
                    is_active=True,
                    sort_order=global_sort_order  # ГЛОБАЛЬНАЯ сортировка
                )
                
                db.add(template)
                imported.append({
                    'vmid': tpl['vmid'],
                    'name': tpl['name'],
                    'group': group_name,
                    'sort_order': global_sort_order
                })
                global_sort_order += 1  # Увеличиваем глобальный счётчик
        
        db.commit()
        
        logger.info(f"User {current_user.username} auto-imported {len(imported)} templates from {server.name}")
        
        return JSONResponse(content={
            'success': True,
            'server_id': server_id,
            'server_name': server.name,
            'imported_count': len(imported),
            'templates': imported,
            'groups_created': [g for g in templates_by_group.keys() if g not in groups_cache]
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error auto-importing templates from {server.name}: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка импорта шаблонов: {str(e)}")


# ==================== VM Deployment ====================

@router.post("/api/deploy", response_model=VMDeployResponse)
async def deploy_vm_from_template(
    deploy_data: VMDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.create"))
):
    """
    Развернуть новую VM из шаблона.
    Клонирует шаблон и настраивает параметры VM.
    Поддерживает развертывание на любую ноду кластера (cross-node deployment).
    """
    # Get template
    template = db.query(OSTemplate).filter(OSTemplate.id == deploy_data.template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    
    if not template.is_active:
        raise HTTPException(status_code=400, detail="Шаблон неактивен")
    
    # Get server
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == template.server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    
    # Validate resources
    cores = deploy_data.cores or template.default_cores
    memory = deploy_data.memory or template.default_memory
    disk = deploy_data.disk or template.default_disk
    
    if cores < template.min_cores:
        raise HTTPException(status_code=400, detail=f"Минимум {template.min_cores} ядер CPU")
    if memory < template.min_memory:
        raise HTTPException(status_code=400, detail=f"Минимум {template.min_memory} MB памяти")
    if disk < template.min_disk:
        raise HTTPException(status_code=400, detail=f"Минимум {template.min_disk} GB диска")
    
    try:
        # Connect to Proxmox
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
        
        # Determine source node (where template exists) and target node (where to deploy)
        source_node = template.get_source_node()
        target_node = deploy_data.target_node or source_node
        
        # Check if template exists on source node
        template_vmid = template.vmid
        template_status = client.get_vm_status(source_node, template_vmid)
        
        if not template_status:
            # Try to find the template on other nodes
            found = client.find_template_on_nodes(template_vmid)
            if found:
                source_node = found['node']
                logger.info(f"Template {template_vmid} found on alternative node: {source_node}")
            else:
                raise HTTPException(
                    status_code=404, 
                    detail=f"Шаблон VMID {template_vmid} не найден. Проверьте настройки шаблона."
                )
        
        # For shared storage: template is accessible from all nodes
        # For local storage: template needs to be on the target node or replicated
        # 
        # In a cluster with shared storage, we can clone from source_node 
        # but specify target_node for the new VM
        clone_from_node = source_node
        clone_template_vmid = template_vmid
        
        # Log cross-node deployment
        if target_node and target_node != source_node:
            logger.info(f"Cross-node deployment: template on {source_node}, target node {target_node}")
        
        # Get next VMID from database (range 10000-19999) or use provided VMID
        if deploy_data.vmid:
            # Используем указанный VMID (для переустановки)
            new_vmid = deploy_data.vmid
            logger.info(f"Using provided VMID: {new_vmid} for reinstall")
        else:
            # Генерируем новый случайный VMID для новой VM
            new_vmid = get_next_vmid(db, server.id)
            logger.info(f"Generated new VMID: {new_vmid} for server {server.id}")
        
        # Clone VM from template
        # For shared storage: clone from source node, target specifies where VM will run
        clone_upid = client.clone_vm_from_template(
            node=clone_from_node,
            template_vmid=clone_template_vmid,
            new_vmid=new_vmid,
            name=deploy_data.name,
            full_clone=True,
            description=f"Deployed from template {template.name} by {current_user.username}",
            target_storage=deploy_data.target_storage,
            target_node=target_node if target_node != clone_from_node else None
        )
        
        if not clone_upid:
            raise HTTPException(status_code=500, detail="Ошибка клонирования VM")
        
        # Wait for clone to complete
        clone_success = client.wait_for_task(clone_from_node, clone_upid, timeout=300)
        if not clone_success:
            raise HTTPException(status_code=500, detail="Таймаут клонирования VM")
        
        # Use target_node for all subsequent operations
        deploy_node = target_node
        
        # IPAM Integration - Auto-allocate IP if network specified but no IP provided
        allocated_ip = deploy_data.ip_address
        ipam_allocation = None
        
        if deploy_data.ipam_network_id and not deploy_data.ip_address:
            ipam = IPAMService(db)
            ipam_allocation, error = ipam.auto_allocate_ip(
                network_id=deploy_data.ipam_network_id,
                pool_id=deploy_data.ipam_pool_id,
                resource_type='vm',
                resource_name=deploy_data.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=deploy_node,
                allocated_by=current_user.username,
                notes=f"Auto-allocated for VM deployment from template {template.name}"
            )
            if error:
                logger.error(f"IPAM auto-allocation failed: {error}")
                # Continue without IPAM - let user know
            else:
                allocated_ip = ipam_allocation.ip_address
                logger.info(f"IPAM allocated IP {allocated_ip} for VM {deploy_data.name}")
                # Get gateway from network if not provided
                if not deploy_data.gateway:
                    from ..models import IPAMNetwork
                    network = db.query(IPAMNetwork).filter(IPAMNetwork.id == deploy_data.ipam_network_id).first()
                    if network and network.gateway:
                        deploy_data.gateway = network.gateway
        elif deploy_data.ip_address and deploy_data.ipam_network_id:
            # Register manually specified IP in IPAM
            ipam = IPAMService(db)
            ipam_allocation, error = ipam.allocate_ip(
                ip_address=deploy_data.ip_address,
                network_id=deploy_data.ipam_network_id,
                pool_id=deploy_data.ipam_pool_id,
                resource_type='vm',
                resource_name=deploy_data.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=deploy_node,
                allocated_by=current_user.username,
                notes=f"Manually assigned for VM deployment from template {template.name}"
            )
            if error:
                logger.warning(f"IPAM registration failed for manually specified IP: {error}")
        
        # Configure VM
        ip_config = None
        if allocated_ip:
            if deploy_data.gateway:
                ip_config = f"ip={allocated_ip}/24,gw={deploy_data.gateway}"
            else:
                ip_config = f"ip={allocated_ip}/24"
        elif allocated_ip is None:
            ip_config = "ip=dhcp"
        
        # Combine user's SSH key with deploy request SSH keys
        ssh_keys = deploy_data.ssh_keys or ""
        if current_user.ssh_public_key:
            if ssh_keys:
                ssh_keys = f"{ssh_keys}\n{current_user.ssh_public_key}"
            else:
                ssh_keys = current_user.ssh_public_key
        
        config_success = client.configure_vm(
            node=deploy_node,
            vmid=new_vmid,
            cores=cores,
            memory=memory,
            disk_size=disk,  # configure_vm will compare with current disk size
            network_bridge=deploy_data.network_bridge,
            cloud_init_user=deploy_data.cloud_init_user,
            cloud_init_password=deploy_data.cloud_init_password,
            ssh_keys=ssh_keys if ssh_keys else None,
            ip_config=ip_config,
            onboot=deploy_data.onboot
        )
        
        if not config_success:
            logger.warning(f"VM {new_vmid} created but configuration may be incomplete")
        
        # Start VM if requested
        if deploy_data.start_after_create:
            client.start_vm(deploy_node, new_vmid)
        
        # Enable High Availability if requested (cluster only)
        ha_enabled = False
        if deploy_data.enable_ha:
            try:
                if client.is_cluster():
                    ha_result = client.add_to_ha(vmid=new_vmid, vm_type='vm', max_restart=3, max_relocate=3)
                    if ha_result.get('success'):
                        ha_enabled = True
                        logger.info(f"HA enabled for VM {new_vmid}")
                    else:
                        logger.warning(f"Failed to enable HA for VM {new_vmid}: {ha_result.get('error')}")
                else:
                    logger.warning(f"HA requested but server {server.name} is not in a cluster")
            except Exception as ha_error:
                logger.error(f"Error enabling HA for VM {new_vmid}: {ha_error}")
        
        # Save VM configuration to database with owner
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
            ip_prefix=24,
            gateway=deploy_data.gateway,
            cloud_init_user=deploy_data.cloud_init_user,
            cloud_init_password=deploy_data.cloud_init_password,
            ssh_keys=deploy_data.ssh_keys,
            template_id=template.id,
            template_name=template.name,
            description=f"Deployed from template {template.name}",
            owner_id=current_user.id  # VPS-style user isolation
        )
        
        logger.info(f"User {current_user.username} deployed VM {new_vmid} ({deploy_data.name}) from template {template.name}")
        
        # Log VM deployment to audit
        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="vm",
            resource_id=new_vmid,
            username=current_user.username,
            resource_name=deploy_data.name,
            server_id=server.id,
            server_name=server.name,
            node_name=deploy_node,
            details={
                "template_id": template.id,
                "template_name": template.name,
                "source_node": source_node if source_node != deploy_node else None,
                "cores": cores,
                "memory": memory,
                "disk": disk,
                "ip_address": allocated_ip,
                "ha_enabled": ha_enabled
            },
            success=True
        )
        
        return VMDeployResponse(
            success=True,
            vmid=new_vmid,
            name=deploy_data.name,
            node=deploy_node,
            server_id=server.id,
            task_upid=clone_upid,
            message=f"VM {deploy_data.name} успешно создана (VMID: {new_vmid})"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        # Log failed deployment
        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="vm",
            resource_id=deploy_data.vmid,
            username=current_user.username,
            resource_name=deploy_data.name,
            server_id=server.id if server else None,
            server_name=server.name if server else None,
            node_name=deploy_node if 'deploy_node' in locals() else None,
            details={
                "template_id": template.id if template else None,
                "template_name": template.name if template else None
            },
            success=False,
            error_message=str(e)
        )
        logger.error(f"Error deploying VM from template {template.id}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка развертывания VM: {str(e)}")


# ==================== Quick Deploy (for UI) ====================

@router.get("/api/quick-deploy/{template_id}")
def get_quick_deploy_info(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.view"))
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
