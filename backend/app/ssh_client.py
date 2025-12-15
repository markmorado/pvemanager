import logging
import asyncio
from typing import Dict, Optional, Union
from concurrent.futures import ThreadPoolExecutor
import paramiko
from paramiko.ssh_exception import SSHException, NoValidConnectionsError
import socket
from functools import lru_cache
import time

logger = logging.getLogger(__name__)

# Thread pool for blocking SSH operations
ssh_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ssh_")


class SSHClient:
    """Улучшенный SSH клиент для подключения к серверам с кешированием"""
    
    # Connection cache
    _connections = {}
    _connection_times = {}
    
    def __init__(self, hostname: str, port: int = 22, username: str = "root", 
                 key_path: Optional[str] = None, timeout: int = 10, max_retries: int = 3):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.key_path = key_path
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = None
        self.connection_key = f"{hostname}:{port}:{username}"
        self._connected = False
    
    def _get_cached_connection(self) -> Optional[paramiko.SSHClient]:
        """Get cached connection if available and valid"""
        if self.connection_key in self._connections:
            client = self._connections[self.connection_key]
            connection_time = self._connection_times.get(self.connection_key, 0)
            
            # Check if connection is still valid (max 5 minutes)
            if time.time() - connection_time < 300:
                try:
                    # Test connection with a simple command
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        return client
                except:
                    pass
            
            # Remove invalid connection
            self._cleanup_connection()
        
        return None
    
    def _cache_connection(self, client: paramiko.SSHClient):
        """Cache successful connection"""
        self._connections[self.connection_key] = client
        self._connection_times[self.connection_key] = time.time()
    
    def _cleanup_connection(self):
        """Clean up cached connection"""
        if self.connection_key in self._connections:
            try:
                self._connections[self.connection_key].close()
            except:
                pass
            del self._connections[self.connection_key]
            del self._connection_times[self.connection_key]
    
    def connect(self) -> bool:
        """Установить SSH соединение с повторными попытками"""
        # Try to get cached connection first
        cached_client = self._get_cached_connection()
        if cached_client:
            self.client = cached_client
            self._connected = True
            logger.debug(f"Using cached SSH connection to {self.hostname}")
            return True
        
        for attempt in range(self.max_retries):
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                connect_kwargs = {
                    'hostname': self.hostname,
                    'port': self.port,
                    'username': self.username,
                    'timeout': self.timeout,
                    'banner_timeout': self.timeout,
                    'auth_timeout': self.timeout,
                }
                
                # Try key authentication first
                if self.key_path:
                    try:
                        connect_kwargs['key_filename'] = self.key_path
                        client.connect(**connect_kwargs, look_for_keys=False, allow_agent=False)
                    except paramiko.AuthenticationException:
                        logger.debug(f"Key auth failed for {self.hostname}, trying other methods")
                        # Remove key and try other methods
                        del connect_kwargs['key_filename']
                        client.connect(**connect_kwargs, look_for_keys=True, allow_agent=True)
                else:
                    client.connect(**connect_kwargs, look_for_keys=True, allow_agent=True)
                
                self.client = client
                self._connected = True
                self._cache_connection(client)
                logger.info(f"Successfully connected to {self.hostname} (attempt {attempt + 1})")
                return True
                
            except paramiko.AuthenticationException as e:
                logger.error(f"Authentication failed for {self.hostname}: {e}")
                return False
            except (SSHException, NoValidConnectionsError, socket.timeout, 
                   socket.error, ConnectionRefusedError) as e:
                logger.warning(f"Connection attempt {attempt + 1} failed for {self.hostname}: {e}")
                if attempt == self.max_retries - 1:
                    logger.error(f"All connection attempts failed for {self.hostname}")
                    return False
                # Wait before retry
                time.sleep(2 ** attempt)  # Exponential backoff
            except Exception as e:
                logger.error(f"Unexpected error connecting to {self.hostname}: {e}")
                return False
        
        return False
    
    def execute(self, command: str, return_exit_code: bool = False) -> Union[Optional[str], tuple]:
        """Выполнить команду на сервере"""
        if not self._connected or not self.client:
            if not self.connect():
                return (None, -1) if return_exit_code else None
        
        try:
            stdin, stdout, stderr = self.client.exec_command(
                command, timeout=self.timeout, get_pty=False
            )
            
            # Wait for command to complete
            exit_code = stdout.channel.recv_exit_status()
            
            output = stdout.read().decode('utf-8', errors='ignore').strip()
            error_output = stderr.read().decode('utf-8', errors='ignore').strip()
            
            if error_output and exit_code != 0:
                logger.warning(f"Command failed on {self.hostname} (exit {exit_code}): {error_output}")
                return (error_output, exit_code) if return_exit_code else None
            
            return (output, exit_code) if return_exit_code else output
            
        except socket.timeout:
            logger.error(f"Command timeout on {self.hostname}: {command}")
            return (None, -1) if return_exit_code else None
        except Exception as e:
            logger.error(f"Error executing command on {self.hostname}: {e}")
            # Connection might be broken, clean up
            self._connected = False
            self._cleanup_connection()
            return (None, -1) if return_exit_code else None
    
    def is_connected(self) -> bool:
        """Check if connection is active"""
        if not self.client or not self._connected:
            return False
        try:
            transport = self.client.get_transport()
            return transport and transport.is_active()
        except:
            return False
    
    def close(self):
        """Закрыть SSH соединение"""
        self._connected = False
        # Don't close cached connections immediately
        self.client = None
    
    def force_close(self):
        """Принудительно закрыть соединение"""
        self._connected = False
        self._cleanup_connection()
        self.client = None
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def get_interactive_shell(self, term: str = "xterm", width: int = 80, height: int = 24):
        """
        Создать интерактивную shell сессию для WebSocket terminal
        
        Args:
            term: Тип терминала (xterm, vt100, etc)
            width: Ширина терминала в символах
            height: Высота терминала в строках
        
        Returns:
            paramiko.Channel объект для интерактивной сессии
        """
        if not self.is_connected():
            if not self.connect():
                raise Exception("Cannot connect to SSH server")
        
        try:
            channel = self.client.invoke_shell(term=term, width=width, height=height)
            channel.settimeout(0.0)  # Non-blocking mode
            return channel
        except Exception as e:
            logger.error(f"Error creating interactive shell on {self.hostname}: {e}")
            raise
    
    def resize_shell(self, channel, width: int, height: int):
        """
        Изменить размер терминала
        
        Args:
            channel: paramiko.Channel объект
            width: Новая ширина в символах
            height: Новая высота в строках
        """
        try:
            channel.resize_pty(width=width, height=height)
        except Exception as e:
            logger.error(f"Error resizing terminal: {e}")
    
    @classmethod
    def cleanup_all_connections(cls):
        """Clean up all cached connections"""
        for key in list(cls._connections.keys()):
            try:
                cls._connections[key].close()
            except:
                pass
        cls._connections.clear()
        cls._connection_times.clear()


