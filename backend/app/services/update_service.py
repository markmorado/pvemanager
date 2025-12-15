"""
Update Service - Check for updates and perform system updates
"""
import os
import subprocess
import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime
import httpx

logger = logging.getLogger(__name__)

# Путь к корню проекта (монтируется через volume или хост)
# В Docker это /project, на хосте - определяется через переменную окружения
PROJECT_ROOT = os.environ.get('PROJECT_ROOT', '/project')
VERSION_FILE = os.path.join(PROJECT_ROOT, "VERSION")

# Для fallback - путь внутри контейнера где может быть VERSION
CONTAINER_VERSION_FILE = "/app/VERSION"

# Статус обновления (in-memory)
update_status = {
    "is_updating": False,
    "started_at": None,
    "stage": None,
    "progress": 0,
    "error": None,
    "completed": False
}


def get_current_version() -> str:
    """Получить текущую версию из файла VERSION"""
    # Сначала пробуем файл из проекта (если монтирован)
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r") as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"Error reading VERSION file from project: {e}")
    
    # Fallback - читаем из контейнера
    if os.path.exists(CONTAINER_VERSION_FILE):
        try:
            with open(CONTAINER_VERSION_FILE, "r") as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"Error reading VERSION file from container: {e}")
    
    return "unknown"


def is_git_available() -> bool:
    """Проверить, доступен ли git"""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def is_project_mounted() -> bool:
    """Проверить, смонтирован ли проект с .git"""
    git_dir = os.path.join(PROJECT_ROOT, ".git")
    return os.path.exists(git_dir)


def ensure_safe_directory():
    """Добавить PROJECT_ROOT в git safe.directory"""
    try:
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", PROJECT_ROOT],
            capture_output=True,
            timeout=5
        )
    except Exception:
        pass


async def check_for_updates() -> Dict[str, Any]:
    """
    Проверить наличие обновлений
    Сравнивает локальную версию с удалённой
    """
    current_version = get_current_version()
    
    result = {
        "current_version": current_version,
        "latest_version": None,
        "update_available": False,
        "changelog": None,
        "error": None,
        "git_available": is_git_available(),
        "project_mounted": is_project_mounted()
    }
    
    # Проверяем доступность git и проекта
    if not result["git_available"]:
        result["error"] = "Git is not installed in container. Mount project directory with docker-compose volume."
        return result
    
    if not result["project_mounted"]:
        result["error"] = "Project directory not mounted. Add volume mount in docker-compose.yml: ./:/project"
        return result
    
    # Добавляем safe.directory для git
    ensure_safe_directory()
    
    try:
        # Fetch последних изменений без merge
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if fetch_result.returncode != 0:
            result["error"] = f"Failed to fetch updates: {fetch_result.stderr}"
            return result
        
        # Получить версию из удалённого репозитория
        remote_version_result = subprocess.run(
            ["git", "show", "origin/main:VERSION"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if remote_version_result.returncode == 0:
            result["latest_version"] = remote_version_result.stdout.strip()
        else:
            result["error"] = "Failed to get remote version"
            return result
        
        # Сравнить версии
        if result["latest_version"] != current_version:
            result["update_available"] = True
            
            # Получить changelog из удалённого репозитория
            changelog_result = subprocess.run(
                ["git", "show", "origin/main:CHANGELOG.md"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if changelog_result.returncode == 0:
                # Извлекаем только последнюю версию из changelog
                changelog_lines = changelog_result.stdout.split('\n')
                latest_changelog = []
                in_latest_version = False
                version_count = 0
                
                for line in changelog_lines:
                    if line.startswith('## [v'):
                        version_count += 1
                        if version_count == 1:
                            in_latest_version = True
                        elif version_count == 2:
                            break
                    
                    if in_latest_version:
                        latest_changelog.append(line)
                
                result["changelog"] = '\n'.join(latest_changelog)
        
        # Проверить количество коммитов позади
        behind_result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if behind_result.returncode == 0:
            result["commits_behind"] = int(behind_result.stdout.strip())
        
    except subprocess.TimeoutExpired:
        result["error"] = "Timeout while checking for updates"
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error checking for updates: {e}")
    
    return result


def get_update_status() -> Dict[str, Any]:
    """Получить текущий статус обновления"""
    return update_status.copy()


async def perform_update() -> Dict[str, Any]:
    """
    Выполнить обновление системы
    1. Git pull
    2. Docker compose build
    3. Docker compose up
    """
    global update_status
    
    if update_status["is_updating"]:
        return {"success": False, "error": "Update already in progress"}
    
    # Проверяем доступность
    if not is_git_available():
        return {"success": False, "error": "Git not available"}
    
    if not is_project_mounted():
        return {"success": False, "error": "Project not mounted"}
    
    # Добавляем safe.directory для git
    ensure_safe_directory()
    
    # Сбросить статус
    update_status = {
        "is_updating": True,
        "started_at": datetime.now().isoformat(),
        "stage": "initializing",
        "progress": 0,
        "error": None,
        "completed": False
    }
    
    try:
        # Stage 1: Git pull (10%)
        update_status["stage"] = "pulling"
        update_status["progress"] = 10
        
        pull_result = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if pull_result.returncode != 0:
            raise Exception(f"Git pull failed: {pull_result.stderr}")
        
        # Stage 2: Docker build (50%)
        update_status["stage"] = "building"
        update_status["progress"] = 30
        
        # Запускаем rebuild в фоне - это перезапустит сам контейнер
        # Используем nohup чтобы процесс продолжился после рестарта
        build_script = f"""
cd {PROJECT_ROOT}
docker compose up -d --build app 2>&1 | tee /tmp/update.log
"""
        
        # Записываем скрипт обновления
        script_path = "/tmp/update_panel.sh"
        with open(script_path, "w") as f:
            f.write(build_script)
        os.chmod(script_path, 0o755)
        
        # Запускаем скрипт через nohup
        subprocess.Popen(
            ["nohup", "sh", script_path],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        
        update_status["stage"] = "restarting"
        update_status["progress"] = 80
        
        return {
            "success": True,
            "message": "Update started. The panel will restart shortly."
        }
        
    except subprocess.TimeoutExpired:
        update_status["error"] = "Update timeout"
        update_status["is_updating"] = False
        return {"success": False, "error": "Update timeout"}
    except Exception as e:
        update_status["error"] = str(e)
        update_status["is_updating"] = False
        logger.error(f"Update failed: {e}")
        return {"success": False, "error": str(e)}


def reset_update_status():
    """Сбросить статус обновления"""
    global update_status
    update_status = {
        "is_updating": False,
        "started_at": None,
        "stage": None,
        "progress": 0,
        "error": None,
        "completed": False
    }

