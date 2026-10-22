import base64
import hashlib
import logging
import asyncio
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Union
from concurrent.futures import ThreadPoolExecutor
import paramiko
from paramiko.ssh_exception import SSHException, NoValidConnectionsError, BadHostKeyException
import socket
from functools import lru_cache
import time

logger = logging.getLogger(__name__)

# Thread pool for blocking SSH operations
ssh_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ssh_")


# ── Host key pinning (trust on first use) ────────────────────────────────────
#
# The panel connects to Proxmox nodes as root. Accepting any host key
# (WarningPolicy) let anyone who can intercept that traffic pose as the node
# and collect the root password / run commands. Strict checking
# (SSH_REJECT_UNKNOWN_HOSTS) needs a pre-filled known_hosts that most installs
# never set up. TOFU sits in between: the key seen on the first connection is
# pinned in a panel-owned known_hosts file on the data volume, and a different
# key later is refused until an administrator resets the pin (e.g. after a node
# reinstall: "Reset SSH host key" on the server card).

_known_hosts_lock = threading.Lock()


# sshd runs a command as `sh -c <command>`, and Linux caps one argument at
# MAX_ARG_STRLEN (128 KiB): an inline base64 payload above ~96 KiB of data
# failed with "Argument list too long". The chunk is a multiple of 4, so each
# piece decodes on its own.
_B64_CHUNK = 64 * 1024


def write_file_commands(path: str, data: bytes) -> List[str]:
    """Shell commands that write ``data`` to ``path`` (mode 0600) via base64.

    Run them in order. ``path`` is inserted verbatim — pass generated names only.
    """
    b64 = base64.b64encode(data).decode("ascii")
    if not b64:
        return [f"umask 077 && : > {path}"]
    cmds = []
    for i in range(0, len(b64), _B64_CHUNK):
        redirect = ">" if i == 0 else ">>"
        # base64 alphabet [A-Za-z0-9+/=] is safe inside single quotes.
        cmds.append(f"umask 077 && printf '%s' '{b64[i:i + _B64_CHUNK]}' | base64 -d {redirect} {path}")
    return cmds


def known_hosts_path() -> Path:
    """Panel-owned known_hosts file (persistent volume by default)."""
    from .config import settings
    if settings.SSH_KNOWN_HOSTS_FILE:
        return Path(settings.SSH_KNOWN_HOSTS_FILE)
    return Path(settings.APPSTORE_DATA_DIR).parent / "ssh_known_hosts"


def host_key_entry(hostname: str, port: int = 22) -> str:
    """known_hosts host field: bare host for port 22, ``[host]:port`` otherwise."""
    return hostname if int(port) == 22 else f"[{hostname}]:{port}"


def key_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _load_known_hosts() -> paramiko.HostKeys:
    keys = paramiko.HostKeys()
    path = known_hosts_path()
    if path.is_file():
        try:
            keys.load(str(path))
        except (OSError, SSHException) as e:
            logger.warning(f"Cannot read pinned SSH host keys from {path}: {e}")
    return keys


def _save_known_hosts(keys: paramiko.HostKeys) -> None:
    """Atomically rewrite the pinned-keys file. Caller holds _known_hosts_lock."""
    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    keys.save(str(tmp))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def pin_host_key(entry: str, key: paramiko.PKey) -> None:
    """Remember ``key`` for ``entry`` (merging with what other threads pinned)."""
    with _known_hosts_lock:
        keys = _load_known_hosts()
        keys.add(entry, key.get_name(), key)
        _save_known_hosts(keys)


def pinned_host_keys(hostname: str, port: int = 22) -> List[Dict[str, str]]:
    """Pinned keys of a host as [{type, fingerprint}] (for the UI)."""
    found = _load_known_hosts().lookup(host_key_entry(hostname, port)) or {}
    return [{"type": ktype, "fingerprint": key_fingerprint(k)} for ktype, k in found.items()]


def forget_host_key(hostname: str, port: int = 22) -> int:
    """Drop the pinned key(s) of a host; returns how many entries were removed."""
    entry = host_key_entry(hostname, port)
    removed = 0
    with _known_hosts_lock:
        keys = _load_known_hosts()
        while True:
            try:
                del keys[entry]
            except KeyError:
                break
            removed += 1
        if removed:
            _save_known_hosts(keys)
    # Cached connections were authenticated against the old key.
    SSHClient.drop_cached_connections(hostname, port)
    return removed


