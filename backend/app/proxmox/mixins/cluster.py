from typing import List, Dict, Optional, Union, Any
import re
import shlex
import time
import urllib3
from loguru import logger

class ClusterMixin:
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

        def create_cluster(self, cluster_name: str, link0: str = None) -> Dict:
            """
            Создать новый Proxmox кластер на этой ноде.
            Требует аутентификации через пароль (root@pam).
    
            Args:
                cluster_name: Уникальное имя кластера (не изменяемо после создания)
                link0: IP-адрес для corosync link0 (по умолчанию — IP ноды)
    
            Returns:
                Dict с UPID задачи или ошибкой
            """
            if not self.proxmox:
                return {'success': False, 'error': 'Not connected'}
    
            try:
                params = {'clustername': cluster_name}
                if link0:
                    params['link0'] = link0
    
                result = self.proxmox.cluster.config.post(**params)
                logger.info(f"Cluster '{cluster_name}' creation started on {self.host}, UPID: {result}")
                return {'success': True, 'upid': result, 'cluster_name': cluster_name}
            except Exception as e:
                logger.error(f"Ошибка создания кластера '{cluster_name}' на {self.host}: {e}")
                return {'success': False, 'error': str(e)}

        def get_cluster_join_info(self) -> Dict:
            """
            Получить информацию для присоединения к кластеру (fingerprint, join token).
            Вызывается на существующей кластерной ноде.
    
            Returns:
                Dict с полями: totem, nodelist, fingerprint, config_digest
            """
            if not self.proxmox:
                return {'success': False, 'error': 'Not connected'}
    
            try:
                info = self.proxmox.cluster.config.join.get()
                logger.info(f"Got cluster join info from {self.host}")
                return {'success': True, 'data': info}
            except Exception as e:
                logger.error(f"Ошибка получения join info с {self.host}: {e}")
                return {'success': False, 'error': str(e)}

        def join_cluster(self, cluster_host: str, rootpw: str, fingerprint: str,
                         link0: str = None) -> Dict:
            """
            Присоединить эту ноду к существующему кластеру.
            ВАЖНО: Нода должна быть пустой — /etc/pve будет перезаписан.
    
            Args:
                cluster_host: IP/hostname существующей кластерной ноды
                rootpw: root-пароль кластерной ноды (нужен для join)
                fingerprint: SHA-256 fingerprint кластерного сертификата
                link0: Локальный IP для corosync link0 (по умолчанию — IP ноды)
    
            Returns:
                Dict с UPID задачи или ошибкой
            """
            if not self.proxmox:
                return {'success': False, 'error': 'Not connected'}
    
            try:
                params = {
                    'hostname': cluster_host,
                    'password': rootpw,
                    'fingerprint': fingerprint,
                }
                if link0:
                    params['link0'] = link0
    
                result = self.proxmox.cluster.config.join.post(**params)
                logger.info(f"Node {self.host} joining cluster at {cluster_host}, UPID: {result}")
                return {'success': True, 'upid': result}
            except Exception as e:
                logger.error(f"Ошибка присоединения {self.host} к кластеру {cluster_host}: {e}")
                return {'success': False, 'error': str(e)}

        def delete_cluster_node(self, node_name: str) -> Dict:
            """
            Удалить ноду из кластера (pvecm delnode).
            Вызывается на ДРУГОЙ ноде кластера, не на удаляемой.
            Удаляемая нода должна быть ВЫКЛЮЧЕНА.
    
            Args:
                node_name: Имя ноды для удаления (hostname)
    
            Returns:
                Dict с результатом
            """
            if not self.proxmox:
                return {'success': False, 'error': 'Not connected'}
    
            try:
                # Proxmox PVE 8 API: DELETE /nodes/{node}
                self.proxmox.nodes(node_name).delete()
                logger.info(f"Node '{node_name}' deleted from cluster via {self.host}")
                return {'success': True, 'node': node_name}
            except Exception as e:
                logger.warning(f"REST API delete node failed, trying SSH fallback: {e}")
                # SSH fallback через pvecm delnode
                return self._delete_cluster_node_via_ssh(node_name)

        def _delete_cluster_node_via_ssh(self, node_name: str) -> Dict:
            """SSH fallback для удаления ноды через pvecm delnode"""
            from app.ssh_client import SSHClient
            # self.host may carry an API port suffix (e.g. "1.2.3.4:8006"); SSH needs the bare host.
            ssh_host = self.host.split(':')[0] if self.host else self.host
            ssh = SSHClient(
                hostname=ssh_host,
                username='root',
                password=getattr(self, '_password', None),
            )
            try:
                if not ssh.connect():
                    return {'success': False, 'error': ssh.last_error or 'SSH connection failed'}
    
                # node_name comes from the API request and runs in a root shell.
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", node_name or ""):
                    return {'success': False, 'error': f'Invalid node name: {node_name!r}'}
                output, exit_code = ssh.execute(f"pvecm delnode {shlex.quote(node_name)}", return_exit_code=True)
                ssh.close()
    
                # "Could not kill node (error = CS_ERR_NOT_EXIST)" — ignorable per Proxmox docs
                if exit_code == 0 or (output and 'CS_ERR_NOT_EXIST' in output):
                    logger.info(f"Node '{node_name}' removed via pvecm delnode (SSH)")
                    return {'success': True, 'node': node_name, 'method': 'ssh'}
                else:
                    return {'success': False, 'error': output or 'pvecm delnode failed'}
            except Exception as e:
                logger.error(f"SSH pvecm delnode failed for {node_name}: {e}")
                return {'success': False, 'error': str(e)}

