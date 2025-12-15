# 🖥️ PVEmanager v2.1.4

Modern web panel for managing Proxmox servers, virtual machines and LXC containers.

[![Version](https://img.shields.io/badge/version-2.1.4-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](compose.yml)

![Dashboard Screenshot](docs/screenshot.png)

## ✨ Key Features

- 🖥️ **Proxmox Integration** — Manage multiple servers and clusters
- 🎛️ **VM/LXC Management** — Start, Stop, Restart, Delete, Resize
- ⚡ **Bulk Operations** — Mass start/stop/restart/delete VMs and containers
- 🖵 **VNC Console** — Built-in console via noVNC
- ⌨️ **Remote Commands** — Bash via QEMU Guest Agent
- 📸 **Snapshots** — Create, delete, rollback VM/LXC snapshots with queue system
- 📋 **OS Templates** — Quick VM deployment from templates
- 🔄 **Cross-Node Templates** — Deploy templates to any cluster node with auto-replication
- 📦 **Smart LXC Creation** — Create containers from templates on any node with auto-migration
- 🌐 **IPAM** — IP Address Management
- 🔔 **Notifications** — Email, Telegram, In-App
- 📊 **Monitoring** — CPU, RAM, Disk, Network in real-time
- 🔒 **Security** — RBAC v2, IP blocking, session management, login protection
- 🌍 **Multilingual** — Russian and English

## 🚀 Quick Start

```bash
# Clone repository
git clone https://github.com/your-repo/pvemanager.git
cd pvemanager

# Copy configuration
cp .env.example .env
cp backend/.env.example backend/.env

# Start
docker compose up -d

# Open http://localhost:8000
# Login: admin / Password: admin123
```

> ⚠️ Make sure to change password after first login!

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [📖 WIKI.md](WIKI.md) | **Complete User Guide** |
| [📝 CHANGELOG.md](CHANGELOG.md) | Change History |
| [🚀 DEPLOYMENT.md](DEPLOYMENT.md) | Production Deployment |
| [🔌 API_EXAMPLES.md](API_EXAMPLES.md) | API Usage Examples |

## 🔔 Notifications

Notification system supports:

- **In-App** — Bell icon with badge in UI
- **Email** — SMTP (Yandex, Gmail, Mail.ru)
- **Telegram** — Via Bot API

Settings: **Settings** → **Notifications**

## 🛠️ Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | FastAPI + Python 3.12 |
| Frontend | Jinja2 + Vanilla JS |
| Database | PostgreSQL 16 |
| Container | Docker + Alpine |
| Proxmox API | proxmoxer |
| VNC | noVNC |

## 📋 Requirements

- Docker & Docker Compose
- 2GB RAM minimum
- Proxmox VE 7.x / 8.x / 9.x

## 🔧 Configuration

### Main Variables (`.env`)

```bash
POSTGRES_PASSWORD=your_secure_password
TZ=Asia/Tashkent
```

### Notifications (SMTP and Telegram)

Email and Telegram notification settings are now managed via web interface:
1. Open **Settings → Notifications**
2. Fill in SMTP server details for email notifications
3. Enter Telegram bot token for Telegram notifications

## 🤝 Contributing

1. Fork the repository
2. Create feature branch
3. Commit changes
4. Push and create Pull Request

## 📜 License

MIT License — see [LICENSE](LICENSE)

## 📞 Support

- 📖 [Documentation (WIKI.md)](WIKI.md)
- 🐛 [Issues](https://github.com/your-repo/pvemanager/issues)
- 📝 [Changelog](CHANGELOG.md)

---

Made with ❤️ for Proxmox users