class _TrustOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """Accept and pin an unknown host key; known keys are verified by paramiko."""

    def missing_host_key(self, client, hostname, key):
        try:
            pin_host_key(hostname, key)
        except OSError as e:
            # Still connect (previous behaviour), but say that nothing is pinned.
            logger.error(f"SSH: could not pin host key of {hostname} in {known_hosts_path()}: {e}")
        client.get_host_keys().add(hostname, key.get_name(), key)
        logger.warning(
            f"SSH: first connection to {hostname} — trusting and pinning "
            f"{key.get_name()} key {key_fingerprint(key)}"
        )


class SSHClient:
    """Улучшенный SSH клиент для подключения к серверам с кешированием"""

    # Connection cache (shared across threads — guard every access with _cache_lock)
    _connections = {}
    _connection_times = {}
    _cache_lock = threading.Lock()
    
    def __init__(self, hostname: str, port: int = 22, username: str = "root",
                 key_path: Optional[str] = None, timeout: int = 10, max_retries: int = 3,
                 password: Optional[str] = None):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.key_path = key_path
        self.password = password
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = None
        self.connection_key = f"{hostname}:{port}:{username}"
        self._connected = False
        # Human-readable reason of the last failed connect() (for API errors).
        self.last_error: Optional[str] = None

    @classmethod
    def drop_cached_connections(cls, hostname: str, port: int = 22) -> None:
        """Close cached connections to a host (any user)."""
        prefix = f"{hostname}:{port}:"
        with cls._cache_lock:
            for key in [k for k in cls._connections if k.startswith(prefix)]:
                client = cls._connections.pop(key, None)
                cls._connection_times.pop(key, None)
                try:
                    if client is not None:
                        client.close()
                except Exception:
                    pass
    
    def _get_cached_connection(self) -> Optional[paramiko.SSHClient]:
        """Get cached connection if available and valid"""
        with self._cache_lock:
            client = self._connections.get(self.connection_key)
            if client is None:
                return None
            connection_time = self._connection_times.get(self.connection_key, 0)

            # Check if connection is still valid (max 5 minutes)
            if time.time() - connection_time < 300:
                try:
                    # Test connection with a simple command
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        return client
                except Exception:
                    pass

            # Remove invalid connection
            self._cleanup_connection_locked()
        return None

    def _cache_connection(self, client: paramiko.SSHClient):
        """Cache successful connection"""
        with self._cache_lock:
            self._connections[self.connection_key] = client
            self._connection_times[self.connection_key] = time.time()

    def _cleanup_connection(self):
        """Clean up cached connection (acquires the cache lock)."""
        with self._cache_lock:
            self._cleanup_connection_locked()

    def _cleanup_connection_locked(self):
        """Clean up cached connection. Caller must hold _cache_lock."""
        client = self._connections.pop(self.connection_key, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            self._connection_times.pop(self.connection_key, None)
    
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
                client.load_system_host_keys()
                # Keys pinned on earlier connections (TOFU); a mismatch makes
                # paramiko raise BadHostKeyException below.
                for entry, by_type in _load_known_hosts().items():
                    for ktype, key in by_type.items():
                        client.get_host_keys().add(entry, ktype, key)
                try:
                    from .config import settings
                    strict = settings.SSH_REJECT_UNKNOWN_HOSTS
                except Exception:
                    strict = False
                if strict:
                    # Only keys already in known_hosts / pinned are accepted.
                    client.set_missing_host_key_policy(paramiko.RejectPolicy())
                else:
                    client.set_missing_host_key_policy(_TrustOnFirstUsePolicy())
                
                connect_kwargs = {
                    'hostname': self.hostname,
                    'port': self.port,
                    'username': self.username,
                    'timeout': self.timeout,
                    'banner_timeout': self.timeout,
                    'auth_timeout': self.timeout,
                }
                if self.password:
                    connect_kwargs['password'] = self.password

                # Try key authentication first
                if self.key_path:
                    try:
                        connect_kwargs['key_filename'] = self.key_path
                        client.connect(**connect_kwargs, look_for_keys=False, allow_agent=False)
                    except paramiko.AuthenticationException:
                        logger.debug(f"Key auth failed for {self.hostname}, trying other methods")
                        # Remove key and try other methods (password / agent / default keys)
                        del connect_kwargs['key_filename']
                        client.connect(**connect_kwargs, look_for_keys=True, allow_agent=True)
                else:
                    client.connect(**connect_kwargs, look_for_keys=True, allow_agent=True)
                
                self.client = client
                self._connected = True
                self.last_error = None
                self._cache_connection(client)
                logger.info(f"Successfully connected to {self.hostname} (attempt {attempt + 1})")
                return True

            except BadHostKeyException as e:
                # Must precede SSHException (its parent): retrying cannot help.
                self.last_error = (
                    f"SSH host key of {self.hostname} does not match the pinned key "
                    f"(got {key_fingerprint(e.key)}, expected {key_fingerprint(e.expected_key)}). "
                    f"Possible MITM — if the node was reinstalled, reset the SSH host key "
                    f"on the server card."
                )
                logger.error(self.last_error)
                return False
            except paramiko.AuthenticationException as e:
                self.last_error = f"SSH authentication failed for {self.hostname}: {e}"
                logger.error(f"Authentication failed for {self.hostname}: {e}")
                return False
            except (SSHException, NoValidConnectionsError, socket.timeout, 
                   socket.error, ConnectionRefusedError) as e:
                logger.warning(f"Connection attempt {attempt + 1} failed for {self.hostname}: {e}")
                if attempt == self.max_retries - 1:
                    self.last_error = f"SSH connection to {self.hostname} failed: {e}"
                    logger.error(f"All connection attempts failed for {self.hostname}")
                    return False
                # Wait before retry
                time.sleep(2 ** attempt)  # Exponential backoff
            except Exception as e:
                self.last_error = f"SSH connection to {self.hostname} failed: {e}"
                logger.error(f"Unexpected error connecting to {self.hostname}: {e}")
                return False
        
        return False
    
    def execute(self, command: str, return_exit_code: bool = False,
                timeout: Optional[int] = None) -> Union[Optional[str], tuple]:
        """Выполнить команду на сервере.

        timeout — предельное время ожидания завершения команды (сек). Если не
        задан, используется self.timeout. Команды, легально идущие долго
        (docker compose pull/up), должны передавать увеличенный timeout.
        """
        if not self._connected or not self.client:
            if not self.connect():
                return (None, -1) if return_exit_code else None

        cmd_timeout = timeout if timeout is not None else self.timeout
        try:
            stdin, stdout, stderr = self.client.exec_command(
                command, timeout=cmd_timeout, get_pty=False
            )

            # ВАЖНО: recv_exit_status() ИГНОРИРУЕТ таймаут exec_command и блокирует
            # поток навсегда, если удалённая команда подвисла (типичная ловушка
            # paramiko — из-за неё установка приложения замирала на «pct push»).
            # Поэтому ждём готовности статуса с жёстким дедлайном.
            channel = stdout.channel
            deadline = time.time() + cmd_timeout
            while not channel.exit_status_ready():
                if time.time() > deadline:
                    logger.error(
                        f"Command exit-status timeout on {self.hostname} "
                        f"(>{cmd_timeout}s): {command}"
                    )
                    try:
                        channel.close()
                    except Exception:
                        pass
                    self._connected = False
                    self._cleanup_connection()
                    return (None, -1) if return_exit_code else None
                time.sleep(0.2)

            exit_code = channel.recv_exit_status()

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
        except Exception:
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
        with cls._cache_lock:
            for key in list(cls._connections.keys()):
                try:
                    cls._connections[key].close()
                except Exception:
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

            with SSHClient._cache_lock:
                expired_keys = [
                    key for key, connection_time in SSHClient._connection_times.items()
                    if current_time - connection_time > 600  # 10 minutes
                ]
                for key in expired_keys:
                    client = SSHClient._connections.pop(key, None)
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass
                        SSHClient._connection_times.pop(key, None)
                        logger.debug(f"Cleaned up expired SSH connection: {key}")
        except Exception as e:
            logger.error(f"Error in SSH cleanup task: {e}")
            await asyncio.sleep(60)  # Wait a minute on error
