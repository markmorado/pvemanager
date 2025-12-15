# Решение проблемы с проверкой обновлений для приватных репозиториев

## Проблема

При работе с приватным GitHub репозиторием система выдает ошибку:
```
Failed to fetch VERSION file: HTTP 404
```

## Решение

Есть два способа решения этой проблемы:

### Способ 1: Отключить проверку обновлений (Рекомендуется)

Добавьте в файл `.env` в корне проекта:

```bash
DISABLE_UPDATE_CHECK=true
```

Затем перезапустите контейнер:

```bash
docker compose restart app
```

### Способ 2: Использовать GitHub Token

1. Создайте Personal Access Token на GitHub:
   - Перейдите на https://github.com/settings/tokens
   - Нажмите "Generate new token (classic)"
   - Выберите scope: `repo` (Full control of private repositories)
   - Скопируйте созданный токен

2. Добавьте токен в файл `.env`:

```bash
DISABLE_UPDATE_CHECK=false
GITHUB_TOKEN=your_github_personal_access_token_here
```

3. Перезапустите контейнер:

```bash
docker compose restart app
```

## Проверка

После применения изменений:

1. Откройте панель: http://localhost:8000
2. Перейдите в **Settings** → **System Updates**
3. Проверьте, что ошибка исчезла

## Дополнительная информация

- Настройки находятся в файле: `backend/app/services/update_service.py`
- Переменные окружения: `DISABLE_UPDATE_CHECK`, `GITHUB_TOKEN`
- Документация: [README.md](README.md)
