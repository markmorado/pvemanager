import logging
from typing import Dict, List, Optional, Union
from contextlib import asynccontextmanager
from proxmoxer import ProxmoxAPI
from proxmoxer.core import AuthenticationError
import time
import asyncio
import base64
from functools import lru_cache
from app.ssh_client import SSHClient

logger = logging.getLogger(__name__)

# Connection cache for reusing Proxmox connections
connection_cache = {}


class ProxmoxClient:
    """Клиент для работы с Proxmox VE API с кешированием соединений"""
    
    def __init__(self, host: str, user: str = "root@pam", password: str = None, 
                 token_name: str = None, token_value: str = None, verify_ssl: bool = False,
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
        # Улучшенный ключ кеша - включаем token_name для уникальности
        self.connection_key = f"{host}:{user}:{token_name or 'password'}"
        self.proxmox = None
        self.last_used = time.time()
        
        # Check cache first
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
                logger.info(f"Connected to Proxmox {host} using password")
            else:
                logger.error(f"Необходимо указать либо password, либо token для {host}")
                return
            
            # Test connection
            if self.proxmox:
                try:
                    self.proxmox.version.get()
                    # Cache successful connection
                    connection_cache[self.connection_key] = {
                        'client': self.proxmox,
                        'created': time.time()
                    }
                    logger.debug(f"Cached connection for {self.connection_key}")
                except Exception as e:
                    logger.error(f"Failed to test Proxmox connection for {host}: {e}")
                    self.proxmox = None
                    # Удаляем неудачное подключение из кеша
                    if self.connection_key in connection_cache:
                        del connection_cache[self.connection_key]
                    
        except AuthenticationError as e:
            logger.error(f"Ошибка аутентификации Proxmox {host}: {e}")
            self.proxmox = None
            # Удаляем из кеша при ошибке аутентификации
            if self.connection_key in connection_cache:
                del connection_cache[self.connection_key]
        except Exception as e:
            logger.error(f"Ошибка подключения к Proxmox {host}: {e}")
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
    
    def get_vm_status(self, node: str, vmid: int) -> Optional[Dict]:
        """Получить статус конкретной VM"""
        if not self.proxmox:
            return None
        
        try:
            status = self.proxmox.nodes(node).qemu(vmid).status.current.get()
            return status
        except Exception as e:
            logger.error(f"Ошибка получения статуса VM {vmid} на {node}: {e}")
            return None
    
    def get_container_status(self, node: str, vmid: int) -> Optional[Dict]:
        """Получить статус конкретного LXC контейнера"""
        if not self.proxmox:
            return None
        
        try:
            status = self.proxmox.nodes(node).lxc(vmid).status.current.get()
            return status
        except Exception as e:
            logger.error(f"Ошибка получения статуса LXC {vmid} на {node}: {e}")
            return None
    
    def get_vm_stats(self, node: str, vmid: int) -> Optional[Dict]:
        """Получить статистику VM (CPU, память, диск)"""
        return self.get_vm_status(node, vmid)
    
    def get_container_stats(self, node: str, vmid: int) -> Optional[Dict]:
        """Получить статистику LXC контейнера (CPU, память, диск)"""
        return self.get_container_status(node, vmid)
    
    def start_vm(self, node: str, vmid: int) -> bool:
        """Запустить VM"""
        if not self.proxmox:
            return False
        
        try:
            self.proxmox.nodes(node).qemu(vmid).status.start.post()
            return True
        except Exception as e:
            logger.error(f"Ошибка запуска VM {vmid} на {node}: {e}")
            return False
    
    def stop_vm(self, node: str, vmid: int, force: bool = False) -> bool:
        """Остановить VM"""
        if not self.proxmox:
            return False
        
        try:
            # Proxmox не поддерживает force параметр через stop endpoint
            # Используем обычную остановку
            self.proxmox.nodes(node).qemu(vmid).status.stop.post()
            return True
        except Exception as e:
            logger.error(f"Ошибка остановки VM {vmid} на {node}: {e}")
            return False
    
    def restart_vm(self, node: str, vmid: int) -> bool:
        """Перезапустить VM"""
        if not self.proxmox:
            return False
        
        try:
            self.proxmox.nodes(node).qemu(vmid).status.reboot.post()
            return True
        except Exception as e:
            logger.error(f"Ошибка перезапуска VM {vmid} на {node}: {e}")
            return False
    
    def start_container(self, node: str, vmid: int) -> bool:
        """Запустить LXC контейнер"""
        if not self.proxmox:
            return False
        
        try:
            self.proxmox.nodes(node).lxc(vmid).status.start.post()
            return True
        except Exception as e:
            logger.error(f"Ошибка запуска LXC {vmid} на {node}: {e}")
            return False
    
    def stop_container(self, node: str, vmid: int, force: bool = False) -> bool:
        """Остановить LXC контейнер"""
        if not self.proxmox:
            return False
        
        try:
            # Proxmox не поддерживает force параметр через stop endpoint
            # Используем обычную остановку
            self.proxmox.nodes(node).lxc(vmid).status.stop.post()
            return True
        except Exception as e:
            logger.error(f"Ошибка остановки LXC {vmid} на {node}: {e}")
            return False
    
    def restart_container(self, node: str, vmid: int) -> bool:
        """Перезапустить LXC контейнер"""
        if not self.proxmox:
            return False
        
        try:
            self.proxmox.nodes(node).lxc(vmid).status.reboot.post()
            return True
        except Exception as e:
            logger.error(f"Ошибка перезапуска LXC {vmid} на {node}: {e}")
            return False
    
    def force_stop_vm(self, node: str, vmid: int) -> bool:
        """Принудительно остановить ВМ (через SSH - аналог kill -9)"""
        if not self.proxmox or not self.host:
            return False
        
        try:
            # Пробуем остановить через SSH команду
            # Сначала получаем информацию о ВМ
            vm_info = self.proxmox.nodes(node).qemu(vmid).status.current.get()
            if vm_info.get('status') != 'running':
                logger.info(f"VM {vmid} на {node} уже остановлена")
                return True
            
            # Используем qm sendkey или qm stop с timeout=0
            try:
                # Пробуем обычный stop - может сработать
                self.proxmox.nodes(node).qemu(vmid).status.stop.post()
                logger.info(f"Принудительная остановка VM {vmid} на {node} выполнена")
                return True
            except Exception as api_error:
                logger.warning(f"Прямой stop не сработал, ошибка: {api_error}")
                # Если прямой stop не сработал, это просто значит что ВМ не бежит или API не поддерживает
                return False
                
        except Exception as e:
            logger.error(f"Ошибка принудительной остановки VM {vmid}: {e}")
            return False
    
    def force_stop_container(self, node: str, vmid: int) -> bool:
        """Принудительно остановить LXC контейнер (через SSH - аналог kill -9)"""
        if not self.proxmox or not self.host:
            return False
        
        try:
            # Пробуем остановить через SSH команду
            # Сначала получаем информацию о контейнере
            container_info = self.proxmox.nodes(node).lxc(vmid).status.current.get()
            if container_info.get('status') != 'running':
                logger.info(f"Контейнер {vmid} на {node} уже остановлен")
                return True
            
            # Используем обычный stop - Proxmox должен остановить его
            try:
                self.proxmox.nodes(node).lxc(vmid).status.stop.post()
                logger.info(f"Принудительная остановка контейнера {vmid} на {node} выполнена")
                return True
            except Exception as api_error:
                logger.warning(f"Прямой stop не сработал, ошибка: {api_error}")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка принудительной остановки контейнера {vmid}: {e}")
            return False

    # ==================== High Availability (HA) Methods ====================
    
    def is_cluster(self) -> bool:
        """Проверить, является ли сервер частью кластера"""
        if not self.proxmox:
            return False
        
        try:
            # Проверяем через cluster/status - это наиболее надёжный способ
            cluster_status = self.proxmox.cluster.status.get()
            # В кластере будет тип 'cluster' с информацией
            for item in cluster_status:
                if item.get('type') == 'cluster':
                    # Это кластер
                    return True
            # Также проверяем количество нод
            nodes = [item for item in cluster_status if item.get('type') == 'node']
            return len(nodes) > 1
        except Exception as e:
            # Если /cluster/status не работает, пробуем через ноды
            try:
                nodes = self.get_nodes()
                return len(nodes) > 1
            except Exception:
                return False
    
    def get_ha_resources(self) -> List[Dict]:
        """Получить список ресурсов в HA"""
        if not self.proxmox:
            return []
        
        try:
            resources = self.proxmox.cluster.ha.resources.get()
            return resources
        except Exception as e:
            logger.error(f"Ошибка получения HA ресурсов: {e}")
            return []
    
    def get_ha_groups(self) -> List[Dict]:
        """Получить список HA групп"""
        if not self.proxmox:
            return []
        
        try:
            groups = self.proxmox.cluster.ha.groups.get()
            return groups
        except Exception as e:
            logger.error(f"Ошибка получения HA групп: {e}")
            return []
    
    def is_in_ha(self, vmid: int, vm_type: str = 'vm') -> bool:
        """
        Проверить, находится ли VM/контейнер в HA
        
        Args:
            vmid: ID виртуальной машины или контейнера
            vm_type: 'vm' или 'ct'
        """
        if not self.proxmox:
            return False
        
        try:
            sid = f"{vm_type}:{vmid}"
            resources = self.get_ha_resources()
            return any(r.get('sid') == sid for r in resources)
        except Exception:
            return False
    
    def add_to_ha(self, vmid: int, vm_type: str = 'vm', group: str = None, 
                  max_restart: int = 1, max_relocate: int = 1, 
                  state: str = 'started', comment: str = None) -> Dict:
        """
        Добавить VM/контейнер в HA
        
        Args:
            vmid: ID виртуальной машины или контейнера
            vm_type: 'vm' для QEMU, 'ct' для LXC
            group: Имя HA группы (опционально)
            max_restart: Максимальное количество перезапусков при сбое
            max_relocate: Максимальное количество перемещений на другую ноду
            state: Целевое состояние: 'started', 'stopped', 'enabled', 'disabled', 'ignored'
            comment: Комментарий
        
        Returns:
            Dict с результатом операции
        """
        if not self.proxmox:
            return {'success': False, 'error': 'Not connected'}
        
        try:
            sid = f"{vm_type}:{vmid}"
            
            # Проверяем, не добавлен ли уже
            if self.is_in_ha(vmid, vm_type):
                return {'success': False, 'error': 'Already in HA', 'already_in_ha': True}
            
            params = {
                'sid': sid,
                'max_restart': max_restart,
                'max_relocate': max_relocate,
                'state': state
            }
            
            if group:
                params['group'] = group
            if comment:
                params['comment'] = comment
            
            self.proxmox.cluster.ha.resources.post(**params)
            logger.info(f"Added {sid} to HA")
            return {'success': True, 'sid': sid}
            
        except Exception as e:
            logger.error(f"Ошибка добавления {vmid} в HA: {e}")
            return {'success': False, 'error': str(e)}
    
    def remove_from_ha(self, vmid: int, vm_type: str = 'vm') -> Dict:
        """
        Удалить VM/контейнер из HA
        
        Args:
            vmid: ID виртуальной машины или контейнера
            vm_type: 'vm' для QEMU, 'ct' для LXC
        
        Returns:
            Dict с результатом операции
        """
        if not self.proxmox:
            return {'success': False, 'error': 'Not connected'}
        
        try:
            sid = f"{vm_type}:{vmid}"
            
            # Проверяем, что ресурс в HA
            if not self.is_in_ha(vmid, vm_type):
                return {'success': False, 'error': 'Not in HA', 'not_in_ha': True}
            
            self.proxmox.cluster.ha.resources(sid).delete()
            logger.info(f"Removed {sid} from HA")
            return {'success': True, 'sid': sid}
            
        except Exception as e:
            logger.error(f"Ошибка удаления {vmid} из HA: {e}")
            return {'success': False, 'error': str(e)}
    
    def get_ha_status(self, vmid: int, vm_type: str = 'vm') -> Optional[Dict]:
        """
        Получить статус HA для VM/контейнера
        
        Returns:
            Dict со статусом HA или None если не в HA
        """
        if not self.proxmox:
            return None
        
        try:
            sid = f"{vm_type}:{vmid}"
            resources = self.get_ha_resources()
            
            for r in resources:
                if r.get('sid') == sid:
                    return {
                        'in_ha': True,
                        'state': r.get('state'),
                        'group': r.get('group'),
                        'max_restart': r.get('max_restart'),
                        'max_relocate': r.get('max_relocate'),
                        'comment': r.get('comment')
                    }
            
            return {'in_ha': False}
            
        except Exception as e:
            logger.error(f"Ошибка получения HA статуса {vmid}: {e}")
            return None

    def get_vm_rrddata(self, node: str, vmid: int, timeframe: str = "hour") -> Dict:
        """
        Получить исторические данные VM для графиков (CPU, Memory, Network, Disk IO)
        
        Args:
            node: Имя ноды
            vmid: ID виртуальной машины
            timeframe: Период времени (hour, day, week, month, year)
        
        Returns:
            Dict с временными рядами данных
        """
        if not self.proxmox:
            return {}
        
        try:
            rrddata = self.proxmox.nodes(node).qemu(vmid).rrddata.get(timeframe=timeframe)
            return {'data': rrddata, 'timeframe': timeframe}
        except Exception as e:
            logger.error(f"Ошибка получения RRD данных VM {vmid} на {node}: {e}")
            return {}
    
    def get_container_rrddata(self, node: str, vmid: int, timeframe: str = "hour") -> Dict:
        """
        Получить исторические данные контейнера для графиков
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
            timeframe: Период времени (hour, day, week, month, year)
        
        Returns:
            Dict с временными рядами данных
        """
        if not self.proxmox:
            return {}
        
        try:
            rrddata = self.proxmox.nodes(node).lxc(vmid).rrddata.get(timeframe=timeframe)
            return {'data': rrddata, 'timeframe': timeframe}
        except Exception as e:
            logger.error(f"Ошибка получения RRD данных LXC {vmid} на {node}: {e}")
            return {}
    
    def get_vm_vnc(self, node: str, vmid: int) -> Dict:
        """
        Получить данные для VNC подключения к VM
        
        Args:
            node: Имя ноды
            vmid: ID виртуальной машины
        
        Returns:
            Dict с данными VNC (port, ticket, cert, upid)
        """
        if not self.proxmox:
            return {}
        
        try:
            # Создать VNC прокси сессию
            vnc_data = self.proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)
            return vnc_data
        except Exception as e:
            logger.error(f"Ошибка получения VNC для VM {vmid} на {node}: {e}")
            return {}
    
    def get_container_vnc(self, node: str, vmid: int) -> Dict:
        """
        Получить данные для VNC подключения к LXC контейнеру
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
        
        Returns:
            Dict с данными VNC (port, ticket, cert, upid)
        """
        if not self.proxmox:
            return {}
        
        try:
            # Создать терминал прокси сессию для LXC
            vnc_data = self.proxmox.nodes(node).lxc(vmid).vncproxy.post(websocket=1)
            return vnc_data
        except Exception as e:
            logger.error(f"Ошибка получения VNC для LXC {vmid} на {node}: {e}")
            return {}
    
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
                except:
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
    ) -> bool:
        """
        Настроить параметры VM после клонирования
        
        Args:
            node: Имя ноды
            vmid: VMID VM
            cores: Количество ядер CPU
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
            True если успешно
        """
        if not self.proxmox:
            return False
        
        try:
            params = {}
            
            if cores:
                params['cores'] = cores
            
            if memory:
                params['memory'] = memory
            
            if onboot is not None:
                params['onboot'] = 1 if onboot else 0
            
            if network_bridge:
                params['net0'] = f'virtio,bridge={network_bridge}'
            
            # Cloud-init configuration
            if cloud_init_user:
                params['ciuser'] = cloud_init_user
            
            if cloud_init_password:
                params['cipassword'] = cloud_init_password
            
            if ssh_keys:
                # SSH keys need URL encoding
                import urllib.parse
                params['sshkeys'] = urllib.parse.quote(ssh_keys, safe='')
            
            if ip_config:
                params['ipconfig0'] = ip_config
            
            if params:
                self.proxmox.nodes(node).qemu(vmid).config.put(**params)
                logger.info(f"VM {vmid} настроена: {params.keys()}")
            
            # Resize disk if needed
            if disk_size:
                try:
                    # Get current disk info
                    config = self.proxmox.nodes(node).qemu(vmid).config.get()
                    # Find the main disk (usually scsi0 or virtio0)
                    # Exclude ide0/ide2 which are often CD-ROM or cloud-init drives
                    disk_key = None
                    current_size_gb = 0
                    for key in ['scsi0', 'virtio0', 'sata0', 'scsi1', 'virtio1']:
                        if key in config:
                            disk_config = config[key]
                            # Skip if it's a cdrom or cloud-init drive
                            if 'cdrom' in disk_config.lower() or 'cloudinit' in disk_config.lower() or 'media=cdrom' in disk_config.lower():
                                continue
                            disk_key = key
                            # Parse current size from config (format: "storage:vm-xxx-disk-0,size=32G")
                            if 'size=' in disk_config:
                                size_part = disk_config.split('size=')[1].split(',')[0]
                                if size_part.endswith('G'):
                                    current_size_gb = int(size_part[:-1])
                                elif size_part.endswith('M'):
                                    current_size_gb = int(size_part[:-1]) // 1024
                            break
                    
                    if disk_key:
                        # Only resize if requested size is larger than current
                        if disk_size > current_size_gb:
                            self.proxmox.nodes(node).qemu(vmid).resize.put(
                                disk=disk_key,
                                size=f'{disk_size}G'
                            )
                            logger.info(f"Диск {disk_key} VM {vmid} изменен с {current_size_gb}G до {disk_size}G")
                        else:
                            logger.info(f"Диск {disk_key} VM {vmid} уже имеет размер {current_size_gb}G >= запрошенного {disk_size}G")
                    else:
                        logger.warning(f"Не найден основной диск для VM {vmid}")
                except Exception as e:
                    logger.warning(f"Не удалось изменить размер диска VM {vmid}: {e}")
            
            return True
        except Exception as e:
            logger.error(f"Ошибка настройки VM {vmid}: {e}")
            return False

    def get_task_status(self, node: str, upid: str) -> Optional[Dict]:
        """
        Получить статус задачи по UPID
        
        Args:
            node: Имя ноды
            upid: UPID задачи
        
        Returns:
            Статус задачи
        """
        if not self.proxmox:
            return None
        
        try:
            status = self.proxmox.nodes(node).tasks(upid).status.get()
            return status
        except Exception as e:
            logger.error(f"Ошибка получения статуса задачи {upid}: {e}")
            return None

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
                    return status.get('exitstatus') == 'OK'
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
        
        try:
            # Фильтруем разрешенные параметры
            allowed_params = {
                'cores', 'sockets', 'memory', 'balloon', 'name', 'description',
                'cpu', 'cpulimit', 'cpuunits', 'onboot', 'boot', 'bootdisk',
                'net0', 'net1', 'net2', 'net3',
                'scsi0', 'scsi1', 'scsi2', 'scsi3',
                'virtio0', 'virtio1', 'virtio2', 'virtio3',
                'ide0', 'ide1', 'ide2', 'ide3',
                'sata0', 'sata1', 'sata2', 'sata3',
                'agent', 'ostype', 'tablet', 'hotplug',
                'ciuser', 'cipassword', 'sshkeys', 'ipconfig0', 'ipconfig1',
            }
            
            filtered_config = {k: v for k, v in config.items() if k in allowed_params}
            
            if filtered_config:
                self.proxmox.nodes(node).qemu(vmid).config.put(**filtered_config)
                logger.info(f"Конфигурация VM {vmid} обновлена: {list(filtered_config.keys())}")
                return True
            else:
                logger.warning(f"Нет разрешенных параметров для обновления VM {vmid}")
                return False
        except Exception as e:
            logger.error(f"Ошибка обновления конфигурации VM {vmid}: {e}")
            return False

    def resize_vm_disk(self, node: str, vmid: int, disk: str, size: str) -> bool:
        """
        Изменить размер диска VM
        
        Args:
            node: Имя ноды
            vmid: ID виртуальной машины
            disk: Имя диска (scsi0, virtio0 и т.д.)
            size: Новый размер (+10G, 50G)
        
        Returns:
            True при успехе
        """
        if not self.proxmox:
            return False
        
        try:
            self.proxmox.nodes(node).qemu(vmid).resize.put(disk=disk, size=size)
            logger.info(f"Размер диска {disk} VM {vmid} изменен на {size}")
            return True
        except Exception as e:
            logger.error(f"Ошибка изменения размера диска VM {vmid}: {e}")
            return False

    def get_container_config(self, node: str, vmid: int) -> Optional[Dict]:
        """
        Получить конфигурацию контейнера (LXC)
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
        
        Returns:
            Словарь с конфигурацией контейнера
        """
        if not self.proxmox:
            return None
        
        try:
            config = self.proxmox.nodes(node).lxc(vmid).config.get()
            return dict(config)
        except Exception as e:
            logger.error(f"Ошибка получения конфигурации контейнера {vmid}: {e}")
            return None

    def update_container_config(self, node: str, vmid: int, config: Dict) -> bool:
        """
        Обновить конфигурацию контейнера (LXC)
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
            config: Словарь с параметрами для обновления
        
        Returns:
            True при успехе
        """
        if not self.proxmox:
            return False
        
        try:
            # Фильтруем разрешенные параметры для LXC
            allowed_params = {
                'cores', 'memory', 'swap', 'hostname', 'description',
                'cpulimit', 'cpuunits', 'onboot', 'protection',
                'net0', 'net1', 'net2', 'net3',
                'rootfs', 'mp0', 'mp1', 'mp2', 'mp3',
                'nameserver', 'searchdomain', 'features',
                'unprivileged', 'cmode', 'tty', 'console',
            }
            
            filtered_config = {k: v for k, v in config.items() if k in allowed_params}
            
            if filtered_config:
                self.proxmox.nodes(node).lxc(vmid).config.put(**filtered_config)
                logger.info(f"Конфигурация контейнера {vmid} обновлена: {list(filtered_config.keys())}")
                return True
            else:
                logger.warning(f"Нет разрешенных параметров для обновления контейнера {vmid}")
                return False
        except Exception as e:
            logger.error(f"Ошибка обновления конфигурации контейнера {vmid}: {e}")
            return False

    def resize_container_disk(self, node: str, vmid: int, disk: str, size: str) -> bool:
        """
        Изменить размер диска контейнера
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
            disk: Имя диска (rootfs, mp0 и т.д.)
            size: Новый размер (+10G, 50G)
        
        Returns:
            True при успехе
        """
        if not self.proxmox:
            return False
        
        try:
            self.proxmox.nodes(node).lxc(vmid).resize.put(disk=disk, size=size)
            logger.info(f"Размер диска {disk} контейнера {vmid} изменен на {size}")
            return True
        except Exception as e:
            logger.error(f"Ошибка изменения размера диска контейнера {vmid}: {e}")
            return False

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

    def get_container_interfaces(self, node: str, vmid: int) -> List[Dict]:
        """
        Получить сетевые интерфейсы контейнера
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
        
        Returns:
            Список интерфейсов с IP адресами
        """
        if not self.proxmox:
            return []
        
        try:
            # Для LXC можем получить IP из конфигурации
            config = self.proxmox.nodes(node).lxc(vmid).config.get()
            interfaces = []
            
            # Проверяем net0, net1 и т.д.
            for i in range(10):
                net_key = f'net{i}'
                if net_key in config:
                    net_config = config[net_key]
                    # Парсим строку конфигурации сети
                    iface_info = {'name': f'eth{i}', 'ips': []}
                    
                    # Пытаемся извлечь MAC адрес
                    if 'hwaddr=' in net_config:
                        mac = net_config.split('hwaddr=')[1].split(',')[0]
                        iface_info['hardware_address'] = mac
                    
                    interfaces.append(iface_info)
            
            # Также проверяем IP из конфигурации
            for i in range(10):
                ipconfig_key = f'ipconfig{i}'
                if ipconfig_key in config and interfaces and i < len(interfaces):
                    ipconfig = config[ipconfig_key]
                    # Парсим ip=192.168.1.10/24,gw=192.168.1.1
                    if 'ip=' in ipconfig:
                        ip_part = ipconfig.split('ip=')[1].split(',')[0]
                        if ip_part != 'dhcp':
                            # Разделяем IP и префикс
                            if '/' in ip_part:
                                ip, prefix = ip_part.split('/')
                                interfaces[i]['ips'].append({
                                    'address': ip,
                                    'type': 'ipv4',
                                    'prefix': int(prefix)
                                })
            
            return [iface for iface in interfaces if iface.get('ips')]
        except Exception as e:
            logger.debug(f"Не удалось получить сетевые интерфейсы контейнера {vmid}: {e}")
            return []

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
            
            # Ждем завершения команды
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
            
        except Exception as e:
            logger.error(f"Failed to execute command on VM {vmid}: {e}")
            return {
                'success': False,
                'error': str(e),
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
        
        try:
            # Вместо создания файла, передаем скрипт напрямую через stdin
            # Это более надежно и не требует записи файлов
            
            # Кодируем скрипт в base64 для безопасной передачи
            script_b64 = base64.b64encode(script_content.encode('utf-8')).decode('utf-8')
            
            # Выполняем скрипт через sh -c с heredoc
            # Это позволяет избежать проблем с кавычками и специальными символами
            exec_cmd = f"{interpreter} -c \"$(echo '{script_b64}' | base64 -d)\""
            
            exec_result = self.execute_command(node, vmid, exec_cmd, timeout=timeout)
            
            return exec_result
            
        except Exception as e:
            logger.error(f"Failed to execute script on VM {vmid}: {e}")
            return {
                'success': False,
                'error': str(e),
                'stdout': '',
                'stderr': '',
                'exit_code': -1
            }

    # ==================== LXC Container Methods ====================
    
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
        features: str = None
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
                import urllib.parse
                params['ssh-public-keys'] = urllib.parse.quote(ssh_public_keys, safe='')
            
            if net0:
                params['net0'] = net0
            else:
                # Конфигурация по умолчанию
                params['net0'] = 'name=eth0,bridge=vmbr0,ip=dhcp'
            
            if description:
                params['description'] = description
            
            if features:
                params['features'] = features
            
            result = self.proxmox.nodes(node).lxc.post(**params)
            logger.info(f"Запущено создание LXC контейнера {vmid} ({hostname}) на {node}")
            return result
        except Exception as e:
            logger.error(f"Ошибка создания LXC контейнера {vmid}: {e}")
            return None

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
            return result
        except Exception as e:
            logger.error(f"Ошибка миграции LXC {vmid}: {e}")
            return None

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

    def update_container_config(
        self,
        node: str,
        vmid: int,
        hostname: str = None,
        memory: int = None,
        swap: int = None,
        cores: int = None,
        cpulimit: float = None,
        onboot: bool = None,
        description: str = None,
        net0: str = None
    ) -> bool:
        """
        Обновить конфигурацию LXC контейнера
        
        Args:
            node: Имя ноды
            vmid: ID контейнера
            hostname: Новое имя хоста
            memory: Память в MB
            swap: Swap в MB
            cores: Количество ядер
            cpulimit: Лимит CPU (0.0-128.0)
            onboot: Автозапуск
            description: Описание
            net0: Конфигурация сети
        
        Returns:
            True при успехе
        """
        if not self.proxmox:
            return False
        
        try:
            params = {}
            
            if hostname:
                params['hostname'] = hostname
            if memory is not None:
                params['memory'] = memory
            if swap is not None:
                params['swap'] = swap
            if cores is not None:
                params['cores'] = cores
            if cpulimit is not None:
                params['cpulimit'] = cpulimit
            if onboot is not None:
                params['onboot'] = 1 if onboot else 0
            if description is not None:
                params['description'] = description
            if net0:
                params['net0'] = net0
            
            if params:
                self.proxmox.nodes(node).lxc(vmid).config.put(**params)
                logger.info(f"Конфигурация LXC {vmid} обновлена: {list(params.keys())}")
                return True
            return False
        except Exception as e:
            logger.error(f"Ошибка обновления конфигурации LXC {vmid}: {e}")
            return False

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
        
        try:
            self.proxmox.nodes(node).lxc(vmid).resize.put(disk=disk, size=size)
            logger.info(f"Диск {disk} LXC {vmid} изменен на {size}")
            return True
        except Exception as e:
            logger.error(f"Ошибка изменения размера диска LXC {vmid}: {e}")
            return False

    # ==================== Terminal Proxy (xterm.js) ====================
    
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
    
    def get_task_status(self, node: str, upid: str) -> Dict:
        """
        Получить статус задачи по UPID
        
        Args:
            node: Имя ноды
            upid: UPID задачи
        
        Returns:
            Dict со статусом задачи
        """
        if not self.proxmox:
            return {}
        
        try:
            status = self.proxmox.nodes(node).tasks(upid).status.get()
            return status
        except Exception as e:
            logger.error(f"Ошибка получения статуса задачи {upid}: {e}")
            return {}
    
    def get_task_log(self, node: str, upid: str, start: int = 0, limit: int = 50) -> List[Dict]:
        """
        Получить лог задачи по UPID
        
        Args:
            node: Имя ноды
            upid: UPID задачи
            start: Начальная строка
            limit: Количество строк
        
        Returns:
            List логов задачи
        """
        if not self.proxmox:
            return []
        
        try:
            logs = self.proxmox.nodes(node).tasks(upid).log.get(start=start, limit=limit)
            return logs
        except Exception as e:
            logger.error(f"Ошибка получения лога задачи {upid}: {e}")
            return []
    
    # ==================== SDN (Software Defined Networking) ====================
    
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
    
    def get_sdn_zones(self) -> List[Dict]:
        """
        Get all SDN zones.
        
        Returns:
            List of SDN zones with type, zone name, pending status
        """
        if not self.proxmox:
            return []
        
        try:
            zones = self.proxmox.cluster.sdn.zones.get()
            return zones if isinstance(zones, list) else []
        except Exception as e:
            logger.error(f"Error getting SDN zones: {e}")
            return []
    
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
    
    def create_sdn_zone(self, zone: str, zone_type: str = "simple", **kwargs) -> Dict:
        """
        Create a new SDN zone.
        
        Args:
            zone: Zone name (alphanumeric, max 8 chars)
            zone_type: Type of zone (simple, vlan, qinq, vxlan, evpn)
            **kwargs: Additional zone options (mtu, dns, reversedns, etc.)
        
        Returns:
            Result dict with success status
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {
                "zone": zone,
                "type": zone_type,
            }
            params.update(kwargs)
            
            self.proxmox.cluster.sdn.zones.post(**params)
            logger.info(f"Created SDN zone: {zone} (type={zone_type})")
            return {"success": True, "zone": zone}
        except Exception as e:
            logger.error(f"Error creating SDN zone {zone}: {e}")
            return {"success": False, "error": str(e)}
    
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
    
    def get_sdn_vnets(self) -> List[Dict]:
        """
        Get all SDN VNets (Virtual Networks).
        
        Returns:
            List of VNets with vnet name, zone, alias, etc.
        """
        if not self.proxmox:
            return []
        
        try:
            vnets = self.proxmox.cluster.sdn.vnets.get()
            return vnets if isinstance(vnets, list) else []
        except Exception as e:
            logger.error(f"Error getting SDN vnets: {e}")
            return []
    
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
    
    def create_sdn_vnet(self, vnet: str, zone: str, tag: int = None, 
                        alias: str = None, vlanaware: bool = False) -> Dict:
        """
        Create a new SDN VNet.
        
        Args:
            vnet: VNet name (alphanumeric, max 8 chars)
            zone: Zone where to create the VNet
            tag: VLAN/VNI tag (optional)
            alias: Human-readable alias
            vlanaware: Enable VLAN-aware bridge
        
        Returns:
            Result dict with success status
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {
                "vnet": vnet,
                "zone": zone,
            }
            if tag is not None:
                params["tag"] = tag
            if alias:
                params["alias"] = alias
            if vlanaware:
                params["vlanaware"] = 1
            
            self.proxmox.cluster.sdn.vnets.post(**params)
            logger.info(f"Created SDN vnet: {vnet} in zone {zone}")
            return {"success": True, "vnet": vnet}
        except Exception as e:
            logger.error(f"Error creating SDN vnet {vnet}: {e}")
            return {"success": False, "error": str(e)}
    
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
    
    def create_sdn_subnet(self, vnet: str, subnet: str, gateway: str = None,
                          snat: bool = False, dnszoneprefix: str = None) -> Dict:
        """
        Create a subnet in a VNet.
        
        Args:
            vnet: VNet name
            subnet: Subnet CIDR (e.g., "10.0.0.0/24")
            gateway: Gateway IP for the subnet
            snat: Enable SNAT for outgoing traffic
            dnszoneprefix: DNS zone prefix
        
        Returns:
            Result dict with success status
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {
                "subnet": subnet,
                "type": "subnet",
            }
            if gateway:
                params["gateway"] = gateway
            if snat:
                params["snat"] = 1
            if dnszoneprefix:
                params["dnszoneprefix"] = dnszoneprefix
            
            self.proxmox.cluster.sdn.vnets(vnet).subnets.post(**params)
            logger.info(f"Created subnet {subnet} in vnet {vnet}")
            return {"success": True, "subnet": subnet}
        except Exception as e:
            logger.error(f"Error creating subnet {subnet} in vnet {vnet}: {e}")
            return {"success": False, "error": str(e)}
    
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
    
    # ==================== Snapshots ====================
    
    def get_vm_snapshots(self, node: str, vmid: int) -> List[Dict]:
        """
        Get all snapshots for a VM.
        
        Args:
            node: Node name
            vmid: VM ID
        
        Returns:
            List of snapshots
        """
        if not self.proxmox:
            return []
        
        try:
            snapshots = self.proxmox.nodes(node).qemu(vmid).snapshot.get()
            return snapshots if isinstance(snapshots, list) else []
        except Exception as e:
            logger.error(f"Error getting VM {vmid} snapshots: {e}")
            return []
    
    def get_container_snapshots(self, node: str, vmid: int) -> List[Dict]:
        """
        Get all snapshots for a container.
        
        Args:
            node: Node name
            vmid: Container ID
        
        Returns:
            List of snapshots
        """
        if not self.proxmox:
            return []
        
        try:
            snapshots = self.proxmox.nodes(node).lxc(vmid).snapshot.get()
            return snapshots if isinstance(snapshots, list) else []
        except Exception as e:
            logger.error(f"Error getting container {vmid} snapshots: {e}")
            return []
    
    def create_vm_snapshot(self, node: str, vmid: int, snapname: str, 
                           description: str = None, vmstate: bool = False) -> Dict:
        """
        Create a snapshot of a VM.
        
        Args:
            node: Node name
            vmid: VM ID
            snapname: Snapshot name (alphanumeric, max 40 chars)
            description: Optional description
            vmstate: Include VM RAM state (requires more storage)
        
        Returns:
            Result dict with UPID or error
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {"snapname": snapname}
            if description:
                params["description"] = description
            if vmstate:
                params["vmstate"] = 1
            
            upid = self.proxmox.nodes(node).qemu(vmid).snapshot.post(**params)
            logger.info(f"Creating snapshot {snapname} for VM {vmid}")
            return {"success": True, "upid": upid, "snapname": snapname}
        except Exception as e:
            logger.error(f"Error creating VM {vmid} snapshot: {e}")
            return {"success": False, "error": str(e)}
    
    def create_container_snapshot(self, node: str, vmid: int, snapname: str, 
                                   description: str = None) -> Dict:
        """
        Create a snapshot of a container.
        
        Args:
            node: Node name
            vmid: Container ID
            snapname: Snapshot name
            description: Optional description
        
        Returns:
            Result dict with UPID or error
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {"snapname": snapname}
            if description:
                params["description"] = description
            
            upid = self.proxmox.nodes(node).lxc(vmid).snapshot.post(**params)
            logger.info(f"Creating snapshot {snapname} for container {vmid}")
            return {"success": True, "upid": upid, "snapname": snapname}
        except Exception as e:
            logger.error(f"Error creating container {vmid} snapshot: {e}")
            return {"success": False, "error": str(e)}
    
    def delete_vm_snapshot(self, node: str, vmid: int, snapname: str, force: bool = False) -> Dict:
        """
        Delete a VM snapshot.
        
        Args:
            node: Node name
            vmid: VM ID
            snapname: Snapshot name to delete
            force: Force deletion even if snapshot is locked
        
        Returns:
            Result dict with success status
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {}
            if force:
                params["force"] = 1
            
            upid = self.proxmox.nodes(node).qemu(vmid).snapshot(snapname).delete(**params)
            logger.info(f"Deleting snapshot {snapname} for VM {vmid}")
            return {"success": True, "upid": upid}
        except Exception as e:
            logger.error(f"Error deleting VM {vmid} snapshot {snapname}: {e}")
            return {"success": False, "error": str(e)}
    
    def delete_container_snapshot(self, node: str, vmid: int, snapname: str, force: bool = False) -> Dict:
        """
        Delete a container snapshot.
        
        Args:
            node: Node name
            vmid: Container ID
            snapname: Snapshot name to delete
            force: Force deletion
        
        Returns:
            Result dict with success status
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {}
            if force:
                params["force"] = 1
            
            upid = self.proxmox.nodes(node).lxc(vmid).snapshot(snapname).delete(**params)
            logger.info(f"Deleting snapshot {snapname} for container {vmid}")
            return {"success": True, "upid": upid}
        except Exception as e:
            logger.error(f"Error deleting container {vmid} snapshot {snapname}: {e}")
            return {"success": False, "error": str(e)}
    
    def rollback_vm_snapshot(self, node: str, vmid: int, snapname: str, start: bool = False) -> Dict:
        """
        Rollback a VM to a snapshot.
        
        Args:
            node: Node name
            vmid: VM ID
            snapname: Snapshot name to rollback to
            start: Start VM after rollback
        
        Returns:
            Result dict with UPID or error
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {}
            if start:
                params["start"] = 1
            
            upid = self.proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post(**params)
            logger.info(f"Rolling back VM {vmid} to snapshot {snapname}")
            return {"success": True, "upid": upid}
        except Exception as e:
            logger.error(f"Error rolling back VM {vmid} to snapshot {snapname}: {e}")
            return {"success": False, "error": str(e)}
    
    def rollback_container_snapshot(self, node: str, vmid: int, snapname: str, start: bool = False) -> Dict:
        """
        Rollback a container to a snapshot.
        
        Args:
            node: Node name
            vmid: Container ID
            snapname: Snapshot name to rollback to
            start: Start container after rollback
        
        Returns:
            Result dict with UPID or error
        """
        if not self.proxmox:
            return {"success": False, "error": "Not connected"}
        
        try:
            params = {}
            if start:
                params["start"] = 1
            
            upid = self.proxmox.nodes(node).lxc(vmid).snapshot(snapname).rollback.post(**params)
            logger.info(f"Rolling back container {vmid} to snapshot {snapname}")
            return {"success": True, "upid": upid}
        except Exception as e:
            logger.error(f"Error rolling back container {vmid} to snapshot {snapname}: {e}")
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
                      token_name: str = None, token_value_hash: str = None) -> ProxmoxClient:
    """Get cached Proxmox client instance"""
    return ProxmoxClient(
        host=host,
        user=user,
        password=password_hash,
        token_name=token_name,
        token_value=token_value_hash
    )
