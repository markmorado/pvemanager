from typing import List, Dict, Optional, Union, Any
import time
import urllib3
import shlex
from loguru import logger

class LxcMixin:
        def unlock_container(self, node: str, vmid: int) -> Dict:
            """Снять блокировку (lock) с LXC — аналог `pct unlock`.

            config.put(delete='lock', skiplock=1); skiplock принимается только
            для root@pam.
            """
            if not self.proxmox:
                return {"success": False, "error": "Not connected"}
            try:
                self.proxmox.nodes(node).lxc(vmid).config.put(delete="lock", skiplock=1)
                logger.info(f"С LXC {vmid} снята блокировка")
                return {"success": True}
            except Exception as e:
                logger.error(f"Ошибка снятия блокировки LXC {vmid}: {e}")
                return {"success": False, "error": str(e)}

        def start_container(self, node: str, vmid: int) -> Optional[str]:
            """Запустить LXC контейнер. Возвращает UPID задачи или None при ошибке."""
            if not self.proxmox:
                return None
            
            try:
                upid = self.proxmox.nodes(node).lxc(vmid).status.start.post()
                return upid if isinstance(upid, str) else None
            except Exception as e:
                logger.error(f"Ошибка запуска LXC {vmid} на {node}: {e}")
                return None

        def stop_container(self, node: str, vmid: int, force: bool = False) -> Optional[str]:
            """Остановить LXC контейнер. Возвращает UPID задачи или None при ошибке."""
            if not self.proxmox:
                return None
            
            try:
                upid = self.proxmox.nodes(node).lxc(vmid).status.stop.post()
                return upid if isinstance(upid, str) else None
            except Exception as e:
                logger.error(f"Ошибка остановки LXC {vmid} на {node}: {e}")
                return None

        def restart_container(self, node: str, vmid: int) -> Optional[str]:
            """Перезапустить LXC контейнер. Возвращает UPID задачи или None при ошибке."""
            if not self.proxmox:
                return None
            
            try:
                upid = self.proxmox.nodes(node).lxc(vmid).status.reboot.post()
                return upid if isinstance(upid, str) else None
            except Exception as e:
                logger.error(f"Ошибка перезапуска LXC {vmid} на {node}: {e}")
                return None

        def shutdown_container(self, node: str, vmid: int, timeout: int = 60) -> Optional[str]:
            """Корректно выключить LXC. Возвращает UPID задачи."""
            if not self.proxmox:
                return None
            try:
                upid = self.proxmox.nodes(node).lxc(vmid).status.shutdown.post(timeout=timeout)
                return upid if isinstance(upid, str) else None
            except Exception as e:
                logger.error(f"Ошибка корректного выключения LXC {vmid} на {node}: {e}")
                return None

        def clone_container(self, node: str, vmid: int, new_vmid: int, hostname: str,
                            full: bool = True, target_node: Optional[str] = None,
                            target_storage: Optional[str] = None,
                            description: Optional[str] = None) -> Optional[str]:
            """Клонировать LXC контейнер."""
            if not self.proxmox:
                return None
            try:
                params = {'newid': new_vmid, 'hostname': hostname, 'full': 1 if full else 0}
                if target_storage:
                    params['storage'] = target_storage
                if target_node and target_node != node:
                    params['target'] = target_node
                if description:
                    params['description'] = description
                upid = self.proxmox.nodes(node).lxc(vmid).clone.post(**params)
                return upid if isinstance(upid, str) else None
            except Exception as e:
                logger.error(f"Ошибка клонирования LXC {vmid} -> {new_vmid}: {e}")
                return None

        def change_container_password(self, node: str, vmid: int, username: str, password: str) -> Dict:
            """
            Сменить пароль пользователя в LXC через `pct exec` (chpasswd).
            Контейнер должен быть запущен.

            REST-эндпоинта /nodes/{node}/lxc/{vmid}/exec в Proxmox нет (501),
            поэтому выполняем pct exec по SSH (root@node) — тот же паттерн,
            что в appstore/pipeline.py и _delete_cluster_node_via_ssh.
            """
            if not self.proxmox:
                return {'success': False, 'error': 'Proxmox connection not initialized'}
            try:
                result = self.proxmox.nodes(node).lxc(vmid).status.current.get()
                if result.get('status') != 'running':
                    return {'success': False, 'error': 'Container is not running'}
            except Exception as e:
                logger.error(f"Не удалось получить статус LXC {vmid}: {e}")
                return {'success': False, 'error': str(e)}

            from app.ssh_client import SSHClient
            # self.host может содержать порт API ("1.2.3.4:8006"); SSH нужен голый хост.
            ssh_host = self.host.split(':')[0] if self.host else self.host
            ssh = SSHClient(
                hostname=ssh_host,
                username='root',
                password=getattr(self, '_password', None),
            )
            try:
                if not ssh.connect():
                    return {'success': False, 'error': ssh.last_error or 'SSH connection to node failed'}
                # printf + chpasswd; shlex.quote защищает от shell-injection
                entry = shlex.quote(f'{username}:{password}')
                inner = f"printf '%s\\n' {entry} | chpasswd"
                cmd = f"pct exec {vmid} -- bash -c {shlex.quote(inner)}"
                output, exit_code = ssh.execute(cmd, return_exit_code=True)
                if exit_code == 0:
                    return {'success': True}
                return {'success': False, 'error': output or f'chpasswd failed (exit {exit_code})'}
            except Exception as e:
                logger.error(f"Не удалось сменить пароль в LXC {vmid}: {e}")
                return {'success': False, 'error': str(e)}
            finally:
                ssh.close()

        def set_container_notes(self, node: str, vmid: int, description: str) -> bool:
            """Установить description (заметки) у LXC."""
            if not self.proxmox:
                return False
            try:
                self.proxmox.nodes(node).lxc(vmid).config.put(description=description or '')
                return True
            except Exception as e:
                logger.error(f"Ошибка обновления заметок LXC {vmid}: {e}")
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

        def get_container_stats(self, node: str, vmid: int) -> Optional[Dict]:
            """Получить статистику LXC контейнера (CPU, память, диск)"""
            return self.get_container_status(node, vmid)

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

