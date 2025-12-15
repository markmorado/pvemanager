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

# GitHub token для доступа к приватным репозиториям (опционально)
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', None)

# Отключить проверку обновлений (для приватных репозиториев без токена)
DISABLE_UPDATE_CHECK = os.environ.get('DISABLE_UPDATE_CHECK', 'false').lower() == 'true'

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


def configure_git_for_public_access():
    """Настроить git для работы без аутентификации с публичным репозиторием"""
    try:
        # Отключить запросы пароля
        subprocess.run(
            ["git", "config", "--global", "credential.helper", ""],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=5
        )
        
        # Получить текущий remote URL
        remote_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if remote_result.returncode == 0:
            current_url = remote_result.stdout.strip()
            
            # Если URL использует https с credentials, очистить их
            if "https://" in current_url and "@" in current_url:
                # Убрать credentials из URL
                clean_url = current_url.split("@")[-1]
                clean_url = "https://" + clean_url
                
                subprocess.run(
                    ["git", "remote", "set-url", "origin", clean_url],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=5
                )
        
        return True
    except Exception as e:
        logger.warning(f"Could not configure git: {e}")
        return False


async def check_for_updates() -> Dict[str, Any]:
    """
    Проверить наличие обновлений через GitHub API
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
        "project_mounted": is_project_mounted(),
        "disabled": DISABLE_UPDATE_CHECK
    }
    
    # Если проверка обновлений отключена
    if DISABLE_UPDATE_CHECK:
        result["error"] = "Update check is disabled"
        result["latest_version"] = current_version
        return result
    
    # Получаем URL репозитория
    repo_url = None
    if result["project_mounted"]:
        try:
            ensure_safe_directory()
            remote_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=5
            )
            if remote_result.returncode == 0:
                repo_url = remote_result.stdout.strip()
        except Exception as e:
            logger.warning(f"Could not get git remote URL: {e}")
    
    # Парсим owner/repo из URL
    owner, repo = None, None
    if repo_url:
        # https://github.com/owner/repo.git -> owner/repo
        if "github.com" in repo_url:
            parts = repo_url.replace(".git", "").split("github.com/")
            if len(parts) == 2:
                owner_repo = parts[1].split("/")
                if len(owner_repo) >= 2:
                    owner, repo = owner_repo[0], owner_repo[1]
    
    if not owner or not repo:
        # Fallback на хардкод для этого проекта
        owner, repo = "markmorado", "pvemanager"
    
    try:
        # Подготавливаем заголовки для запроса
        headers = {}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        
        # Используем GitHub API для получения файлов
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Пробуем оба варианта: main и master
            version_url = None
            version_response = None
            
            for branch in ["main", "master"]:
                test_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/VERSION"
                test_response = await client.get(test_url, headers=headers)
                
                if test_response.status_code == 200:
                    version_url = test_url
                    version_response = test_response
                    break
            
            if version_response and version_response.status_code == 200:
                result["latest_version"] = version_response.text.strip()
            elif version_response and version_response.status_code == 404:
                result["error"] = "Repository is private or not accessible. Set DISABLE_UPDATE_CHECK=true to hide this error."
                result["latest_version"] = current_version
                return result
            else:
                result["error"] = f"Failed to fetch VERSION file: HTTP {version_response.status_code if version_response else 'unknown'}"
                result["latest_version"] = current_version
                return result
            
            # Сравнить версии
            if result["latest_version"] != current_version:
                result["update_available"] = True
                
                # Получить CHANGELOG.md (используем ту же ветку, что и для VERSION)
                branch = version_url.split("/")[-2] if version_url else "main"
                changelog_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/CHANGELOG.md"
                changelog_response = await client.get(changelog_url, headers=headers)
                
                if changelog_response.status_code == 200:
                    # Извлекаем только последнюю версию из changelog
                    changelog_lines = changelog_response.text.split('\n')
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
            
            # Получить информацию о коммитах через API
            if result["project_mounted"]:
                try:
                    # Получить локальный commit hash
                    local_commit_result = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    
                    if local_commit_result.returncode == 0:
                        local_commit = local_commit_result.stdout.strip()
                        
                        # Получить удаленный commit через API
                        commits_url = f"https://api.github.com/repos/{owner}/{repo}/commits/main"
                        commits_response = await client.get(commits_url)
                        
                        if commits_response.status_code == 200:
                            remote_commit = commits_response.json()["sha"]
                            
                            # Сравнить хеши
                            if local_commit != remote_commit:
                                result["commits_behind"] = 1  # Упрощенно
                except Exception as e:
                    logger.warning(f"Could not check commits: {e}")
        
    except httpx.TimeoutException:
        result["error"] = "Timeout while checking for updates"
    except httpx.HTTPError as e:
        result["error"] = f"HTTP error: {str(e)}"
        logger.error(f"HTTP error checking for updates: {e}")
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
    
    # Настраиваем git для публичного доступа
    configure_git_for_public_access()
    
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
        
        # Пробуем pull с публичным доступом
        pull_result = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # Отключить запрос пароля
        )
        
        if pull_result.returncode != 0:
            # Если не получилось через pull, пробуем через reset
            # Fetch с установкой remote для публичного доступа
            fetch_result = subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            )
            
            if fetch_result.returncode != 0:
                raise Exception(f"Git fetch failed: {fetch_result.stderr}")
            
            # Reset к удаленной версии
            reset_result = subprocess.run(
                ["git", "reset", "--hard", "origin/main"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if reset_result.returncode != 0:
                raise Exception(f"Git reset failed: {reset_result.stderr}")
        
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

