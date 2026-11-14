import time
import ipaddress
import urllib3
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional, Union, Any
from proxmoxer import ProxmoxAPI, AuthenticationError
from loguru import logger
import threading
from functools import lru_cache
from contextlib import asynccontextmanager
import base64

from .net_parser import parse_guest_nics
from .mixins.vm import VmMixin
from .mixins.lxc import LxcMixin
from .mixins.cluster import ClusterMixin
from .mixins.storage import StorageMixin
from .mixins.network import NetworkMixin
from .mixins.snapshot import SnapshotMixin
from .mixins.firewall import FirewallMixin
from .mixins.pools import PoolsMixin
from .mixins.disks import DisksMixin
from .mixins.access import AccessMixin
from .mixins.node_admin import NodeAdminMixin

proxmox_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="proxmox_")

connection_cache = {}
connection_cache_lock = threading.Lock()
MAX_CACHE_SIZE = 50

async def _run_in_executor(func, *args, **kwargs):
    """Run a blocking function in the proxmox thread pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(proxmox_executor, lambda: func(*args, **kwargs))

# qemu-guest-agent в образах RHEL-семейства (Rocky Linux, AlmaLinux, CentOS
# Stream, Fedora) собран с урезанным набором RPC. Ограничение задаётся в
# /etc/sysconfig/qemu-ga, откуда systemd-юнит берёт аргументы агента:
# FILTER_RPC_ARGS с allow-list без guest-exec (актуальные пакеты) либо
# BLOCK_RPCS / BLACKLIST_RPC с block-list (пакеты постарше). Proxmox отдаёт
# это как "The command guest-exec has been disabled for this instance" — по
# такому тексту не догадаться, что и где чинить, поэтому дописываем инструкцию.
def agent_error_hint(error: str) -> str:
    """Дополнить ошибку guest agent подсказкой, если в госте запрещён RPC."""
    low = (error or '').lower()
    if 'has been disabled' in low and 'guest-exec' in low:
        return (
            f'{error}. В гостевой ОС запрещён RPC guest-exec — так по умолчанию '
            'собран qemu-guest-agent в Rocky Linux, AlmaLinux, CentOS Stream и '
            'Fedora. Внутри ВМ очистите в /etc/sysconfig/qemu-ga значение '
            'FILTER_RPC_ARGS (в пакетах постарше — BLOCK_RPCS или '
            'BLACKLIST_RPC) и выполните systemctl restart qemu-guest-agent'
        )
    return error


def get_proxmox_resources(host: str, user: str = "root@pam", 
                         password: str = None, token_name: str = None, 
                         token_value: str = None, verify_ssl: bool = False,
                         node: str = None, timeout: int = 10) -> Dict[str, List[Dict]]:
    """
    Вспомогательная функция для получения всех ресурсов Proxmox
    
    Args:
        node: Имя ноды для фильтрации (если None, получить со всех нод)
        timeout: Connection timeout in seconds
    
    Returns:
        Dict с ключами 'vms' и 'containers'
    """
    client = ProxmoxClient(host, user, password, token_name, token_value, verify_ssl, timeout=timeout)
    return client.get_all_resources(node)

def cleanup_expired_connections():
    """Cleanup expired connections from cache"""
    current_time = time.time()
    expired_keys = []
    
    for key, data in connection_cache.items():
        if current_time - data['created'] > 3600:  # 1 hour
            expired_keys.append(key)
    
    for key in expired_keys:
        del connection_cache[key]
        logger.debug(f"Removed expired Proxmox connection: {key}")

def clear_server_cache(host: str):
    """Clear all cached connections for a specific server"""
    keys_to_remove = [key for key in connection_cache.keys() if key.startswith(f"{host}:")]
    for key in keys_to_remove:
        del connection_cache[key]
        logger.info(f"Cleared cache for server: {key}")

def clear_all_cache():
    """Clear entire connection cache"""
    connection_cache.clear()
    logger.info("Cleared all Proxmox connection cache")

@lru_cache(maxsize=32)
def get_cached_client(host: str, user: str, password_hash: str = None,
                      token_name: str = None, token_value_hash: str = None) -> 'ProxmoxClient':
    """Get cached Proxmox client instance"""
    return ProxmoxClient(
        host=host,
        user=user,
        password=password_hash,
        token_name=token_name,
        token_value=token_value_hash
    )

class ProxmoxClient(VmMixin, LxcMixin, ClusterMixin, StorageMixin, NetworkMixin, SnapshotMixin, FirewallMixin, PoolsMixin, DisksMixin, AccessMixin, NodeAdminMixin):
        @classmethod
        def from_server(cls, server) -> "ProxmoxClient":
            """Single source of truth for building a client from a ProxmoxServer model.

            Picks token auth when available, otherwise password auth, and appends a
            non-default port to the host. Callers across the API and services layers
            (see api/proxmox/_helpers.py and services/task_queue_service.py) should
            go through here instead of duplicating the host/auth branching.
            """
            host = server.ip_address or getattr(server, "hostname", "") or ""
            if getattr(server, "port", None) and server.port != 8006:
                host = f"{host}:{server.port}"

            if getattr(server, "use_password", False) and getattr(server, "password", ""):
                return cls(
                    host=host,
                    user=server.api_user,
                    password=server.password,
                    verify_ssl=server.verify_ssl,
                )
            client = cls(
                host=host,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl,
            )
            # API calls go through the token, but a password kept from token
            # auto-provisioning is still needed for root SSH to the node
            # (pct exec/push in App Store, golden template build, pvecm delnode).
            # Without it SSH had no credentials at all and failed with
            # "No authentication methods available".
            client._password = getattr(server, "password", None) or None
            return client

        def __init__(self, host: str, user: str = "root@pam", password: str = None,
                     token_name: str = None, token_value: str = None, verify_ssl: bool = True,
                     timeout: int = 30):
            """
            Инициализация Proxmox клиента
            
            Args:
                host: IP адрес или hostname Proxmox сервера
                user: Пользователь (например root@pam)
                password: Пароль (для password auth)
                token_name: Имя API токена (для token auth)
                token_value: Значение API токена (для token auth)
                verify_ssl: Проверять SSL сертификат
                timeout: Таймаут подключения в секундах
            """
            self.host = host
            self.user = user
            self.timeout = timeout
            # Stored for SSH fallbacks (e.g. pvecm delnode) that need root password auth
            self._password = password
            # Улучшенный ключ кеша - включаем token_name для уникальности
            self.connection_key = f"{host}:{user}:{token_name or 'password'}"
            self.proxmox = None
            self.last_used = time.time()
            # Причина неудачного подключения: 'auth' | 'connection' | None.
            # Нужна, чтобы отличить протухший токен от недоступной ноды.
            self.connect_error_kind = None
            self.connect_error = None
            
            # Check cache first
            with connection_cache_lock:
                if self.connection_key in connection_cache:
                    cached_client = connection_cache[self.connection_key]
                    if time.time() - cached_client['created'] < 3600:  # 1 hour cache
                        self.proxmox = cached_client['client']
                        logger.debug(f"Using cached Proxmox connection for {host}")
                        # Обновляем время последнего использования
                        cached_client['created'] = time.time()
                        return
                    else:
                        # Remove expired connection
                        del connection_cache[self.connection_key]
                        logger.debug(f"Removed expired cache for {self.connection_key}")
            
            try:
                if token_name and token_value:
                    # Аутентификация через API токен (рекомендуется)
                    self.proxmox = ProxmoxAPI(
                        host,
                        user=user,
                        token_name=token_name,
                        token_value=token_value,
                        verify_ssl=verify_ssl,
                        timeout=timeout
                    )
                    logger.info(f"Connected to Proxmox {host} using API token")
                elif password:
                    # Аутентификация через пароль
                    self.proxmox = ProxmoxAPI(
                        host,
                        user=user,
                        password=password,
                        verify_ssl=verify_ssl,
                        timeout=timeout
                    )
                    logger.info("Connected to Proxmox using password")
                else:
                    logger.error("Необходимо указать либо password, либо token")
                    return
                
                # Test connection
                if self.proxmox:
                    try:
                        self.proxmox.version.get()
                        # Cache successful connection
                        with connection_cache_lock:
                            if len(connection_cache) >= MAX_CACHE_SIZE:
                                oldest_key = min(connection_cache.keys(), key=lambda k: connection_cache[k]['created'])
                                del connection_cache[oldest_key]
                            connection_cache[self.connection_key] = {
                                'client': self.proxmox,
                                'created': time.time()
                            }
                        logger.debug(f"Cached connection for {self.connection_key}")
                    except Exception as e:
                        logger.error(f"Failed to test Proxmox connection for {host}: {e}")
                        # При токен-авторизации 401 прилетает не AuthenticationError,
                        # а ResourceException со status_code — proxmoxer не делает
                        # отдельный логин, токен проверяется на первом же запросе.
                        self.connect_error_kind = (
                            'auth' if getattr(e, 'status_code', None) == 401 else 'connection'
                        )
                        self.connect_error = str(e)
                        self.proxmox = None
                        # Удаляем неудачное подключение из кеша
                        with connection_cache_lock:
                            if self.connection_key in connection_cache:
                                del connection_cache[self.connection_key]
                        
            except AuthenticationError as e:
                logger.error(f"Ошибка аутентификации Proxmox {host}: {e}")
                self.connect_error_kind = 'auth'
                self.connect_error = str(e)
                self.proxmox = None
                # Удаляем из кеша при ошибке аутентификации
                with connection_cache_lock:
                    if self.connection_key in connection_cache:
                        del connection_cache[self.connection_key]
            except Exception as e:
                logger.error(f"Ошибка подключения к Proxmox {host}: {e}")
                self.connect_error_kind = (
                    'auth' if getattr(e, 'status_code', None) == 401 else 'connection'
                )
                self.connect_error = str(e)
                self.proxmox = None
                # Удаляем из кеша при ошибке подключения
                if self.connection_key in connection_cache:
                    del connection_cache[self.connection_key]

        def is_connected(self) -> bool:
            """Check if client is properly connected"""
            if not self.proxmox:
                return False
            try:
                self.proxmox.version.get()
                self.last_used = time.time()
                return True
            except Exception:
                return False

        def check_connection(self) -> tuple:
            """
            Проверить API, отличая отказ авторизации от недоступности ноды.

            «Нода не отвечает» и «нода ответила 401» чинятся по-разному: первое —
            сеть/питание, второе — протухший токен (например, после ввода ноды в
            кластер, когда user.cfg и priv/token.cfg замещаются кластерными).

            Returns:
                (ok, error_kind, error) — error_kind: None | 'auth' | 'connection'
            """
            if not self.proxmox:
                return (False, self.connect_error_kind or 'connection',
                        self.connect_error or 'Failed to connect')
            try:
                self.proxmox.version.get()
                self.last_used = time.time()
                return (True, None, None)
            except AuthenticationError as e:
                return (False, 'auth', str(e))
            except Exception as e:
                kind = 'auth' if getattr(e, 'status_code', None) == 401 else 'connection'
                return (False, kind, str(e))

        @asynccontextmanager
        async def ensure_connection(self):
            """Ensure connection is active before use"""
            if not self.is_connected():
                logger.warning(f"Lost connection to Proxmox {self.host}, attempting reconnect")
                # Remove from cache and try to reconnect
                if self.connection_key in connection_cache:
                    del connection_cache[self.connection_key]
            yield self.proxmox

        def get_nodes(self) -> List[Dict]:
            """Получить список нод кластера"""
            if not self.proxmox:
                return []
            
            try:
                nodes = self.proxmox.nodes.get()
                return nodes
            except Exception as e:
                logger.error(f"Ошибка получения списка нод {self.host}: {e}")
                return []

        def get_vms(self, node: str = None) -> List[Dict]:
            """
            Получить список виртуальных машин (QEMU)
            
            Args:
                node: Имя ноды (если None, получить со всех нод)
            
            Returns:
                Список VM с информацией
            """
            if not self.proxmox:
                logger.warning(f"Cannot get VMs from {self.host}: proxmox client is None")
                return []
            
            vms = []
            
            try:
                if node:
                    nodes = [{'node': node}]
                else:
                    nodes = self.get_nodes()
                
                for n in nodes:
                    node_name = n.get('node')
                    try:
                        qemu_vms = self.proxmox.nodes(node_name).qemu.get()
                        for vm in qemu_vms:
                            vm['node'] = node_name
                            vm['type'] = 'qemu'
                            vms.append(vm)
                    except Exception as e:
                        logger.error(f"Ошибка получения VM с ноды {node_name}: {e}")
            except Exception as e:
                logger.error(f"Ошибка получения списка VM {self.host}: {e}")
            
            return vms

        def get_containers(self, node: str = None) -> List[Dict]:
            """
            Получить список LXC контейнеров
            
            Args:
                node: Имя ноды (если None, получить со всех нод)
            
            Returns:
                Список LXC контейнеров с информацией
            """
            if not self.proxmox:
                logger.warning(f"Cannot get containers from {self.host}: proxmox client is None")
                return []
            
            containers = []
            
            try:
                if node:
                    nodes = [{'node': node}]
                else:
                    nodes = self.get_nodes()
                
                for n in nodes:
                    node_name = n.get('node')
                    try:
                        lxc_containers = self.proxmox.nodes(node_name).lxc.get()
                        for ct in lxc_containers:
                            ct['node'] = node_name
                            ct['type'] = 'lxc'
                            containers.append(ct)
                    except Exception as e:
                        logger.error(f"Ошибка получения LXC с ноды {node_name}: {e}")
            except Exception as e:
                logger.error(f"Ошибка получения списка LXC {self.host}: {e}")
            
            return containers

        def get_all_resources(self, node: str = None) -> Dict[str, List[Dict]]:
            """Получить все ресурсы (VM + LXC)
            
            Args:
                node: Имя ноды для фильтрации (если None, получить со всех нод)
            """
            return {
                'vms': self.get_vms(node),
                'containers': self.get_containers(node)
            }

        def get_cluster_resources(self, type_: Optional[str] = None) -> List[Dict]:
            """Получить живые метрики со всех VM/LXC одним запросом через /cluster/resources.
    
            Args:
                type_: 'vm' для VM/LXC, 'node' для нод, None для всех
            Returns:
                Список ресурсов с полями cpu, mem, maxmem, disk, maxdisk, uptime, netin, netout, status и т.д.
            """
            if not self.proxmox:
                return []
            try:
                if type_:
                    return self.proxmox.cluster.resources.get(type=type_) or []
                return self.proxmox.cluster.resources.get() or []
            except Exception as e:
                logger.error(f"Ошибка получения cluster/resources с {self.host}: {e}")
                return []

        def get_node_status(self, node: str) -> Optional[Dict]:
            """
            Получить статус ноды Proxmox (CPU, память, uptime и т.д.)
            
            Args:
                node: Имя ноды
            
            Returns:
                Dict с данными о статусе ноды
            """
            if not self.proxmox:
                return None
            
            try:
                status = self.proxmox.nodes(node).status.get()
                return status
            except Exception as e:
                logger.error(f"Ошибка получения статуса ноды {node}: {e}")
                return None

        def get_node_rrddata(self, node: str, timeframe: str = "hour") -> Dict:
            """
            Получить исторические данные ноды для графиков
            
            Args:
                node: Имя ноды
                timeframe: Временной диапазон (hour, day, week, month, year)
            
            Returns:
                Dict с массивом данных RRD
            """
            if not self.proxmox:
                return {"data": []}
            
            try:
                rrddata = self.proxmox.nodes(node).rrddata.get(timeframe=timeframe)
                return {"data": rrddata}
            except Exception as e:
                logger.error(f"Ошибка получения RRD данных ноды {node}: {e}")
                return {"data": []}

        def get_templates(self, node: str = None) -> List[Dict]:
            """
            Получить список шаблонов VM (template=1)
            
            Args:
                node: Имя ноды (если None, получить со всех нод)
            
            Returns:
                Список VM-шаблонов
            """
            if not self.proxmox:
                return []
            
            templates = []
            
            try:
                if node:
                    nodes = [{'node': node}]
                else:
                    nodes = self.get_nodes()
                
                for n in nodes:
                    node_name = n.get('node')
                    try:
                        qemu_vms = self.proxmox.nodes(node_name).qemu.get()
                        for vm in qemu_vms:
                            # Проверяем, является ли VM шаблоном
                            if vm.get('template') == 1:
                                vm['node'] = node_name
                                vm['type'] = 'qemu'
                                templates.append(vm)
                    except Exception as e:
                        logger.error(f"Ошибка получения шаблонов с ноды {node_name}: {e}")
                    try:
                        lxc_cts = self.proxmox.nodes(node_name).lxc.get()
                        for ct in lxc_cts:
                            # LXC контейнер-шаблон (template=1)
                            if ct.get('template') in (1, '1', True):
                                ct['node'] = node_name
                                ct['type'] = 'lxc'
                                templates.append(ct)
                    except Exception as e:
                        logger.error(f"Ошибка получения LXC-шаблонов с ноды {node_name}: {e}")
                    # vztmpl файлы из storage (стандартные LXC шаблоны)
                    try:
                        storages = self.proxmox.nodes(node_name).storage.get()
                        seen_volids = set()
                        for stor in storages:
                            stor_name = stor.get('storage')
                            stor_content = stor.get('content', '') or ''
                            if 'vztmpl' not in stor_content:
                                continue
                            try:
                                items = self.proxmox.nodes(node_name).storage(stor_name).content.get(content='vztmpl')
                            except Exception as e:
                                logger.warning(f"Не удалось прочитать vztmpl из {stor_name} на {node_name}: {e}")
                                continue
                            for item in items:
                                volid = item.get('volid')
                                if not volid or volid in seen_volids:
                                    continue
                                seen_volids.add(volid)
                                # Имя файла без 'storage:vztmpl/'
                                fname = volid.split('/', 1)[-1] if '/' in volid else volid
                                # Убираем расширения .tar.zst / .tar.gz / .tar.xz
                                display_name = fname
                                for ext in ('.tar.zst', '.tar.gz', '.tar.xz', '.tar'):
                                    if display_name.endswith(ext):
                                        display_name = display_name[: -len(ext)]
                                        break
                                templates.append({
                                    'vmid': None,
                                    'volid': volid,
                                    'name': display_name,
                                    'node': node_name,
                                    'type': 'lxc',
                                    'is_file_template': True,
                                    'maxmem': 0,
                                    'maxdisk': int(item.get('size') or 0),
                                    'storage': stor_name,
                                })
                    except Exception as e:
                        logger.error(f"Ошибка получения vztmpl с ноды {node_name}: {e}")
            except Exception as e:
                logger.error(f"Ошибка получения списка шаблонов {self.host}: {e}")
            
            return templates

        def get_storages(self, node: str) -> List[Dict]:
            """
            Получить список хранилищ на ноде
            
            Args:
                node: Имя ноды
            
            Returns:
                Список доступных хранилищ
            """
            if not self.proxmox:
                return []
            
            try:
                storages = self.proxmox.nodes(node).storage.get()
                return storages
            except Exception as e:
                logger.error(f"Ошибка получения хранилищ ноды {node}: {e}")
                return []

        async def async_get_nodes(self) -> List[Dict]:
            """Non-blocking version of get_nodes()."""
            return await _run_in_executor(self.get_nodes)

        async def async_get_vms(self, node: str = None) -> List[Dict]:
            """Non-blocking version of get_vms()."""
            return await _run_in_executor(self.get_vms, node)

        async def async_get_containers(self, node: str = None) -> List[Dict]:
            """Non-blocking version of get_containers()."""
            return await _run_in_executor(self.get_containers, node)

        async def async_get_node_status(self, node: str) -> Optional[Dict]:
            """Non-blocking version of get_node_status()."""
            return await _run_in_executor(self.get_node_status, node)

        async def async_get_vm_status(self, node: str, vmid: int) -> Optional[Dict]:
            """Non-blocking version of get_vm_status()."""
            return await _run_in_executor(self.get_vm_status, node, vmid)

        async def async_get_container_status(self, node: str, vmid: int) -> Optional[Dict]:
            """Non-blocking version of get_container_status()."""
            return await _run_in_executor(self.get_container_status, node, vmid)

        async def async_get_storages(self, node: str) -> List[Dict]:
            """Non-blocking version of get_storages()."""
            return await _run_in_executor(self.get_storages, node)

        async def async_get_all_resources(self, node: str = None) -> Dict[str, List[Dict]]:
            """Non-blocking version of get_all_resources()."""
            return await _run_in_executor(self.get_all_resources, node)

        def get_next_vmid(self) -> Optional[int]:
            """
            Получить следующий свободный VMID
    
            Returns:
                Свободный VMID или None
            """
            if not self.proxmox:
                return None
            
            try:
                vmid = self.proxmox.cluster.nextid.get()
                return int(vmid)
            except Exception as e:
                logger.error(f"Ошибка получения следующего VMID: {e}")
                return None

        def clone_vm_from_template(
            self,
            node: str,
            template_vmid: int,
            new_vmid: int,
            name: str,
            full_clone: bool = True,
            target_storage: str = None,
            target_node: str = None,
            description: str = None
        ) -> Optional[str]:
            """
            Клонировать VM из шаблона
            
            Args:
                node: Имя ноды где находится шаблон
                template_vmid: VMID шаблона
                new_vmid: VMID новой VM
                name: Имя новой VM
                full_clone: Полный клон (True) или linked clone (False)
                target_storage: Целевое хранилище (опционально)
                target_node: Целевая нода для VM (для кросс-нодного деплоя)
                description: Описание VM
            
            Returns:
                UPID задачи или None при ошибке
            """
            if not self.proxmox:
                return None
            
            try:
                params = {
                    'newid': new_vmid,
                    'name': name,
                    'full': 1 if full_clone else 0,
                }
                
                if target_storage:
                    params['storage'] = target_storage
                
                if target_node and target_node != node:
                    params['target'] = target_node
                
                if description:
                    params['description'] = description
                
                result = self.proxmox.nodes(node).qemu(template_vmid).clone.post(**params)
                target_info = f" -> {target_node}" if target_node and target_node != node else ""
                logger.info(f"Клонирование VM {template_vmid} -> {new_vmid} ({name}) на {node}{target_info}")
                return result
            except Exception as e:
                logger.error(f"Ошибка клонирования VM {template_vmid} -> {new_vmid}: {e}")
                return None

        def clone_template_to_node(
            self,
            source_node: str,
            template_vmid: int,
            target_node: str,
            new_vmid: int = None,
            target_storage: str = None
        ) -> Optional[Dict]:
            """
            Клонировать шаблон с одной ноды на другую.
            Используется для репликации шаблонов между нодами кластера.
            
            Args:
                source_node: Исходная нода где находится шаблон
                template_vmid: VMID шаблона на исходной ноде
                target_node: Целевая нода куда клонировать
                new_vmid: VMID для клона на целевой ноде (опционально, будет сгенерирован)
                target_storage: Целевое хранилище на целевой ноде (если не указано - найдёт автоматически)
            
            Returns:
                Dict с upid задачи и new_vmid, или None при ошибке
            """
            if not self.proxmox:
                return None
            
            if source_node == target_node:
                logger.warning(f"Source and target node are the same: {source_node}")
                return None
            
            try:
                # Get next VMID if not provided
                if not new_vmid:
                    new_vmid = self.get_next_vmid()
                    if not new_vmid:
                        logger.error("Failed to get next VMID for template clone")
                        return None
                
                # Get template info to preserve its name
                template_config = self.proxmox.nodes(source_node).qemu(template_vmid).config.get()
                template_name = template_config.get('name', f'template-{template_vmid}')
                
                # Auto-detect target storage if not provided
                # For cross-node cloning with local storage, we MUST specify target storage
                if not target_storage:
                    target_storage = self._get_default_vm_storage(target_node)
                    if target_storage:
                        logger.info(f"Auto-detected target storage: {target_storage} on {target_node}")
                
                params = {
                    'newid': new_vmid,
                    'name': f"{template_name}",
                    'target': target_node,
                    'full': 1,  # Full clone for cross-node
                }
                
                if target_storage:
                    params['storage'] = target_storage
                
                result = self.proxmox.nodes(source_node).qemu(template_vmid).clone.post(**params)
                logger.info(f"Клонирование шаблона {template_vmid} с {source_node} на {target_node} (new vmid: {new_vmid}, storage: {target_storage})")
                
                return {
                    'upid': result,
                    'new_vmid': new_vmid,
                    'target_node': target_node
                }
            except Exception as e:
                logger.error(f"Ошибка клонирования шаблона {template_vmid} на ноду {target_node}: {e}")
                return None

        def _get_default_vm_storage(self, node: str) -> Optional[str]:
            """
            Получить хранилище по умолчанию для VM на ноде.
            Ищет хранилища с поддержкой 'images' (VM дисков).
            
            Returns:
                Имя хранилища или None
            """
            if not self.proxmox:
                return None
            
            try:
                storages = self.proxmox.nodes(node).storage.get()
                
                # Priority: local-lvm > local-zfs > any with 'images' content
                priority_storages = ['local-lvm', 'local-zfs', 'local']
                
                # Filter storages that support VM images
                vm_storages = []
                for s in storages:
                    content = s.get('content', '')
                    if 'images' in content:
                        vm_storages.append(s)
                
                if not vm_storages:
                    logger.warning(f"No VM storage found on node {node}")
                    return None
                
                # Try priority storages first
                for priority in priority_storages:
                    for s in vm_storages:
                        if s.get('storage') == priority and s.get('active', 1):
                            return s.get('storage')
                
                # Return first active storage with images support
                for s in vm_storages:
                    if s.get('active', 1):
                        return s.get('storage')
                
                return vm_storages[0].get('storage') if vm_storages else None
                
            except Exception as e:
                logger.error(f"Error getting default storage for node {node}: {e}")
                return None

        def convert_to_template(self, node: str, vmid: int) -> bool:
            """
            Преобразовать VM в шаблон
            
            Args:
                node: Имя ноды
                vmid: VMID VM
            
            Returns:
                True если успешно
            """
            if not self.proxmox:
                return False
            
            try:
                self.proxmox.nodes(node).qemu(vmid).template.post()
                logger.info(f"VM {vmid} на {node} преобразован в шаблон")
                return True
            except Exception as e:
                logger.error(f"Ошибка преобразования VM {vmid} в шаблон: {e}")
                return False

        def create_vm_with_import(self, node: str, vmid: int, name: str,
                                  memory: int, cores: int,
                                  disk_storage: str, import_volid: str,
                                  bridge: str = 'vmbr0', ostype: str = 'l26') -> Optional[str]:
            """
            Создать ВМ, импортируя готовый диск из скачанного образа (PVE 8.2+).

            Использует синтаксис диска `<storage>:0,import-from=<volid>`, при котором
            Proxmox сам аллоцирует диск нужного размера и конвертирует образ —
            аналог `qm importdisk` без SSH. Добавляет cloud-init drive и serial-консоль.

            Args:
                node: имя ноды
                vmid: VMID новой ВМ
                name: имя ВМ
                memory: память в MB
                cores: число ядер
                disk_storage: хранилище для диска ВМ (content=images)
                import_volid: volid скачанного образа, напр. 'local:import/img.qcow2'
                bridge: сетевой мост
                ostype: тип ОС (l26 — Linux 2.6+/3.x/4.x/5.x/6.x)

            Returns:
                UPID задачи создания/импорта или None
            """
            if not self.proxmox:
                return None
            params = {
                'vmid': vmid,
                'name': name,
                'memory': memory,
                'cores': cores,
                'sockets': 1,
                'cpu': 'host',
                'scsihw': 'virtio-scsi-single',
                'ostype': ostype,
                'scsi0': f'{disk_storage}:0,import-from={import_volid}',
                'ide2': f'{disk_storage}:cloudinit',
                'boot': 'order=scsi0',
                'serial0': 'socket',
                'vga': 'serial0',
                'agent': 'enabled=1',
                'net0': f'virtio,bridge={bridge}',
            }
            # Исключение пробрасываем наверх — воркер отличит «импорт не поддержан»
            result = self.proxmox.nodes(node).qemu.post(**params)
            logger.info(f"Создание ВМ {vmid} ({name}) с импортом {import_volid} на {node}, UPID: {result}")
            return result

        def find_template_on_nodes(self, template_vmid: int, nodes: List[str] = None) -> Optional[Dict]:
            """
            Найти шаблон по VMID на всех нодах кластера
            
            Args:
                template_vmid: VMID искомого шаблона
                nodes: Список нод для поиска (опционально, по умолчанию все ноды)
            
            Returns:
                Dict с node и template info, или None если не найден
            """
            if not self.proxmox:
                return None
            
            try:
                if not nodes:
                    nodes_info = self.get_nodes()
                    nodes = [n.get('node') for n in nodes_info if n.get('node')]
                
                for node in nodes:
                    try:
                        status = self.get_vm_status(node, template_vmid)
                        if status:
                            config = self.proxmox.nodes(node).qemu(template_vmid).config.get()
                            is_template = config.get('template', 0) == 1
                            return {
                                'node': node,
                                'vmid': template_vmid,
                                'name': config.get('name', ''),
                                'is_template': is_template,
                                'status': status
                            }
                    except Exception:
                        continue
                
                return None
            except Exception as e:
                logger.error(f"Ошибка поиска шаблона {template_vmid}: {e}")
                return None

        def replicate_template_to_node(
            self,
            source_node: str,
            template_vmid: int,
            target_node: str,
            target_storage: str = None,
            timeout: int = 600
        ) -> Optional[int]:
            """
            Полная репликация шаблона на другую ноду с ожиданием завершения.
            Клонирует шаблон с тем же VMID и преобразует клон в шаблон на целевой ноде.
            
            Args:
                source_node: Исходная нода
                template_vmid: VMID шаблона (будет сохранён на целевой ноде)
                target_node: Целевая нода
                target_storage: Целевое хранилище
                timeout: Таймаут ожидания в секундах
            
            Returns:
                VMID шаблона на целевой ноде (тот же что и исходный), или None при ошибке
            """
            if not self.proxmox:
                return None
            
            try:
                # Step 1: Clone template to target node with SAME VMID
                clone_result = self.clone_template_to_node(
                    source_node=source_node,
                    template_vmid=template_vmid,
                    target_node=target_node,
                    new_vmid=template_vmid,  # Keep the same VMID
                    target_storage=target_storage
                )
                
                if not clone_result:
                    logger.error("Failed to start template clone")
                    return None
                
                upid = clone_result['upid']
                new_vmid = clone_result['new_vmid']  # Should be same as template_vmid
                
                # Step 2: Wait for clone to complete
                logger.info(f"Waiting for template clone task: {upid}")
                success = self.wait_for_task(source_node, upid, timeout=timeout)
                
                if not success:
                    logger.error(f"Template clone task failed or timed out: {upid}")
                    return None
                
                # Step 3: Convert cloned VM to template
                logger.info(f"Converting cloned VM {new_vmid} to template on {target_node}")
                if not self.convert_to_template(target_node, new_vmid):
                    logger.error(f"Failed to convert VM {new_vmid} to template")
                    # VM created but not a template - still usable
                    return new_vmid
                
                logger.info(f"Successfully replicated template {template_vmid} to {target_node} as {new_vmid}")
                return new_vmid
                
            except Exception as e:
                logger.error(f"Error replicating template to {target_node}: {e}")
                return None

        def wait_for_vm_unlock(self, node: str, vmid: int, timeout: int = 120) -> bool:
            """
            Дождаться снятия блокировки VM (lock: clone/migrate/...)

            После wait_for_task клон может ещё держать блокировку, особенно
            при cross-node клонировании — config.put в этот момент падает.

            Args:
                node: Имя ноды
                vmid: VMID VM
                timeout: Таймаут в секундах

            Returns:
                True если блокировка снята
            """
            import time as time_module

            start_time = time_module.time()
            while time_module.time() - start_time < timeout:
                try:
                    config = self.proxmox.nodes(node).qemu(vmid).config.get()
                    if 'lock' not in config:
                        return True
                    logger.info(f"VM {vmid} заблокирована ({config.get('lock')}), ожидание разблокировки...")
                except Exception as e:
                    # При cross-node клоне VM может ещё не зарегистрироваться на целевой ноде
                    if time_module.time() - start_time > 15:
                        logger.warning(f"VM {vmid} недоступна при ожидании разблокировки: {e}")
                time_module.sleep(2)

            logger.warning(f"Таймаут ожидания разблокировки VM {vmid}")
            return False

        def update_cloud_init(
            self, node: str, vmid: int,
            ciuser=None, cipassword=None, sshkeys=None,
            ipconfig0=None, nameserver=None, searchdomain=None,
        ) -> Dict:
            """Точечно обновить cloud-init параметры существующей VM.

            Значение None — поле не трогаем; пустая строка "" — удалить параметр.
            Пустой cipassword трактуется как «оставить текущий» (blank = keep).
            sshkeys перед отправкой URL-кодируется (требование PVE).
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            import urllib.parse

            params: Dict = {}
            deletes = []

            def _set(key, value, allow_clear=True):
                if value is None:
                    return
                if value == '' and allow_clear:
                    deletes.append(key)
                elif value != '':
                    params[key] = value

            _set('ciuser', ciuser)
            if cipassword:  # пустой пароль не меняем
                params['cipassword'] = cipassword
            if sshkeys is not None:
                if sshkeys.strip() == '':
                    deletes.append('sshkeys')
                else:
                    params['sshkeys'] = urllib.parse.quote(sshkeys, safe='')
            _set('ipconfig0', ipconfig0)
            _set('nameserver', nameserver)
            _set('searchdomain', searchdomain)

            if deletes:
                params['delete'] = ','.join(deletes)
            if not params:
                return {"success": True}

            try:
                self.proxmox.nodes(node).qemu(vmid).config.put(**params)
                logger.info(f"Cloud-init VM {vmid} обновлён: {list(params.keys())}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Ошибка обновления cloud-init VM {vmid}: {e}")
                return {"success": False, "error": str(e)}

        def configure_vm(
            self,
            node: str,
            vmid: int,
            cores: int = None,
            memory: int = None,
            disk_size: int = None,
            disk_storage: str = None,
            network_bridge: str = None,
            cloud_init_user: str = None,
            cloud_init_password: str = None,
            ssh_keys: str = None,
            ip_config: str = None,
            onboot: bool = None
        ) -> Dict:
            """
            Настроить параметры VM после клонирования

            Параметры применяются независимыми группами (compute, network,
            cloudinit, disk), чтобы ошибка одной группы не отменяла остальные.

            Args:
                node: Имя ноды
                vmid: VMID VM
                cores: Количество ядер CPU (итоговое число vCPU, sockets=1)
                memory: Память в MB
                disk_size: Размер диска в GB
                disk_storage: Хранилище диска
                network_bridge: Сетевой мост
                cloud_init_user: Пользователь cloud-init
                cloud_init_password: Пароль cloud-init
                ssh_keys: SSH ключи (public)
                ip_config: Конфигурация IP (например, "ip=dhcp" или "ip=192.168.1.100/24,gw=192.168.1.1")
                onboot: Автозапуск VM при старте хоста

            Returns:
                {"ok": bool, "applied": [группы], "errors": {группа: сообщение}}
            """
            import time as time_module

            result = {"ok": False, "applied": [], "errors": {}}
            if not self.proxmox:
                result["errors"]["connection"] = "Proxmox client not connected"
                return result

            try:
                current_config = dict(self.proxmox.nodes(node).qemu(vmid).config.get())
            except Exception as e:
                logger.warning(f"Не удалось прочитать конфигурацию VM {vmid}: {e}")
                current_config = {}

            def _apply(group: str, params: Dict):
                for attempt in (1, 2):
                    try:
                        self.proxmox.nodes(node).qemu(vmid).config.put(**params)
                        result["applied"].append(group)
                        logger.info(f"VM {vmid}: группа '{group}' применена: {list(params.keys())}")
                        return
                    except Exception as e:
                        if attempt == 1 and 'lock' in str(e).lower():
                            logger.info(f"VM {vmid} заблокирована, повтор группы '{group}' через 5с")
                            time_module.sleep(5)
                            continue
                        result["errors"][group] = str(e)
                        logger.error(f"VM {vmid}: ошибка применения группы '{group}': {e}")
                        return

            # CPU / RAM / автозапуск
            compute_params = {}
            if cores:
                compute_params['cores'] = cores
                # sockets=1 гарантирует, что итоговое число vCPU равно cores
                # независимо от топологии шаблона
                compute_params['sockets'] = 1
            if memory:
                compute_params['memory'] = memory
            if onboot is not None:
                compute_params['onboot'] = 1 if onboot else 0
            if compute_params:
                _apply('compute', compute_params)

            # Сеть: сохраняем модель/MAC/опции из шаблона, меняем только bridge
            if network_bridge:
                existing_net0 = current_config.get('net0')
                if existing_net0:
                    parts = [p for p in str(existing_net0).split(',') if p.strip()]
                    new_parts = []
                    replaced = False
                    for p in parts:
                        if p.startswith('bridge='):
                            new_parts.append(f'bridge={network_bridge}')
                            replaced = True
                        else:
                            new_parts.append(p)
                    if not replaced:
                        new_parts.append(f'bridge={network_bridge}')
                    net0_value = ','.join(new_parts)
                else:
                    net0_value = f'virtio,bridge={network_bridge}'
                _apply('network', {'net0': net0_value})

            # Cloud-init
            ci_params = {}
            if cloud_init_user:
                ci_params['ciuser'] = cloud_init_user
            if cloud_init_password:
                ci_params['cipassword'] = cloud_init_password
            if ssh_keys:
                # SSH keys need URL encoding
                import urllib.parse
                ci_params['sshkeys'] = urllib.parse.quote(ssh_keys, safe='')
            if ip_config:
                ci_params['ipconfig0'] = ip_config
            if ci_params:
                has_ci_drive = any(
                    isinstance(v, str) and 'cloudinit' in v for v in current_config.values()
                )
                # Если конфиг прочитать не удалось — всё равно пробуем применить
                if has_ci_drive or not current_config:
                    _apply('cloudinit', ci_params)
                else:
                    result["errors"]["cloudinit"] = (
                        "шаблон без cloud-init диска — пользователь/пароль/SSH/IP не применены"
                    )
                    logger.warning(f"VM {vmid}: нет cloud-init диска, параметры cloud-init пропущены")

            # Диск
            if disk_size:
                disk_error = self._resize_vm_disk(node, vmid, disk_size)
                if disk_error:
                    result["errors"]["disk"] = disk_error
                    logger.error(f"VM {vmid}: ошибка ресайза диска: {disk_error}")
                else:
                    result["applied"].append('disk')

            result["ok"] = not result["errors"]
            return result

        def _resize_vm_disk(self, node: str, vmid: int, disk_size: int) -> Optional[str]:
            """
            Увеличить основной диск VM до disk_size GB.

            Returns:
                None при успехе (или если ресайз не нужен), иначе текст ошибки
            """
            import re

            def _parse_size_gb(value: str) -> int:
                if 'size=' not in value:
                    return 0
                size_part = value.split('size=')[1].split(',')[0]
                try:
                    if size_part.endswith('T'):
                        return int(float(size_part[:-1]) * 1024)
                    if size_part.endswith('G'):
                        return int(float(size_part[:-1]))
                    if size_part.endswith('M'):
                        return int(float(size_part[:-1])) // 1024
                    return int(size_part) // (1024 ** 3)
                except ValueError:
                    return 0

            try:
                config = self.proxmox.nodes(node).qemu(vmid).config.get()

                candidates = {}
                for key, value in config.items():
                    if not re.match(r'^(scsi|virtio|sata|ide)\d+$', key):
                        continue
                    str_value = str(value)
                    if 'media=cdrom' in str_value.lower() or 'cloudinit' in str_value.lower():
                        continue
                    candidates[key] = str_value

                if not candidates:
                    return f'основной диск VM {vmid} не найден'

                # Предпочитаем загрузочный диск из boot=order=...
                disk_key = None
                boot_match = re.search(r'order=([^,;]+(?:;[^,;]+)*)', str(config.get('boot', '')))
                if boot_match:
                    for dev in boot_match.group(1).split(';'):
                        if dev in candidates:
                            disk_key = dev
                            break
                if not disk_key:
                    disk_key = sorted(candidates)[0]

                current_size_gb = _parse_size_gb(candidates[disk_key])
                if disk_size <= current_size_gb:
                    logger.info(f"Диск {disk_key} VM {vmid} уже имеет размер {current_size_gb}G >= запрошенного {disk_size}G")
                    return None

                resize_upid = self.proxmox.nodes(node).qemu(vmid).resize.put(
                    disk=disk_key,
                    size=f'{disk_size}G'
                )
                if isinstance(resize_upid, str):
                    if not self.wait_for_task(node, resize_upid, timeout=120):
                        status = self.get_task_status(node, resize_upid) or {}
                        reason = status.get('exitstatus') or 'resize task failed'
                        return f'ресайз {disk_key} не удался: {reason}'
                logger.info(f"Диск {disk_key} VM {vmid} изменен с {current_size_gb}G до {disk_size}G")
                return None
            except Exception as e:
                return str(e)

        def get_task_status(self, node: str, upid: str, raise_on_error: bool = False) -> Dict:
            """Get status of a Proxmox task by UPID.

            raise_on_error=True — пробросить исключение вместо возврата {}.
            Нужно вызывающим, которым важно отличать «задача не найдена» от
            транзитной сетевой ошибки (см. run_upid_task_sync): пустой словарь
            эту разницу стирает.
            """
            if not self.proxmox:
                if raise_on_error:
                    raise RuntimeError(f"Proxmox client for {self.host} is not connected")
                return {}
            try:
                status = self.proxmox.nodes(node).tasks(upid).status.get()
                return status
            except Exception as e:
                if raise_on_error:
                    raise
                logger.error(f"Error getting task status {upid}: {e}")
                return {}

        def wait_for_task(self, node: str, upid: str, timeout: int = 300) -> bool:
            """
            Ждать завершения задачи
            
            Args:
                node: Имя ноды
                upid: UPID задачи
                timeout: Таймаут в секундах
            
            Returns:
                True если задача успешно завершена
            """
            import time as time_module
            
            start_time = time_module.time()
            while time_module.time() - start_time < timeout:
                status = self.get_task_status(node, upid)
                if status:
                    if status.get('status') == 'stopped':
                        # Проверяем exitstatus
                        exit_status = status.get('exitstatus')
                        if exit_status != 'OK':
                            logger.error(f"Proxmox task {upid} failed with exitstatus={exit_status!r}")
                        # 'OK' — success; 'WARNINGS: N' — success with warnings (container still created)
                        return exit_status == 'OK' or (isinstance(exit_status, str) and exit_status.startswith('WARNINGS:'))
                time_module.sleep(2)
            
            logger.warning(f"Таймаут ожидания задачи {upid}")
            return False

        def delete_vm(self, node: str, vmid: int, force: bool = False) -> Optional[str]:
            """
            Удалить виртуальную машину
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
                force: Принудительное удаление (остановит VM если запущена)
            
            Returns:
                UPID задачи удаления или True при успехе
            
            Raises:
                Exception: При ошибке удаления
            """
            if not self.proxmox:
                raise Exception("Proxmox client not connected")
            
            # Check if VM is HA managed and remove from HA first
            try:
                ha_status = self.proxmox.nodes(node).qemu(vmid).status.current.get()
                if ha_status and isinstance(ha_status, dict):
                    ha_info = ha_status.get('ha', {})
                    if ha_info.get('managed'):
                        # Remove from HA
                        try:
                            self.proxmox.cluster.ha.resources(f"vm:{vmid}").delete()
                            logger.info(f"Removed VM {vmid} from HA before deletion")
                        except Exception as ha_e:
                            logger.warning(f"Failed to remove VM {vmid} from HA: {ha_e}")
            except Exception as e:
                logger.debug(f"Could not check HA status for VM {vmid}: {e}")
            
            params = {'purge': 1}  # Always purge to handle HA and other resources
            if force:
                params['force'] = 1
            
            result = self.proxmox.nodes(node).qemu(vmid).delete(**params)
            logger.info(f"Запущено удаление VM {vmid} на {node}")
            return result if result else True

        def delete_container(self, node: str, vmid: int, force: bool = False) -> Optional[str]:
            """
            Удалить контейнер (LXC)
            
            Args:
                node: Имя ноды
                vmid: ID контейнера
                force: Принудительное удаление (остановит контейнер если запущен)
            
            Returns:
                UPID задачи удаления или True при успехе
            
            Raises:
                Exception: При ошибке удаления
            """
            if not self.proxmox:
                raise Exception("Proxmox client not connected")
            
            # Check if container is HA managed and remove from HA first
            try:
                ha_status = self.proxmox.nodes(node).lxc(vmid).status.current.get()
                if ha_status and isinstance(ha_status, dict):
                    ha_info = ha_status.get('ha', {})
                    if ha_info.get('managed'):
                        # Remove from HA
                        try:
                            self.proxmox.cluster.ha.resources(f"ct:{vmid}").delete()
                            logger.info(f"Removed container {vmid} from HA before deletion")
                        except Exception as ha_e:
                            logger.warning(f"Failed to remove container {vmid} from HA: {ha_e}")
            except Exception as e:
                logger.debug(f"Could not check HA status for container {vmid}: {e}")
            
            params = {'purge': 1}  # Always purge to handle HA and other resources
            if force:
                params['force'] = 1
            
            result = self.proxmox.nodes(node).lxc(vmid).delete(**params)
            logger.info(f"Запущено удаление контейнера {vmid} на {node}")
            return result if result else True

        def get_vm_config(self, node: str, vmid: int) -> Optional[Dict]:
            """
            Получить конфигурацию виртуальной машины
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
            
            Returns:
                Словарь с конфигурацией VM
            """
            if not self.proxmox:
                return None
            
            try:
                config = self.proxmox.nodes(node).qemu(vmid).config.get()
                return dict(config)
            except Exception as e:
                logger.error(f"Ошибка получения конфигурации VM {vmid}: {e}")
                return None

        def update_vm_config(self, node: str, vmid: int, config: Dict) -> bool:
            """
            Обновить конфигурацию виртуальной машины
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
                config: Словарь с параметрами для обновления
            
            Returns:
                True при успехе
            """
            if not self.proxmox:
                return False
            
            import re

            # Фильтруем разрешенные параметры
            allowed_params = {
                'cores', 'sockets', 'vcpus', 'numa', 'memory', 'balloon', 'name', 'description',
                'cpu', 'cpulimit', 'cpuunits', 'affinity', 'onboot', 'startup', 'boot', 'bootdisk',
                'agent', 'ostype', 'tablet', 'hotplug', 'protection', 'kvm',
                'ciuser', 'cipassword', 'sshkeys', 'tags',
                # Hardware editor: CPU/NUMA, дисплей, контроллер, тип платформы,
                # efidisk0 — EFI-диск, обязателен для загрузки в режиме OVMF (UEFI).
                'numa', 'vga', 'scsihw', 'machine', 'bios', 'efidisk0',
            }
            # Индексированные ключи: сеть/диски + passthrough (PCI/USB) + serial
            indexed_re = re.compile(
                r'^(net|scsi|virtio|ide|sata|ipconfig|hostpci|usb|serial)\d+$'
            )
            # Удаляемые ключи: интерфейсы, passthrough-устройства + опциональные флаги
            delete_token_re = re.compile(
                r'^(net\d+|hostpci\d+|usb\d+|serial\d+|startup|onboot|protection|tags)$'
            )

            filtered_config = {}
            for k, v in config.items():
                if k in ('onboot', 'protection'):
                    filtered_config[k] = 1 if v in (True, 1, '1') else 0
                elif k in allowed_params or indexed_re.match(k):
                    filtered_config[k] = v
            # Удаление ключей: delete='net1' / 'startup' (или 'net1,startup')
            delete_value = config.get('delete')
            if isinstance(delete_value, str) and delete_value and all(
                delete_token_re.match(tok) for tok in delete_value.split(',')
            ):
                filtered_config['delete'] = delete_value

            if not filtered_config:
                logger.warning(f"Нет разрешенных параметров для обновления VM {vmid}")
                return False

            try:
                self.proxmox.nodes(node).qemu(vmid).config.put(**filtered_config)
                logger.info(f"Конфигурация VM {vmid} обновлена: {list(filtered_config.keys())}")
                return True
            except Exception as e:
                logger.error(f"Ошибка обновления конфигурации VM {vmid}: {e}")
                raise RuntimeError(f"Proxmox: {e}") from e

        def get_container_config(self, node: str, vmid: int) -> Optional[Dict]:
            """
            Получить конфигурацию LXC контейнера
            
            Args:
                node: Имя ноды
                vmid: ID контейнера
            
            Returns:
                Словарь с конфигурацией
            """
            if not self.proxmox:
                return None
            
            try:
                config = self.proxmox.nodes(node).lxc(vmid).config.get()
                return dict(config)
            except Exception as e:
                logger.error(f"Ошибка получения конфигурации LXC {vmid}: {e}")
                return None

        def update_container_config(self, node: str, vmid: int, config: Dict) -> bool:
            """
            Обновить конфигурацию LXC контейнера

            Args:
                node: Имя ноды
                vmid: ID контейнера
                config: Словарь с параметрами для обновления

            Returns:
                True при успехе
            """
            if not self.proxmox:
                return False

            import re

            allowed_params = {
                'hostname', 'memory', 'swap', 'cores', 'cpulimit', 'cpuunits',
                'onboot', 'startup', 'protection', 'description', 'nameserver',
                'searchdomain', 'tags',
            }
            net_re = re.compile(r'^net\d+$')
            delete_token_re = re.compile(r'^(net\d+|startup|onboot|protection|tags)$')

            filtered_config = {}
            for k, v in config.items():
                if k in ('onboot', 'protection'):
                    filtered_config[k] = 1 if v in (True, 1, '1') else 0
                elif k in allowed_params or net_re.match(k):
                    filtered_config[k] = v
            delete_value = config.get('delete')
            if isinstance(delete_value, str) and delete_value and all(
                delete_token_re.match(tok) for tok in delete_value.split(',')
            ):
                filtered_config['delete'] = delete_value

            if not filtered_config:
                logger.warning(f"Нет разрешенных параметров для обновления LXC {vmid}")
                return False

            try:
                self.proxmox.nodes(node).lxc(vmid).config.put(**filtered_config)
                logger.info(f"Конфигурация LXC {vmid} обновлена: {list(filtered_config.keys())}")
                return True
            except Exception as e:
                logger.error(f"Ошибка обновления конфигурации LXC {vmid}: {e}")
                raise RuntimeError(f"Proxmox: {e}") from e

        def resize_container_disk(self, node: str, vmid: int, disk: str, size: str) -> bool:
            """
            Изменить размер диска LXC контейнера
            
            Args:
                node: Имя ноды
                vmid: ID контейнера
                disk: Имя диска (rootfs, mp0, mp1, etc)
                size: Размер для добавления (например '+5G')
            
            Returns:
                True при успехе
            """
            if not self.proxmox:
                return False
            
            resize_upid = self.proxmox.nodes(node).lxc(vmid).resize.put(disk=disk, size=size)
            # Проверяем результат задачи: упавший ресайз не должен считаться
            # успешным (см. resize_vm_disk).
            if isinstance(resize_upid, str):
                if not self.wait_for_task(node, resize_upid, timeout=120):
                    status = self.get_task_status(node, resize_upid) or {}
                    reason = status.get('exitstatus') or 'resize task failed'
                    logger.error(f"Ресайз диска {disk} LXC {vmid} провалился: {reason}")
                    raise RuntimeError(f"Proxmox: {reason}")
            logger.info(f"Диск {disk} LXC {vmid} изменен на {size}")
            return True

        def move_container_disk(self, node: str, vmid: int, disk: str, target_storage: str,
                                delete: bool = True) -> Dict:
            """Переместить том LXC контейнера в другое хранилище (POST lxc/{vmid}/move_volume).

            Args:
                disk: имя тома (rootfs, mp0, mp1, ...)
                target_storage: целевое хранилище
                delete: удалить исходный том после копирования
            Returns:
                {"success": bool, "upid"?: str, "error"?: str}
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                params = {"volume": disk, "storage": target_storage, "delete": 1 if delete else 0}
                upid = self.proxmox.nodes(node).lxc(vmid).move_volume.post(**params)
                if isinstance(upid, str):
                    if not self.wait_for_task(node, upid, timeout=3600):
                        status = self.get_task_status(node, upid) or {}
                        reason = status.get("exitstatus") or "move_volume task failed"
                        logger.error(f"Move тома {disk} LXC {vmid} провалился: {reason}")
                        return {"success": False, "error": f"Proxmox: {reason}"}
                logger.info(f"Том {disk} LXC {vmid} перемещён в {target_storage}")
                return {"success": True, "upid": upid if isinstance(upid, str) else None}
            except Exception as e:
                logger.error(f"Ошибка перемещения тома {disk} LXC {vmid}: {e}")
                return {"success": False, "error": str(e)}

        def get_vm_interfaces(self, node: str, vmid: int) -> List[Dict]:
            """
            Получить сетевые интерфейсы VM через QEMU guest agent
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
            
            Returns:
                Список интерфейсов с IP адресами
            """
            if not self.proxmox:
                return []
            
            try:
                # Пытаемся получить информацию через guest agent
                result = self.proxmox.nodes(node).qemu(vmid).agent('network-get-interfaces').get()
                
                interfaces = []
                if result and 'result' in result:
                    for iface in result['result']:
                        if 'ip-addresses' in iface and iface['ip-addresses']:
                            name = iface.get('name', 'unknown')
                            # Пропускаем loopback
                            if name == 'lo':
                                continue
                            
                            ips = []
                            for ip_info in iface['ip-addresses']:
                                ip = ip_info.get('ip-address')
                                ip_type = ip_info.get('ip-address-type', 'unknown')
                                if ip and ip_type in ['ipv4', 'ipv6']:
                                    ips.append({
                                        'address': ip,
                                        'type': ip_type,
                                        'prefix': ip_info.get('prefix', 0)
                                    })
                            
                            if ips:
                                interfaces.append({
                                    'name': name,
                                    'hardware_address': iface.get('hardware-address', ''),
                                    'ips': ips
                                })
                
                return interfaces
            except Exception as e:
                logger.debug(f"Не удалось получить сетевые интерфейсы VM {vmid} (guest agent может быть не установлен): {e}")
                return []

        def get_vm_blockstats(self, node: str, vmid: int) -> str:
            """Raw HMP 'info blockstats' text via the QEMU monitor (VM only).
            Returns '' if unavailable."""
            if not self.proxmox:
                return ""
            try:
                res = self.proxmox.nodes(node).qemu(vmid).monitor.post(command="info blockstats")
                return res if isinstance(res, str) else str(res or "")
            except Exception as e:
                logger.debug(f"blockstats unavailable for VM {vmid} on {node}: {e}")
                return ""

        def get_node_netstat(self, node: str) -> list:
            """Per-tap/veth interface counters for the node ([] on failure)."""
            if not self.proxmox:
                return []
            try:
                res = self.proxmox.nodes(node).netstat.get()
                return res if isinstance(res, list) else []
            except Exception as e:
                logger.debug(f"netstat unavailable for node {node}: {e}")
                return []

        def get_container_interfaces(self, node: str, vmid: int,
                                     include_live: bool = True) -> List[Dict]:
            """
            Получить сетевые интерфейсы контейнера с их IP адресами.

            LXC держит адрес внутри строки netN
            ("name=eth0,bridge=vmbr0,hwaddr=..,ip=10.0.0.5/24"); ключа ipconfigN у
            контейнеров не бывает — это cloud-init QEMU. При ip=dhcp статики в
            конфиге нет вовсе, поэтому фактические адреса запущенного контейнера
            дочитываются из /nodes/{node}/lxc/{vmid}/interfaces.

            Args:
                node: Имя ноды
                vmid: ID контейнера
                include_live: опрашивать ли /interfaces (бесполезно для остановленного CT)

            Returns:
                Список интерфейсов: {'name', 'hardware_address', 'ips': [{'address', 'type', 'prefix'}]}
            """
            if not self.proxmox:
                return []

            interfaces: Dict[str, Dict] = {}

            def _slot(name: str, mac: str = '') -> Dict:
                iface = interfaces.setdefault(
                    name, {'name': name, 'hardware_address': '', 'ips': []}
                )
                if mac and not iface['hardware_address']:
                    iface['hardware_address'] = mac
                return iface

            def _add_ip(iface: Dict, value: str) -> None:
                """Принимает '10.0.0.5/24' либо '10.0.0.5'; мусор и dhcp игнорирует."""
                addr, _, prefix = (value or '').strip().partition('/')
                if not addr or addr in ('dhcp', 'manual', 'auto'):
                    return
                try:
                    ip_obj = ipaddress.ip_address(addr)
                except ValueError:
                    return
                if ip_obj.is_loopback or ip_obj.is_link_local:
                    return
                if any(existing['address'] == addr for existing in iface['ips']):
                    return
                iface['ips'].append({
                    'address': addr,
                    'type': 'ipv6' if ip_obj.version == 6 else 'ipv4',
                    'prefix': int(prefix) if prefix.isdigit() else (128 if ip_obj.version == 6 else 32),
                })

            # 1) Статика из конфига контейнера
            try:
                config = self.proxmox.nodes(node).lxc(vmid).config.get()
                for nic in parse_guest_nics(config):
                    iface = _slot(nic.name or f'eth{nic.index}', nic.mac or '')
                    _add_ip(iface, nic.ip or '')
                    _add_ip(iface, nic.ip6 or '')
            except Exception as e:
                logger.debug(f"Не удалось прочитать конфиг контейнера {vmid}: {e}")

            # 2) Живые адреса — единственный источник для DHCP-контейнеров.
            # Если статика уже найдена, лишний запрос не делаем: у остановленного
            # контейнера этот эндпоинт всё равно отдаёт ошибку.
            has_static_v4 = any(
                ip_info['type'] == 'ipv4'
                for iface in interfaces.values() for ip_info in iface['ips']
            )
            if not has_static_v4 and include_live:
                try:
                    for iface_data in (self.proxmox.nodes(node).lxc(vmid).interfaces.get() or []):
                        name = iface_data.get('name') or ''
                        if not name or name == 'lo':
                            continue
                        iface = _slot(name, (iface_data.get('hwaddr') or '').upper())
                        _add_ip(iface, iface_data.get('inet') or '')
                        _add_ip(iface, iface_data.get('inet6') or '')
                except Exception as e:
                    logger.debug(
                        f"Интерфейсы контейнера {vmid} недоступны (возможно, остановлен): {e}"
                    )

            return [iface for iface in interfaces.values() if iface['ips']]

        def execute_command(self, node: str, vmid: int, command: str, timeout: int = 30) -> Dict:
            """
            Выполнить команду на VM через QEMU guest agent
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
                command: Команда для выполнения (например: "ls -la /tmp")
                timeout: Таймаут в секундах для ожидания результата
            
            Returns:
                Dict с ключами: success, stdout, stderr, exit_code
            """
            if not self.proxmox:
                return {
                    'success': False,
                    'error': 'Proxmox connection not initialized',
                    'stdout': '',
                    'stderr': '',
                    'exit_code': -1
                }
            
            try:
                # Запускаем команду через guest agent
                exec_result = self.proxmox.nodes(node).qemu(vmid).agent.exec.post(
                    command=command
                )
                
                if 'pid' not in exec_result:
                    return {
                        'success': False,
                        'error': 'Failed to start command execution',
                        'stdout': '',
                        'stderr': '',
                        'exit_code': -1
                    }
                
                pid = exec_result['pid']

                # Ждем завершения команды и возвращаем результат
                return self._wait_exec_status(node, vmid, pid, timeout)

            except Exception as e:
                logger.error(f"Failed to execute command on VM {vmid}: {e}")
                return {
                    'success': False,
                    'error': agent_error_hint(str(e)),
                    'stdout': '',
                    'stderr': '',
                    'exit_code': -1
                }

        def _wait_exec_status(self, node: str, vmid: int, pid: int, timeout: int) -> Dict:
            """
            Опросить статус выполнения команды guest agent по pid до завершения.

            Returns:
                Dict с ключами: success, stdout, stderr, exit_code
            """
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    status = self.proxmox.nodes(node).qemu(vmid).agent('exec-status').get(pid=pid)

                    if status.get('exited'):
                        # Команда завершена
                        return {
                            'success': True,
                            'stdout': status.get('out-data', ''),
                            'stderr': status.get('err-data', ''),
                            'exit_code': status.get('exitcode', 0)
                        }
                except Exception as e:
                    logger.debug(f"Error checking command status: {e}")

                # Ждем немного перед следующей проверкой
                time.sleep(0.5)

            # Таймаут
            return {
                'success': False,
                'error': f'Command execution timeout ({timeout}s)',
                'stdout': '',
                'stderr': '',
                'exit_code': -1
            }

        def execute_script(self, node: str, vmid: int, script_content: str, 
                          interpreter: str = "/bin/bash", timeout: int = 60) -> Dict:
            """
            Выполнить bash скрипт на VM через QEMU guest agent
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
                script_content: Содержимое скрипта
                interpreter: Путь к интерпретатору (по умолчанию /bin/bash)
                timeout: Таймаут в секундах
            
            Returns:
                Dict с результатом выполнения
            """
            if not self.proxmox:
                return {
                    'success': False,
                    'error': 'Proxmox connection not initialized',
                    'stdout': '',
                    'stderr': '',
                    'exit_code': -1
                }

            # Нормализуем переводы строк CRLF/CR -> LF. Скрипты из браузерного
            # textarea часто приходят с \r, и тогда каждая команда ломается
            # ("$'cmd\\r': command not found", "growpart 1\\r" и т.п.).
            script_content = script_content.replace('\r\n', '\n').replace('\r', '\n')

            try:
                # Передаем скрипт напрямую интерпретатору через stdin (input-data).
                # Интерпретатор запускается без -c, читая тело скрипта со stdin,
                # поэтому многострочные скрипты, кавычки и shebang (#!...) в первой
                # строке обрабатываются корректно без дополнительного слоя кавычек.
                #
                # Proxmox API сам кодирует input-data в base64 перед передачей
                # guest agent'у, поэтому передаем сырое содержимое скрипта.
                # Proxmox кодирует input-data в base64 силами Perl, а тот падает
                # с "Wide character in subroutine entry" на любой строке с
                # не-ASCII — например на комментариях кириллицей. Отдаём те же
                # UTF-8 байты, представленные как latin-1: в JSON уходят байты,
                # гость получает корректный UTF-8.
                payload = script_content.encode('utf-8').decode('latin-1')

                exec_result = self.proxmox.nodes(node).qemu(vmid).agent.exec.post(
                    command=[interpreter],
                    **{'input-data': payload}
                )

                if 'pid' not in exec_result:
                    return {
                        'success': False,
                        'error': 'Failed to start script execution',
                        'stdout': '',
                        'stderr': '',
                        'exit_code': -1
                    }

                return self._wait_exec_status(node, vmid, exec_result['pid'], timeout)

            except Exception as e:
                logger.error(f"Failed to execute script on VM {vmid}: {e}")
                return {
                    'success': False,
                    'error': agent_error_hint(str(e)),
                    'stdout': '',
                    'stderr': '',
                    'exit_code': -1
                }

        def get_all_lxc_templates(self) -> List[Dict]:
            """
            Получить шаблоны LXC со всех нод кластера с информацией о типе хранилища.
            
            Returns:
                Список шаблонов с полями: volid, storage, node, shared, storage_type
            """
            if not self.proxmox:
                return []
            
            all_templates = []
            seen = set()  # Для дедупликации shared шаблонов
            
            try:
                nodes = self.get_nodes()
                
                # Сначала получим информацию о всех хранилищах кластера
                storage_info = {}
                try:
                    cluster_storage = self.proxmox.storage.get()
                    for stor in cluster_storage:
                        stor_name = stor.get('storage')
                        # shared=1 означает что хранилище доступно со всех нод
                        storage_info[stor_name] = {
                            'shared': stor.get('shared', 0) == 1,
                            'type': stor.get('type', 'unknown')
                        }
                except Exception as e:
                    logger.warning(f"Could not get cluster storage info: {e}")
                
                for node_info in nodes:
                    node = node_info.get('node')
                    if not node:
                        continue
                    
                    try:
                        templates = self.get_lxc_templates(node)
                        for tpl in templates:
                            stor_name = tpl.get('storage', '')
                            volid = tpl.get('volid', '')
                            
                            # Определяем shared ли хранилище
                            is_shared = storage_info.get(stor_name, {}).get('shared', False)
                            stor_type = storage_info.get(stor_name, {}).get('type', 'unknown')
                            
                            # Для shared хранилищ - добавляем только один раз
                            if is_shared:
                                if volid in seen:
                                    continue
                                seen.add(volid)
                            
                            tpl['shared'] = is_shared
                            tpl['storage_type'] = stor_type
                            all_templates.append(tpl)
                    except Exception as e:
                        logger.warning(f"Could not get templates from node {node}: {e}")
                
                return all_templates
            except Exception as e:
                logger.error(f"Error getting all LXC templates: {e}")
                return []

        def get_lxc_templates(self, node: str, storage: str = None) -> List[Dict]:
            """
            Получить список доступных шаблонов LXC контейнеров
            
            Args:
                node: Имя ноды
                storage: Хранилище (если None - ищем на всех с типом vztmpl)
            
            Returns:
                Список шаблонов с информацией
            """
            if not self.proxmox:
                return []
            
            templates = []
            try:
                # Получаем список хранилищ с типом vztmpl
                storages = self.proxmox.nodes(node).storage.get()
                
                for stor in storages:
                    stor_name = stor.get('storage')
                    stor_content = stor.get('content', '')
                    
                    # Проверяем, поддерживает ли хранилище vztmpl
                    if 'vztmpl' not in stor_content:
                        continue
                    
                    if storage and stor_name != storage:
                        continue
                    
                    try:
                        # Получаем содержимое хранилища
                        logger.info(f"[LXC Templates] Fetching templates from storage: {stor_name}")
                        content = self.proxmox.nodes(node).storage(stor_name).content.get(content='vztmpl')
                        logger.info(f"[LXC Templates] Found {len(content)} templates in {stor_name}")
                        for item in content:
                            item['storage'] = stor_name
                            item['node'] = node
                            templates.append(item)
                    except Exception as e:
                        logger.warning(f"Не удалось получить шаблоны из {stor_name}: {e}")
                
                return templates
            except Exception as e:
                logger.error(f"Ошибка получения LXC шаблонов с ноды {node}: {e}")
                return []

        def download_lxc_template(self, node: str, storage: str, template: str) -> Optional[str]:
            """
            Скачать шаблон LXC из репозитория
            
            Args:
                node: Имя ноды
                storage: Хранилище для загрузки
                template: Имя шаблона (например debian-12-standard_12.2-1_amd64.tar.zst)
            
            Returns:
                UPID задачи или None
            """
            if not self.proxmox:
                return None
            
            try:
                result = self.proxmox.nodes(node).aplinfo.post(
                    storage=storage,
                    template=template
                )
                logger.info(f"Запущена загрузка шаблона {template} на {node}:{storage}")
                return result
            except Exception as e:
                logger.error(f"Ошибка загрузки шаблона {template}: {e}")
                return None

        def get_available_lxc_templates(self, node: str) -> List[Dict]:
            """
            Получить список доступных для загрузки шаблонов LXC
            
            Args:
                node: Имя ноды
            
            Returns:
                Список доступных шаблонов из репозитория
            """
            if not self.proxmox:
                return []
            
            try:
                templates = self.proxmox.nodes(node).aplinfo.get()
                return templates
            except Exception as e:
                logger.error(f"Ошибка получения списка доступных шаблонов: {e}")
                return []

        def create_lxc_container(
            self,
            node: str,
            vmid: int,
            ostemplate: str,
            hostname: str,
            password: str = None,
            ssh_public_keys: str = None,
            storage: str = 'local-lvm',
            rootfs_size: int = 8,
            memory: int = 512,
            swap: int = 512,
            cores: int = 1,
            net0: str = None,
            unprivileged: bool = True,
            start_after_create: bool = False,
            onboot: bool = False,
            description: str = None,
            features: str = None,
            nameserver: str = None,
            searchdomain: str = None,
        ) -> Optional[str]:
            """
            Создать новый LXC контейнер
            
            Args:
                node: Имя ноды
                vmid: ID контейнера
                ostemplate: Путь к шаблону (storage:vztmpl/template.tar.gz)
                hostname: Имя хоста контейнера
                password: Пароль root (опционально, если есть ssh_public_keys)
                ssh_public_keys: SSH публичные ключи
                storage: Хранилище для rootfs
                rootfs_size: Размер rootfs в GB
                memory: Память в MB
                swap: Swap в MB
                cores: Количество ядер CPU
                net0: Конфигурация сети (например: name=eth0,bridge=vmbr0,ip=dhcp)
                unprivileged: Непривилегированный контейнер (рекомендуется)
                start_after_create: Запустить после создания
                onboot: Автозапуск при старте хоста
                description: Описание контейнера
                features: Дополнительные features (nesting, keyctl, etc)
            
            Returns:
                UPID задачи или None при ошибке
            """
            if not self.proxmox:
                return None
            
            try:
                params = {
                    'vmid': vmid,
                    'ostemplate': ostemplate,
                    'hostname': hostname,
                    'storage': storage,
                    'rootfs': f'{storage}:{rootfs_size}',
                    'memory': memory,
                    'swap': swap,
                    'cores': cores,
                    'unprivileged': 1 if unprivileged else 0,
                    'start': 1 if start_after_create else 0,
                    'onboot': 1 if onboot else 0,
                }
                
                if password:
                    params['password'] = password
                
                if ssh_public_keys:
                    # proxmoxer URL-encodes params automatically; pass key as-is
                    params['ssh-public-keys'] = ssh_public_keys.strip()
                
                if net0:
                    params['net0'] = net0
                else:
                    # Конфигурация по умолчанию
                    params['net0'] = 'name=eth0,bridge=vmbr0,ip=dhcp'
                
                if description:
                    params['description'] = description
                
                # Enable nesting by default — required for systemd 255+ (Ubuntu 24.04, Debian 13)
                # to avoid "WARN: Systemd 255 detected. You may need to enable nesting." warning
                if features:
                    params['features'] = features
                else:
                    params['features'] = 'nesting=1'
    
                if nameserver:
                    params['nameserver'] = nameserver

                if searchdomain:
                    params['searchdomain'] = searchdomain

                # Ретрай на транзиентную гонку блокировки конфига (частый случай:
                # тот же VMID пересоздаётся сразу после удаления, пока нода ещё
                # держит pve-config-<vmid>.lock — напр. фоновая очистка диска).
                import time as _time
                attempts = 3
                for attempt in range(1, attempts + 1):
                    try:
                        result = self.proxmox.nodes(node).lxc.post(**params)
                        logger.info(f"Запущено создание LXC контейнера {vmid} ({hostname}) на {node}")
                        return result
                    except Exception as e:
                        msg = str(e)
                        is_lock_race = "got lock" in msg or "can't lock file" in msg
                        if is_lock_race and attempt < attempts:
                            logger.warning(
                                f"Создание LXC {vmid}: блокировка конфига (попытка "
                                f"{attempt}/{attempts}), повтор через 5с: {msg}"
                            )
                            _time.sleep(5)
                            continue
                        raise
            except Exception as e:
                logger.error(f"Ошибка создания LXC контейнера {vmid}: {e}")
                raise

        def clone_lxc_container(
            self,
            node: str,
            source_vmid: int,
            new_vmid: int,
            hostname: str,
            full_clone: bool = True,
            target_storage: str = None,
            description: str = None
        ) -> Optional[str]:
            """
            Клонировать LXC контейнер
            
            Args:
                node: Имя ноды
                source_vmid: VMID исходного контейнера
                new_vmid: VMID нового контейнера
                hostname: Имя хоста нового контейнера
                full_clone: Полный клон (True) или linked clone (False)
                target_storage: Целевое хранилище
                description: Описание
            
            Returns:
                UPID задачи или None
            """
            if not self.proxmox:
                return None
            
            try:
                params = {
                    'newid': new_vmid,
                    'hostname': hostname,
                    'full': 1 if full_clone else 0,
                }
                
                if target_storage:
                    params['storage'] = target_storage
                
                if description:
                    params['description'] = description
                
                result = self.proxmox.nodes(node).lxc(source_vmid).clone.post(**params)
                logger.info(f"Клонирование LXC {source_vmid} -> {new_vmid} ({hostname}) на {node}")
                return result
            except Exception as e:
                logger.error(f"Ошибка клонирования LXC {source_vmid} -> {new_vmid}: {e}")
                return None

        def migrate_vm(
            self,
            node: str,
            vmid: int,
            target_node: str,
            target_storage: str = None,
            online: bool = False,
            with_local_disks: bool = True,
        ) -> Optional[str]:
            """
            Мигрировать VM (qemu) на другую ноду.

            Args:
                node: Текущая нода VM
                vmid: ID виртуальной машины
                target_node: Целевая нода
                target_storage: Целевое хранилище (опционально, storage migration)
                online: Онлайн (live) миграция для запущенной VM
                with_local_disks: Мигрировать локальные диски (обязательно для live
                    миграции VM с дисками на local storage)

            Returns:
                UPID задачи или None
            """
            if not self.proxmox:
                return None

            try:
                params = {'target': target_node}
                if online:
                    params['online'] = 1
                if with_local_disks:
                    params['with-local-disks'] = 1
                if target_storage:
                    params['targetstorage'] = target_storage

                result = self.proxmox.nodes(node).qemu(vmid).migrate.post(**params)
                logger.info(f"Миграция VM {vmid} с {node} на {target_node}")
                return result if isinstance(result, str) else None
            except Exception as e:
                logger.error(f"Ошибка миграции VM {vmid}: {e}")
                raise RuntimeError(f"Proxmox: {e}") from e

        def migrate_container(
            self,
            node: str,
            vmid: int,
            target_node: str,
            target_storage: str = None,
            online: bool = False
        ) -> Optional[str]:
            """
            Мигрировать LXC контейнер на другую ноду
            
            Args:
                node: Текущая нода контейнера
                vmid: ID контейнера
                target_node: Целевая нода
                target_storage: Целевое хранилище (опционально)
                online: Онлайн миграция (для запущенных контейнеров)
            
            Returns:
                UPID задачи или None
            """
            if not self.proxmox:
                return None
            
            try:
                params = {
                    'target': target_node,
                }
                
                if target_storage:
                    params['target-storage'] = target_storage
                
                if online:
                    params['online'] = 1
                
                result = self.proxmox.nodes(node).lxc(vmid).migrate.post(**params)
                logger.info(f"Миграция LXC {vmid} с {node} на {target_node}")
                return result if isinstance(result, str) else None
            except Exception as e:
                logger.error(f"Ошибка миграции LXC {vmid}: {e}")
                raise RuntimeError(f"Proxmox: {e}") from e

        def remote_migrate_vm(
            self,
            node: str,
            vmid: int,
            target_endpoint: str,
            target_vmid: int,
            target_storage: str = None,
            target_bridge: str = None,
            online: bool = False,
            delete_source: bool = True,
            bwlimit: int = None,
        ) -> Optional[str]:
            """
            Мигрировать VM (qemu) на другой (независимый) кластер через remote_migrate
            API (PVE >= 8.0). В отличие от migrate_vm, авторизация на целевой стороне
            идёт по API-токену из target_endpoint, а не по членству в corosync.

            Args:
                node: Текущая нода VM
                vmid: ID виртуальной машины на источнике
                target_endpoint: Строка `apitoken=...,host=...,fingerprint=...` целевого кластера
                target_vmid: ID виртуальной машины на цели
                target_storage: Целевое хранилище (применяется ко всем дискам)
                target_bridge: Целевой сетевой мост (применяется ко всем интерфейсам)
                online: Live-миграция запущенной VM
                delete_source: Удалить VM с источника после успешного переноса
                bwlimit: Ограничение полосы (KiB/s)

            Returns:
                UPID задачи (на источнике) или None
            """
            if not self.proxmox:
                return None

            try:
                params = {
                    'target-endpoint': target_endpoint,
                    'target-vmid': target_vmid,
                    'delete': 1 if delete_source else 0,
                }
                if target_storage:
                    params['target-storage'] = target_storage
                if target_bridge:
                    params['target-bridge'] = target_bridge
                if online:
                    params['online'] = 1
                if bwlimit:
                    params['bwlimit'] = bwlimit

                result = self.proxmox.nodes(node).qemu(vmid).remote_migrate.post(**params)
                logger.info(f"Remote-миграция VM {vmid} с {node} на {target_vmid}@{target_endpoint.split(',')[1] if ',' in target_endpoint else target_endpoint}")
                return result if isinstance(result, str) else None
            except Exception as e:
                logger.error(f"Ошибка remote-миграции VM {vmid}: {e}")
                raise RuntimeError(f"Proxmox: {e}") from e

        def remote_migrate_container(
            self,
            node: str,
            vmid: int,
            target_endpoint: str,
            target_vmid: int,
            target_storage: str = None,
            target_bridge: str = None,
            online: bool = False,
            delete_source: bool = True,
            bwlimit: int = None,
            restart: bool = False,
        ) -> Optional[str]:
            """
            Мигрировать LXC контейнер на другой (независимый) кластер через
            remote_migrate API (PVE >= 8.0). См. remote_migrate_vm.

            Args:
                restart: Перезапустить контейнер на источнике перед переносом
                    (аналог pct remote-migrate --restart, нужен, если online=False,
                    но контейнер запущен)
            """
            if not self.proxmox:
                return None

            try:
                params = {
                    'target-endpoint': target_endpoint,
                    'target-vmid': target_vmid,
                    'delete': 1 if delete_source else 0,
                }
                if target_storage:
                    params['target-storage'] = target_storage
                if target_bridge:
                    params['target-bridge'] = target_bridge
                if online:
                    params['online'] = 1
                if restart:
                    params['restart'] = 1
                if bwlimit:
                    params['bwlimit'] = bwlimit

                result = self.proxmox.nodes(node).lxc(vmid).remote_migrate.post(**params)
                logger.info(f"Remote-миграция LXC {vmid} с {node} на {target_vmid}")
                return result if isinstance(result, str) else None
            except Exception as e:
                logger.error(f"Ошибка remote-миграции LXC {vmid}: {e}")
                raise RuntimeError(f"Proxmox: {e}") from e

        def get_vm_termproxy(self, node: str, vmid: int) -> Dict:
            """
            Получить данные для терминального подключения к VM через xterm.js
            
            Args:
                node: Имя ноды
                vmid: ID виртуальной машины
            
            Returns:
                Dict с данными терминала (port, ticket, user)
            """
            if not self.proxmox:
                return {}
            
            try:
                term_data = self.proxmox.nodes(node).qemu(vmid).termproxy.post()
                return term_data
            except Exception as e:
                logger.error(f"Ошибка получения termproxy для VM {vmid} на {node}: {e}")
                return {}

        def get_container_termproxy(self, node: str, vmid: int) -> Dict:
            """
            Получить данные для терминального подключения к LXC контейнеру через xterm.js
            
            Args:
                node: Имя ноды
                vmid: ID контейнера
            
            Returns:
                Dict с данными терминала (port, ticket, user)
            """
            if not self.proxmox:
                return {}
            
            try:
                term_data = self.proxmox.nodes(node).lxc(vmid).termproxy.post()
                return term_data
            except Exception as e:
                logger.error(f"Ошибка получения termproxy для LXC {vmid} на {node}: {e}")
                return {}

        def exec_in_container(self, node: str, vmid: int, command: list) -> Dict:
            """
            Выполнить команду в LXC контейнере через pct exec
            
            Args:
                node: Имя ноды
                vmid: ID контейнера
                command: Список [команда, arg1, arg2, ...]
            
            Returns:
                Dict с upid задачи выполнения
            """
            if not self.proxmox:
                return {}
            
            try:
                # Proxmox ожидает command и args отдельно
                cmd = command[0] if command else "/bin/bash"
                args = command[1:] if len(command) > 1 else []
                
                # Формируем data для POST запроса
                data = {"command": cmd}
                for idx, arg in enumerate(args):
                    data[f"args[{idx}]"] = arg
                
                result = self.proxmox.nodes(node).lxc(vmid).exec.post(**data)
                return result
            except Exception as e:
                logger.error(f"Ошибка exec в LXC {vmid} на {node}: {e}")
                return {}

        def get_task_log(self, node: str, upid: str, start: int = 0, limit: int = 500) -> List[Dict]:
            """Get task log lines"""
            if not self.proxmox:
                return []
            try:
                log = self.proxmox.nodes(node).tasks(upid).log.get(start=start, limit=limit)
                return log
            except Exception as e:
                logger.error(f"Error getting task log {upid}: {e}")
                return []

        def sdn_is_available(self) -> bool:
            """
            Check if SDN is available on this Proxmox cluster.
            SDN requires Proxmox VE 7.0+ and proper configuration.
            """
            if not self.proxmox:
                return False
            
            try:
                # Try to access SDN API
                self.proxmox.cluster.sdn.get()
                return True
            except Exception as e:
                logger.debug(f"SDN not available: {e}")
                return False

        def get_sdn_zone(self, zone: str) -> Optional[Dict]:
            """
            Get details of a specific SDN zone.
            
            Args:
                zone: Zone name/ID
            
            Returns:
                Zone configuration dict or None
            """
            if not self.proxmox:
                return None
            
            try:
                zone_data = self.proxmox.cluster.sdn.zones(zone).get()
                return zone_data
            except Exception as e:
                logger.error(f"Error getting SDN zone {zone}: {e}")
                return None

        def delete_sdn_zone(self, zone: str) -> Dict:
            """
            Delete an SDN zone.
            
            Args:
                zone: Zone name to delete
            
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            
            try:
                self.proxmox.cluster.sdn.zones(zone).delete()
                logger.info(f"Deleted SDN zone: {zone}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Error deleting SDN zone {zone}: {e}")
                return {"success": False, "error": str(e)}

        def get_sdn_vnet(self, vnet: str) -> Optional[Dict]:
            """
            Get details of a specific VNet.
            
            Args:
                vnet: VNet name
            
            Returns:
                VNet configuration dict or None
            """
            if not self.proxmox:
                return None
            
            try:
                vnet_data = self.proxmox.cluster.sdn.vnets(vnet).get()
                return vnet_data
            except Exception as e:
                logger.error(f"Error getting SDN vnet {vnet}: {e}")
                return None

        def delete_sdn_vnet(self, vnet: str) -> Dict:
            """
            Delete an SDN VNet.
            
            Args:
                vnet: VNet name to delete
            
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            
            try:
                self.proxmox.cluster.sdn.vnets(vnet).delete()
                logger.info(f"Deleted SDN vnet: {vnet}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Error deleting SDN vnet {vnet}: {e}")
                return {"success": False, "error": str(e)}

        def get_sdn_subnets(self, vnet: str) -> List[Dict]:
            """
            Get subnets for a specific VNet.
            
            Args:
                vnet: VNet name
            
            Returns:
                List of subnets in the VNet
            """
            if not self.proxmox:
                return []
            
            try:
                subnets = self.proxmox.cluster.sdn.vnets(vnet).subnets.get()
                return subnets if isinstance(subnets, list) else []
            except Exception as e:
                logger.error(f"Error getting subnets for vnet {vnet}: {e}")
                return []

        def delete_sdn_subnet(self, vnet: str, subnet: str) -> Dict:
            """
            Delete a subnet from a VNet.
            
            Args:
                vnet: VNet name
                subnet: Subnet to delete (CIDR format, URL-encoded)
            
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            
            try:
                # Subnet ID in URL is the CIDR with / replaced by -
                subnet_id = subnet.replace("/", "-")
                self.proxmox.cluster.sdn.vnets(vnet).subnets(subnet_id).delete()
                logger.info(f"Deleted subnet {subnet} from vnet {vnet}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Error deleting subnet {subnet} from vnet {vnet}: {e}")
                return {"success": False, "error": str(e)}

        def apply_sdn_changes(self) -> Dict:
            """
            Apply pending SDN configuration changes to all nodes.
            This is required after creating/modifying zones, vnets, or subnets.
            
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            
            try:
                result = self.proxmox.cluster.sdn.put()
                logger.info("Applied SDN changes to cluster")
                return {"success": True, "upid": result}
            except Exception as e:
                logger.error(f"Error applying SDN changes: {e}")
                return {"success": False, "error": str(e)}

        def get_sdn_pending(self) -> List[Dict]:
            """
            Get pending SDN changes.
            
            Returns:
                List of pending changes
            """
            if not self.proxmox:
                return []
            
            try:
                # Pending status is typically in the zone/vnet responses
                zones = self.get_sdn_zones()
                vnets = self.get_sdn_vnets()
                
                pending = []
                for zone in zones:
                    if zone.get('pending'):
                        pending.append({"type": "zone", "id": zone.get('zone'), "pending": zone.get('pending')})
                for vnet in vnets:
                    if vnet.get('pending'):
                        pending.append({"type": "vnet", "id": vnet.get('vnet'), "pending": vnet.get('pending')})
                
                return pending
            except Exception as e:
                logger.error(f"Error getting SDN pending changes: {e}")
                return []

        def update_sdn_zone(self, zone: str, **kwargs) -> Dict:
            """
            Update an existing SDN zone.
    
            Args:
                zone: Zone name to update
                **kwargs: Fields to update (mtu, dns, reversedns, ipam, etc.)
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                self.proxmox.cluster.sdn.zones(zone).put(**kwargs)
                logger.info(f"Updated SDN zone: {zone}")
                return {"success": True, "zone": zone}
            except Exception as e:
                logger.error(f"Error updating SDN zone {zone}: {e}")
                return {"success": False, "error": str(e)}

        def update_sdn_vnet(self, vnet: str, **kwargs) -> Dict:
            """
            Update an existing SDN VNet.
    
            Args:
                vnet: VNet name to update
                **kwargs: Fields to update (alias, tag, vlanaware, etc.)
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                self.proxmox.cluster.sdn.vnets(vnet).put(**kwargs)
                logger.info(f"Updated SDN vnet: {vnet}")
                return {"success": True, "vnet": vnet}
            except Exception as e:
                logger.error(f"Error updating SDN vnet {vnet}: {e}")
                return {"success": False, "error": str(e)}

        def get_node_networks(self, node: str) -> List[Dict]:
            """
            Get all network interfaces configured on a node.
    
            Args:
                node: Node name
    
            Returns:
                List of network interface objects
            """
            if not self.proxmox:
                return []
    
            try:
                ifaces = self.proxmox.nodes(node).network.get()
                return ifaces if isinstance(ifaces, list) else []
            except Exception as e:
                logger.error(f"Error getting node {node} networks: {e}")
                return []

        def get_node_network(self, node: str, iface: str) -> Optional[Dict]:
            """
            Get details of a specific network interface on a node.
    
            Args:
                node: Node name
                iface: Interface name (e.g., vmbr0, bond0)
    
            Returns:
                Interface configuration dict or None
            """
            if not self.proxmox:
                return None
    
            try:
                return self.proxmox.nodes(node).network(iface).get()
            except Exception as e:
                logger.error(f"Error getting node {node} interface {iface}: {e}")
                return None

        def create_node_network(self, node: str, iface_type: str, **kwargs) -> Dict:
            """
            Create a new network interface on a node.
    
            Args:
                node: Node name
                iface_type: Type of interface ('bridge', 'bond', 'vlan', 'eth')
                **kwargs: Interface parameters (iface, address, netmask, gateway,
                          bridge_ports, bond_slaves, bond_mode, vlan-id,
                          vlan-raw-device, autostart, comments, etc.)
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                params = {"type": iface_type}
                params.update(kwargs)
                self.proxmox.nodes(node).network.post(**params)
                iface_name = kwargs.get("iface", "")
                logger.info(f"Created {iface_type} interface {iface_name} on node {node}")
                return {"success": True, "iface": iface_name}
            except Exception as e:
                logger.error(f"Error creating {iface_type} interface on node {node}: {e}")
                return {"success": False, "error": str(e)}

        def update_node_network(self, node: str, iface: str, **kwargs) -> Dict:
            """
            Update a network interface on a node.
    
            Args:
                node: Node name
                iface: Interface name to update
                **kwargs: Fields to update
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                self.proxmox.nodes(node).network(iface).put(**kwargs)
                logger.info(f"Updated interface {iface} on node {node}")
                return {"success": True, "iface": iface}
            except Exception as e:
                logger.error(f"Error updating interface {iface} on node {node}: {e}")
                return {"success": False, "error": str(e)}

        def delete_node_network(self, node: str, iface: str) -> Dict:
            """
            Delete a network interface from a node.
    
            Args:
                node: Node name
                iface: Interface name to delete
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                self.proxmox.nodes(node).network(iface).delete()
                logger.info(f"Deleted interface {iface} from node {node}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Error deleting interface {iface} from node {node}: {e}")
                return {"success": False, "error": str(e)}

        def apply_node_network_config(self, node: str) -> Dict:
            """
            Apply pending network configuration changes on a node.
    
            Args:
                node: Node name
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                result = self.proxmox.nodes(node).network.put()
                logger.info(f"Applied network config on node {node}")
                return {"success": True, "result": result}
            except Exception as e:
                logger.error(f"Error applying network config on node {node}: {e}")
                return {"success": False, "error": str(e)}

        def revert_node_network_config(self, node: str) -> Dict:
            """
            Revert pending network configuration changes on a node (restore from running config).
    
            Args:
                node: Node name
    
            Returns:
                Result dict with success status
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
    
            try:
                self.proxmox.nodes(node).network.delete()
                logger.info(f"Reverted network config on node {node}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Error reverting network config on node {node}: {e}")
                return {"success": False, "error": str(e)}

        def get_snapshot_config(self, node: str, vmid: int, snapname: str, vm_type: str = 'qemu') -> Optional[Dict]:
            """
            Get configuration of a snapshot.
            
            Args:
                node: Node name
                vmid: VM/Container ID
                snapname: Snapshot name
                vm_type: 'qemu' for VMs, 'lxc' for containers
            
            Returns:
                Snapshot configuration dict or None
            """
            if not self.proxmox:
                return None
            
            try:
                if vm_type == 'qemu':
                    config = self.proxmox.nodes(node).qemu(vmid).snapshot(snapname).config.get()
                else:
                    config = self.proxmox.nodes(node).lxc(vmid).snapshot(snapname).config.get()
                return config
            except Exception as e:
                logger.error(f"Error getting snapshot {snapname} config: {e}")
                return None

        def get_cluster_storages(self) -> List[Dict]:
            """Get all storages configured on this Proxmox cluster"""
            if not self.proxmox:
                return []
            try:
                storages = self.proxmox.storage.get()
                return storages
            except Exception as e:
                logger.error(f"Error getting storages from {self.host}: {e}")
                return []

        def create_storage(self, storage_id: str, storage_type: str, **kwargs) -> Dict:
            """Create a new storage configuration"""
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.storage.post(storage=storage_id, type=storage_type, **kwargs)
                return {"success": True}
            except Exception as e:
                logger.error(f"Error creating storage {storage_id}: {e}")
                return {"success": False, "error": str(e)}

        def update_storage(self, storage_id: str, **kwargs) -> Dict:
            """Update storage configuration"""
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.storage(storage_id).put(**kwargs)
                return {"success": True}
            except Exception as e:
                logger.error(f"Error updating storage {storage_id}: {e}")
                return {"success": False, "error": str(e)}

        def delete_storage(self, storage_id: str) -> Dict:
            """Delete storage configuration"""
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.storage(storage_id).delete()
                return {"success": True}
            except Exception as e:
                logger.error(f"Error deleting storage {storage_id}: {e}")
                return {"success": False, "error": str(e)}

        def list_backups(self, node: str, storage: str, vmid: int = None) -> List[Dict]:
            """List backup files in a storage on a node"""
            if not self.proxmox:
                return []
            try:
                params = {"content": "backup"}
                if vmid:
                    params["vmid"] = vmid
                items = self.proxmox.nodes(node).storage(storage).content.get(**params)
                return items
            except Exception as e:
                logger.error(f"Error listing backups on {node}/{storage}: {e}")
                return []

        def create_backup(self, node: str, vmid: int, storage: str,
                          mode: str = "snapshot", compress: str = "zstd",
                          remove: int = 1, keep_last: int = None, notes: str = None) -> Dict:
            """
            Trigger vzdump backup for a VM/container.
            Returns {"success": True, "upid": "..."} or {"success": False, "error": "..."}
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                params = {
                    "storage": storage,
                    "mode": mode,
                    "compress": compress,
                    "remove": int(bool(remove)),
                }
                # «Все VM»: vzdump требует all=1, а не vmid (vmid=0 — ошибка PVE)
                if vmid in (None, 0, "all"):
                    params["all"] = 1
                else:
                    params["vmid"] = vmid
                if keep_last and int(keep_last) > 0:
                    params["prune-backups"] = f"keep-last={int(keep_last)}"
                if notes:
                    params["notes-template"] = notes
                upid = self.proxmox.nodes(node).vzdump.post(**params)
                return {"success": True, "upid": upid}
            except Exception as e:
                logger.error(f"Error creating backup for VM {vmid} on {node}: {e}")
                return {"success": False, "error": str(e)}

        def restore_vm(self, node: str, vmid: int, archive: str, storage: str = None,
                       new_vmid: int = None, start: bool = False, unique: bool = True,
                       force: bool = False) -> Dict:
            """
            Restore a QEMU VM from backup.
            archive: volume id like 'local:backup/vzdump-qemu-100-...'
            storage: TARGET storage for the restored disks. When empty/None, Proxmox
                     restores each disk to the storage recorded in the backup config
                     ("from backup configuration"). The backup source is encoded in
                     `archive`, so this must NOT be the backup storage.
            force: overwrite an existing VM with the same vmid (must be stopped).
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                # For QEMU, restore is triggered by passing `archive` to POST /nodes/{node}/qemu.
                # There is NO `restore` parameter (unlike LXC) — sending it returns a 400.
                params = {
                    "vmid": new_vmid if new_vmid else vmid,
                    "archive": archive,
                    "start": 1 if start else 0,
                    "unique": 1 if unique else 0,
                }
                if storage:
                    params["storage"] = storage
                if force:
                    params["force"] = 1
                upid = self.proxmox.nodes(node).qemu.post(**params)
                return {"success": True, "upid": upid}
            except Exception as e:
                logger.error(f"Error restoring VM {vmid} on {node}: {e}")
                return {"success": False, "error": str(e)}

        def restore_lxc(self, node: str, vmid: int, archive: str, storage: str = None,
                        new_vmid: int = None, start: bool = False,
                        force: bool = False) -> Dict:
            """Restore an LXC container from backup.

            storage: TARGET storage for the restored rootfs. When empty/None, Proxmox
                     restores to the storage recorded in the backup config. The backup
                     source is encoded in `archive`, so this must NOT be the backup storage.
            force: overwrite an existing container with the same vmid (must be stopped).
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                params = {
                    "vmid": new_vmid if new_vmid else vmid,
                    "ostemplate": archive,
                    "restore": 1,
                    "start": 1 if start else 0,
                }
                if storage:
                    params["storage"] = storage
                if force:
                    params["force"] = 1
                upid = self.proxmox.nodes(node).lxc.post(**params)
                return {"success": True, "upid": upid}
            except Exception as e:
                logger.error(f"Error restoring LXC {vmid} on {node}: {e}")
                return {"success": False, "error": str(e)}

        def delete_backup(self, node: str, storage: str, volid: str) -> Dict:
            """Delete a specific backup file by its volume ID"""
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.nodes(node).storage(storage).content(volid).delete()
                return {"success": True}
            except Exception as e:
                logger.error(f"Error deleting backup {volid}: {e}")
                return {"success": False, "error": str(e)}

        def get_cluster_backup_jobs(self) -> List[Dict]:
            """Fetch native vzdump backup jobs configured in Proxmox (cluster/backup)"""
            if not self.proxmox:
                return []
            try:
                jobs = self.proxmox.cluster.backup.get()
                return jobs if isinstance(jobs, list) else []
            except Exception as e:
                logger.error(f"Error fetching cluster backup jobs: {e}")
                return []

        def create_cluster_backup_job(self, props: Dict) -> Dict:
            """Создать нативное задание бэкапа Proxmox (POST /cluster/backup).

            props — уже подготовленные параметры vzdump (schedule, storage, mode,
            compress, enabled, all/vmid/pool/node, prune-backups, comment, ...).
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.cluster.backup.post(**props)
                logger.info(f"Создано нативное задание бэкапа: {props.get('id') or '(auto id)'}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Ошибка создания задания бэкапа: {e}")
                return {"success": False, "error": str(e)}

        def update_cluster_backup_job(self, job_id: str, props: Dict) -> Dict:
            """Обновить нативное задание бэкапа Proxmox (PUT /cluster/backup/{id})."""
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.cluster.backup(job_id).put(**props)
                logger.info(f"Обновлено нативное задание бэкапа: {job_id}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Ошибка обновления задания бэкапа {job_id}: {e}")
                return {"success": False, "error": str(e)}

        def delete_cluster_backup_job(self, job_id: str) -> Dict:
            """Удалить нативное задание бэкапа Proxmox (DELETE /cluster/backup/{id})."""
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.cluster.backup(job_id).delete()
                logger.info(f"Удалено нативное задание бэкапа: {job_id}")
                return {"success": True}
            except Exception as e:
                logger.error(f"Ошибка удаления задания бэкапа {job_id}: {e}")
                return {"success": False, "error": str(e)}