def check_server_status(hostname: str, port: int = 22, username: str = "root",
                       key_path: Optional[str] = None) -> Dict:
    """
    Проверить статус сервера и получить системную информацию
    
    Returns:
        Dict с информацией о сервере: online, uptime, cpu, memory, disk
    """
    result = {
        "online": False,
        "uptime": None,
        "cpu_percent": None,
        "memory_percent": None,
        "memory_used": None,
        "memory_total": None,
        "disk_percent": None,
        "disk_used": None,
        "disk_total": None,
        "load_average": None,
        "error": None,
    }
    
    try:
        with SSHClient(hostname, port, username, key_path, timeout=5) as ssh:
            if not ssh.is_connected():
                result["error"] = "SSH connection failed"
                return result
            
            result["online"] = True
            
            # Uptime
            uptime_output = ssh.execute("uptime -p 2>/dev/null || uptime")
            if uptime_output:
                result["uptime"] = uptime_output
            
            # Load average
            load_output = ssh.execute("cat /proc/loadavg 2>/dev/null | awk '{print $1, $2, $3}'")
            if load_output:
                result["load_average"] = load_output
            
            # CPU usage (более точный метод)
            cpu_script = """
                # Get CPU usage over 1 second interval
                cpu_idle=$(top -bn2 -d1 | grep "Cpu(s)" | tail -1 | awk '{print $8}' | cut -d'%' -f1)
                if [ -n "$cpu_idle" ]; then
                    echo "scale=1; 100 - $cpu_idle" | bc 2>/dev/null || echo "0"
                else
                    echo "0"
                fi
            """
            cpu_output = ssh.execute(cpu_script)
            if cpu_output and cpu_output.replace('.', '').isdigit():
                try:
                    result["cpu_percent"] = float(cpu_output)
                except ValueError:
                    pass
            
            # Memory usage
            mem_output = ssh.execute(
                "free -m | awk 'NR==2{printf \"%.1f %.0f %.0f\", $3*100/$2, $3, $2}'"
            )
            if mem_output:
                try:
                    parts = mem_output.split()
                    result["memory_percent"] = float(parts[0])
                    result["memory_used"] = int(parts[1])
                    result["memory_total"] = int(parts[2])
                except (IndexError, ValueError):
                    pass
            
            # Disk usage (root partition)
            disk_output = ssh.execute(
                "df -h / | awk 'NR==2{printf \"%.1f %s %s\", $5+0, $3, $2}'"
            )
            if disk_output:
                try:
                    parts = disk_output.split()
                    result["disk_percent"] = float(parts[0])
                    result["disk_used"] = parts[1]
                    result["disk_total"] = parts[2]
                except (IndexError, ValueError):
                    pass
            
    except Exception as e:
        logger.error(f"Error checking status of {hostname}: {e}")
        result["error"] = str(e)
    
    return result


async def async_check_server_status(hostname: str, port: int = 22, username: str = "root",
                                  key_path: Optional[str] = None) -> Dict:
    """Асинхронная версия проверки статуса сервера"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        ssh_executor, 
        check_server_status, 
        hostname, port, username, key_path
    )


def test_ssh_connection(hostname: str, port: int = 22, username: str = "root",
                       key_path: Optional[str] = None) -> bool:
    """Быстрая проверка SSH подключения"""
    try:
        with SSHClient(hostname, port, username, key_path, timeout=3) as ssh:
            return ssh.is_connected()
    except Exception as e:
        logger.debug(f"SSH connection test failed for {hostname}: {e}")
        return False


async def async_test_ssh_connection(hostname: str, port: int = 22, username: str = "root",
                                  key_path: Optional[str] = None) -> bool:
    """Асинхронная версия тестирования SSH подключения"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        ssh_executor,
        test_ssh_connection,
        hostname, port, username, key_path
    )


async def batch_check_servers_status(servers: list) -> Dict[int, Dict]:
    """Асинхронная проверка статуса множества серверов"""
    tasks = []
    for server in servers:
        task = async_check_server_status(
            hostname=server.ip_address,
            port=server.ssh_port or 22,
            username=server.ssh_user or "root",
            key_path=server.ssh_key_path
        )
        tasks.append((server.id, task))
    
    results = {}
    completed_tasks = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
    
    for i, (server_id, _) in enumerate(tasks):
        result = completed_tasks[i]
        if isinstance(result, Exception):
            results[server_id] = {
                "online": False,
                "error": str(result)
            }
        else:
            results[server_id] = result
    
    return results


# Connection cleanup scheduler
async def cleanup_ssh_connections():
    """Периодическая очистка неиспользуемых SSH подключений"""
    while True:
        try:
            await asyncio.sleep(300)  # Check every 5 minutes
            current_time = time.time()
            expired_keys = []
            
            for key, connection_time in SSHClient._connection_times.items():
                if current_time - connection_time > 600:  # 10 minutes
                    expired_keys.append(key)
            
            for key in expired_keys:
                if key in SSHClient._connections:
                    try:
                        SSHClient._connections[key].close()
                    except:
                        pass
                    del SSHClient._connections[key]
                    del SSHClient._connection_times[key]
                    logger.debug(f"Cleaned up expired SSH connection: {key}")
        except Exception as e:
            logger.error(f"Error in SSH cleanup task: {e}")
            await asyncio.sleep(60)  # Wait a minute on error
