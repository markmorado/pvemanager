# Changelog

All notable changes to PVEmanager will be documented in this file.

---

## [Unreleased]

### 🔒 Security

- **Tenants can no longer act on other users' guests** — 28 VM/LXC endpoints (command execution via guest agent and `pct exec`, config read/write, disks, cloud-init, unlock, HA, metrics, RRD) and the guest firewall endpoints checked only the permission, not ownership. Any user with `vm:console` (part of the stock `user` role) could run commands in any VM. Backup create/restore/list and Proxmox task logs are scoped the same way
- **Server-scoped endpoints honour server assignment** — node syslog/services/APT, cluster firewall, disks/ZFS, networks, pools, SDN, backups and templates of servers that are not assigned to a tenant (directly or via a workspace) now return 404
- **Privilege escalation in user management closed** — `user:create` could mint administrators (`is_admin` was taken from the request), `user:update` could reset the password of an account that is admin through its role or assign the admin role to oneself, `role:manage` could grant permissions the actor did not hold
- **Script catalog hardening** — `script:execute` no longer runs scripts on any server/guest: node scripts need `node:manage`, guest scripts need access to the guest. Git sources: option-injection-safe `git clone`, URL/branch/glob validation, symlinks and paths outside the clone are ignored (a repository could import `/proc/self/environ`). Parameter names and the interpreter are validated; temp files get random names
- **WebSocket**: `/ws/tasks` validates the session like the REST API (a logged-out token kept streaming); channel subscriptions and RRD requests are authorized (tenants could stream other users' guest metrics); the shared instance list is filtered per subscriber
- **Password reset links ignore the request's `Origin`/`Host`** (reset poisoning) — new `PANEL_URL` setting, filled by `deploy.sh` when a domain is configured
- **Plaintext passwords no longer reach the audit log** — form-encoded bodies (e.g. `/api/auth/change-password`) were stored raw; 2FA codes and private keys are masked too
- **Stored XSS via template group icons** fixed (rendered with `dangerouslySetInnerHTML`, editable with `template:manage`)
- **SSRF**: `/api/images/resolve-filename` requires `template:manage` and refuses loopback/link-local targets on every redirect hop
- **Client IP can no longer be spoofed** with `X-Forwarded-For` to dodge brute-force blocking; proxy headers are trusted only from private peers
- **2FA**: TOTP codes are single-use (RFC 6238), the TOTP secret is stored encrypted (migration 44), login timing no longer reveals whether a username exists
- **Sessions**: changing a password ends the user's other sessions (both endpoints; the profile endpoint also enforces the password policy now), `stop-impersonate` requires a live session, a placeholder `SECRET_KEY` is replaced by a generated key stored on the data volume
- **SSH host keys are pinned on first use (TOFU)** — the panel used to accept any key presented by a node (`WarningPolicy`), so anyone able to intercept the connection could pose as the node and receive the root password. The first key seen is now stored in `/app/data/ssh_known_hosts` (`SSH_KNOWN_HOSTS_FILE`), a different key later is refused with a clear error, and the new **Reset SSH host key** button on the server card (`DELETE /proxmox/api/servers/{id}/ssh-host-key`, `server:update`) clears the pin after a node reinstall. `SSH_REJECT_UNKNOWN_HOSTS=true` keeps strict mode
- **Quotas** apply to clones and App Store installs; App Store install validates storage/bridge/node and server access
- VM notifications and resource alerts go to the guest's owner and administrators only (they were sent to every user); WebSocket broadcasts no longer carry other tenants' guest names or the new owner's e-mail
- **Consoles no longer hand out the panel's Proxmox root ticket** — opening a VNC console or terminal returned the `PVEAuthCookie` of the panel's Proxmox account (usually `root@pam`) and the node IP to the browser, so any tenant with `vm:console` on their own guest got full API access to the cluster for two hours. The ticket now stays on the backend; the browser gets a single-use handle bound to the user and guest
- **Bulk operations check every guest** — `/api/bulk-operation` checked only `vm:manage`, and a role holding `vm:*` passed it without being treated as global elsewhere, so it could stop or delete other tenants' guests. Each item now needs the action's own permission (as the single-guest endpoints: `vm:start`, `vm:stop`, `vm:delete`, `vm:migrate`/`lxc:migrate`) and access to the guest — owners can use bulk actions on their own guests
- **Backups are scoped to the guest's owner** — a tenant could restore someone else's archive into their own guest (taking over its disks), restore to a new untracked VMID, force-overwrite foreign guests with bulk restore, delete foreign backup files and read any task log via `/api/backups/task`. Scheduled backup jobs validate VMIDs against ownership (with `keep_last` pruning, a foreign VMID wiped that guest's backups), validate cron expressions, and `toggle` / `run-now` check the job owner
- **Command injection in the App Store golden template** — `POST /api/appstore/golden-template` put `template_storage`, `rootfs_storage` and `bridge` from the request into root shell commands on the node (`app:manage` → root on the node); they are validated now, and the server must be visible to the user
- **Deploy from template honours server assignment** — the request names a template, so the router-level check did not apply and a tenant could deploy onto any server; the template list shows tenants only templates of their servers
- App Store logos (often SVG from third-party catalogs) are served with a sandboxing CSP — opened directly, a script inside ran on the panel's origin next to the session token; the global security-headers middleware no longer overrides an endpoint's stricter CSP
- "Open app" after an App Store install opens the app without `window.opener` (the third-party app could navigate the panel tab)
- **LXC / ISO creation could read other guests' data** — `ostemplate` accepted a vzdump archive (`pct create` from a backup is a restore, so a tenant got another guest's filesystem) and `iso` / `extra_iso` accepted any volume (another guest's disk attached as a CD-ROM); only `<storage>:vztmpl/…` and `<storage>:iso/…` are accepted now. Both endpoints honour server assignment (the server comes from the body), and privileged containers (root in them is root on the node) require `vm:manage`
- **Parallel creations no longer bypass `max_instances`** — the instance row appears only after the clone finishes, so N simultaneous deploys all passed the check; queued/running deploys, clones, ISO VMs and App Store installs now count
- **Node names and other identifiers are validated before reaching the Proxmox API** — `node`, path segments, storage ids, backup volume ids and UPIDs from requests were inserted into Proxmox API URL paths unescaped, so a crafted value could make a request authorized for one guest address a different resource. Every `/proxmox` route (HTTP and WebSocket) now rejects such values, and request bodies validate node names
- **Open consoles close when access ends** — WebSocket access was checked only at the handshake, so a VNC/terminal/serial console or a root node shell stayed usable after logout, a password change, a terminated session, an account lock or a revoked permission / guest ownership. Access is now re-checked every 30 s on all console bridges and `/ws/tasks`
- The panel page itself is served with `frame-ancestors 'self'` / `X-Frame-Options`, `nosniff` and a referrer policy: the security headers were set only on API responses (the outer nginx's ones were not inherited by the SSL server block), so any site could frame the panel
- Without the SSL profile, `compose.yml` published the API on every host interface (port 8000), bypassing nginx; it is bound to `127.0.0.1` now (the UI goes through the frontend's nginx as before)
- The SMTP password and Telegram bot token are stored encrypted (migration 46 encrypts existing values), like server passwords and TOTP seeds
- Wrong two-factor codes count toward the account lockout like wrong passwords (with the password known, only the per-IP block limited guessing the 6-digit code)
- The update repository URL must be `https://` — updates pull, build and run that code as root on the host
- Session tokens and console handles in WebSocket query strings are no longer written to the logs (uvicorn and both nginx configs)
- **The session JWT is no longer put in WebSocket URLs** (where it ended up in proxy logs and browser history) — the panel fetches a single-use ticket (`POST /api/auth/ws-ticket`, valid 30 s, bound to the user and session) before every WebSocket (re)connect and passes `?ticket=`. **`?token=<jwt>` is no longer accepted** — WebSocket clients outside the panel must fetch a ticket first; browser tabs opened before the update need a reload
- Guest password change rejects user names and passwords that would inject extra `chpasswd` lines
- Console WebSocket bridges verify the Proxmox TLS certificate when the server is set to verify it (they always skipped verification, while the root password is sent there)

### ⚡ Performance

- Session validation writes to the DB at most once a minute per session instead of on every request; security settings cache is per key
- Task lists and WebSocket task updates no longer carry full Proxmox task logs; script execution lists no longer carry outputs (up to 200 KB each)
- VM cache sync: one query for all cached rows instead of one per guest every 10 s, `last_sync_at` written at most once a minute, one Proxmox client per server
- Resource monitoring reuses the VM list instead of one extra API call per running VM per minute
- Request audit logging and the WebSocket initial snapshot run off the event loop; uploads are written to disk in a worker thread
- Scheduler jobs coalesce instead of piling up; the first VM cache sync no longer blocks application startup
- Metrics broadcaster tasks are kept referenced (they could be garbage-collected and silently stop)
- **Translations are loaded per language** — `ru.json` and `en.json` (~210 KB) were bundled into the entry for everyone; now only the active language is fetched, and switching languages loads the other on demand. Russian sessions do not download English as a fallback: `ru.json` was completed (18 strings of the node administration page were shown in English) and CI checks that it covers every key of `en.json`
- Login page backgrounds: 2.4 MB PNG → 36 KB WebP; `index.html` is served with `Cache-Control: no-cache`
- Dashboard task counters refresh every 30 s instead of pulling 500 tasks every 5 s; finished task logs stop polling
- Migration 45: indexes for per-user task polling

### 🛠 Process

- Production deploy waits for the backend tests and the frontend build (it ran on every push to `main` regardless); deploys no longer overlap
- New `Frontend Checks` workflow (build, typecheck and lint reported); `upload-artifact` bumped to v4 (v3 is shut down)
- `.dockerignore` excludes `.venv`, `.env` and `graphify-out` from build contexts
- The backend image no longer installs `docker.io` (unused: self-update runs on the host via a trigger file, no Docker socket is mounted)

### 🐛 Fixes

- WebSocket client keeps reconnecting with backoff (it gave up after ~30 s, e.g. during a self-update) and reconnects under the new identity after login, logout and impersonation
- A failed token refresh during a backend restart (5xx) no longer logs the user out
- `HTTPException`s raised inside `try` blocks are no longer turned into 500 errors (28 endpoints)
- A token refresh during impersonation no longer drops the impersonation link — the admin was stranded in the user's account (no way back) and further actions were not marked as impersonated
- VNC, terminal, serial and node-shell consoles work on servers registered with a non-default API port (`:8006` was hard-coded)
- App Store data files and catalog scripts larger than ~96 KB failed to install (`Argument list too long`: the whole file went into one shell argument); they are now written in chunks, temp files get random names
- Deploys, App Store operations and script runs interrupted by a backend restart are marked failed at startup — they stayed `running` forever, inflating the active-task counter; the instances page stops polling a stored deploy task the backend returns 404/403 for (it polled every 2 s indefinitely)
- Template list: two queries instead of two per template
- Apps stuck in `installing` / `updating` / `deleting` after a restart are moved to their failure state (retry is available; `installing` also held a quota slot)
- Metrics history reads column tuples instead of ORM objects (month windows are tens of thousands of rows) and loads only the selected NIC
- The console, dashboard and instance detail pages are loaded on demand: xterm (with its CSS) and recharts (~0.4 MB) were in the entry bundle for every user, including tenants without a dashboard
- Image upload copies the spooled file in one worker call instead of two thread hops per 4 MB chunk
- Scheduled backups no longer block the event loop while talking to Proxmox; a job with malformed VMIDs is marked failed instead of hanging in `running`
- `backend/.env` keys unknown to the settings model (e.g. `ALLOWED_HOSTS`) no longer prevent startup

- **`guest-exec` is unblocked in the Rocky Linux, AlmaLinux, CentOS Stream and Fedora images** — the RHEL-family `qemu-guest-agent` package runs the agent with an allow-list that omits `guest-exec`, `guest-exec-status` and `guest-file-*` (`FILTER_RPC_ARGS` in `/etc/sysconfig/qemu-ga`; older packages expressed the same thing through `BLOCK_RPCS`/`BLACKLIST_RPC`). Because of it, script execution, extra IP addresses and filesystem growth after a disk resize did not work in such VMs
- **Five mirror images were rebuilt** — release `2026-08-22`, and the bundled catalog now points at it; Ubuntu and Debian are untouched and stay in the previous releases, so older panel versions keep working
- **`patch-cloud-images.sh` clears the restriction while building** (turn it off with `UNBLOCK_RPC=0`) and can build part of the catalog — `ONLY="rocky alma centos fedora"`. And when the panel hits the block, it spells out what to fix inside the guest

---

## [v1.18.0] - 2026-08-22

### 🌐 Several IP addresses per instance

- **An instance is no longer limited to one address** — a guest can hold a primary address plus any number of extra ones. Previously every lookup assumed "one guest → one allocation": `find_allocation_by_resource()` returned the first row it saw, `release_ip_by_vmid()` freed a single address (leaving the rest occupied forever after a guest was deleted), and the link tool skipped guests that already had an allocation
- **New "IP addresses" card on the instance Network tab** — lists every address of the guest with its state, and lets you add one (next free in a network/pool or entered by hand), retry applying it, mark it primary or release it. The IP column of the instance list shows the primary address with a **+N** badge for the rest, and the allocations table marks the primary one
- **Extra addresses are applied inside the guest as aliases** — through the existing script engine, so LXC goes over `pct exec` and VMs over the QEMU guest agent. The address is brought up with `ip addr add` and never restarts guest networking, so a running guest keeps its connectivity
- **Addresses survive a guest reboot** — Proxmox regenerates the guest network configuration on every start (pve-container rewrites `/etc/network/interfaces`, `ifcfg` or the NetworkManager keyfile from `netN`), which wiped any alias added to the native stack. A small oneshot systemd unit re-adds the recorded addresses after `network-online.target`; `/etc/systemd` is not touched by Proxmox. The native stacks (ifupdown, netplan, NetworkManager, sysconfig/network-scripts for RHEL-based guests) are still written, so the reported state can be composite, e.g. `nm+systemd`
- **An address is reserved even when it cannot be applied** — a stopped guest, a missing guest agent or no SSH access to the node leaves the allocation in `pending` with the reason shown, and the **Apply** button retries it later. An unrecognised network stack degrades to `runtime_only`, which warns that the address will not survive a reboot
- **New endpoints** — `GET/POST /ipam/api/guests/{server_id}/{vmid}/addresses`, `POST …/{allocation_id}/apply`, `POST …/{allocation_id}/primary`, `DELETE …/{allocation_id}`; the allocations list accepts `proxmox_server_id` / `proxmox_vmid` filters, and the instance list exposes `ips[]` next to the primary `ip`
- **Migration 43** adds `is_primary`, `assignment_kind`, `target_interface` and the `apply_*` columns to `ipam_allocations` and marks existing rows as primary. The first address of a guest is now flagged automatically, no matter which of the six allocation paths issued it
- **Releasing an address removes it from the guest first**, then frees it in IPAM; the primary address stays owned by the NIC config and is not releasable from this card

### 🔍 IPAM address discovery fixes

- **LXC containers finally show their IP** — `get_container_interfaces()` looked for `ipconfigN` keys, which exist only on QEMU cloud-init guests. LXC keeps the address inside the `netN` string, so the method never returned anything and the IP column stayed empty for every container. The `netN` parser is used now, with a fallback to `/nodes/{node}/lxc/{vmid}/interfaces` for DHCP guests
- **"Scan & link" registers every address of a guest**, not just the first one, and no longer skips guests that already have an allocation — a changed or added address used to stay unnoticed forever
- **The MAC address is carried over** into the allocation when linking, taken from the interface the address was found on
- **A duplicate address can no longer be stolen from a live guest** — when two guests advertise the same IP (typically a stale `ip=` left in the config of a stopped container), the owner is kept and the conflict is reported instead of silently reassigning the record
- **Allocations are no longer matched by resource name** — two guests named alike on different servers made one guest display the other's address
- **IPAM history no longer fails on datetime values**, which used to abort re-linking with a 500
- **Guest IP is re-checked at most once every 5 minutes per guest** instead of on every cache sync (every 10 s), removing hundreds of pointless Proxmox calls per minute

### 🐛 Other fixes

- **Scripts with non-ASCII text run on VMs again** — Proxmox base64-encodes the guest agent payload in Perl, which dies with `Wide character in subroutine entry` as soon as the script contains non-ASCII (a Cyrillic comment is enough). The payload is now handed over as bytes
- **Next-free-IP search can scan the whole subnet** when a network has no auto-assign pool, skipping node addresses and cached guest addresses so it never hands out an address that is already live

---


### 🖥️ LXC console — failed termproxy tasks and the black screen

- **⚠️ Breaking change: `GET /api/{server_id}/container/{vmid}/terminal` has been removed.** The WebSocket endpoint `/proxmox/ws/terminal/{server_id}/{node}/{vmid}` creates the termproxy session itself, so the REST call was unused by the panel and left an orphaned session on the node on every invocation. External clients should open the WebSocket directly
- **Opening an LXC console no longer leaves a failed task on the node** — `failed waiting for client: timed out` / `TASK ERROR: command '/usr/bin/termproxy 5900 ... vzctlconsole<id>' failed`. The console page created a termproxy session over REST (using only the `node` field of the response and discarding port and ticket), then a second one over the WebSocket endpoint and attached to that one. Nobody ever attached to the first, so Proxmox waited ~10 s for a client and failed the task
- **Console no longer shows a black screen with a "connected" badge in dev** — React StrictMode mounts the effect twice and the connect helpers are async, so effect cleanup ran before they had assigned `wsRef`/`termRef` and cleaned up nothing. Two xterm instances ended up in the same container: the one filling the viewport was orphaned while the live one was pushed out of the overflow-hidden area. Sessions now carry a cancellation guard, and both mount and the reconnect button go through a single entry point that tears the previous session down first. The same guard was applied to the node shell page

---

### 🔍 NFS storage dialog — scan for available exports

- **Added a "scan exports" button** next to the server field when adding an NFS storage — calls Proxmox's `GET /nodes/{node}/scan/nfs` and turns the export path field from a free-text input into a select populated with the exports actually found on the target server, instead of requiring the path to be typed by hand and guessed
- New endpoint `GET /api/backups/scan-nfs/{server_id}?server=<ip>` on the backend, running the scan through the server's first available node

---

## [v1.17.0] - 2026-08-20

### 💾 Move LXC container disks to another storage

- **Disk move now works for LXC**, not just QEMU VMs — the Move disk action was previously hardcoded to the VM `move_disk` API and unreachable for containers (no Hardware tab, and the disk-row detector only matched VM device names). `rootfs`/`mp\d+` are now recognized as LXC disk devices, and a **Move disk** card was added to the Settings tab for LXC instances
- New endpoint `POST /api/{server_id}/container/{vmid}/disk/move` calls Proxmox's `lxc/{vmid}/move_volume`; the target storage list is filtered by supported content type (`rootdir` for LXC, `images` for VM) instead of showing every storage
- **Blocked on running containers with a guided flow** — Proxmox rejects `move_volume` on a running LXC (`move_disk` works live for QEMU, `move_volume` does not for LXC). Attempting it now opens a confirmation dialog that stops the container, moves the disk, and restarts it, showing progress at each step; the container is always restarted even if the move itself fails. VMs and already-stopped containers still get a plain confirm dialog

### 📊 Instance list — collapsible metrics panel

- **CPU/RAM/Disk/Net/Disk I/O/Uptime moved out of the always-visible table columns** into a per-row panel that expands when you click the instance name, matching the disclosure pattern from VMmanager. The panel adds a **Parameters** link straight into the instance's Settings tab and clearer stat cards for network/disk throughput
- vCPU/RAM bars widened and enlarged (bar width and label/detail font size) for easier at-a-glance reading in the collapsed panel

### ✨ Network topology page

- **New `/topology` page** — an interactive map of the infrastructure: panel → cluster → Proxmox node → VM/LXC, rendered with React Flow. Node cards carry live CPU/RAM meters, guests show status, IP, bridge/VLAN and tags
- **Filters** — narrow the graph to one connection, filter guests by status (all / running / stopped), and collapse a node's guests into a single badge above a configurable threshold (remembered per browser); expand a collapsed node individually
- **Canvas tools** — zoom, pan, fit-to-screen, fullscreen, minimap, legend and PNG/SVG export of the whole graph
- **New API `GET /proxmox/api/network/topology`** — one aggregated request instead of fanning out over servers/nodes/SDN/VMs; gated on `network:view`, honours workspace scoping and per-owner VM isolation. Unreachable connections degrade to cached data plus a warning instead of failing the page
- **`netN` parser** — guest NIC strings are now parsed (bridge, VLAN tag, MAC, model, firewall, rate, MTU) for both the QEMU and LXC dialects, available via `include_nics=true`

---

## [v1.16.1] - 2026-08-17

### 🎨 UI

- **Inter Variable font** — the frontend now uses the variable-weight Inter font with a bumped type scale for sharper, more consistent typography across the panel

### 🔧 Deployment Fixes

- **`pve` CLI installs without root** — `deploy.sh` now tries `sudo` to install the CLI to `/usr/local/bin`, and falls back to `~/.local/bin` (with automatic PATH configuration) when root access is unavailable; previously the auto-install was silently skipped, leaving the `pve` command unavailable after deploy
- **`pve` CLI file permissions** — the installed script is now set to `755` instead of relying on umask; previously `sudo cp` could leave the file without read permission for non-root users, causing "Permission denied" on every invocation
- **SSL no longer rolls back on deploy** — `verify_nginx_running` now uses `docker compose ps --format '{{.State}}'` (returns `"running"` immediately) instead of grepping the human-readable STATUS column (which shows `"Up Xs (health: starting)"` for 30 s before the first healthcheck); with the old 20 s timeout the function always timed out, rolling back to HTTP even though the SSL certificate was successfully obtained

---

## [v1.16.0] - 2026-08-14

### 🌍 i18n — Full Translation Pass

A comprehensive sweep that eliminated the remaining hardcoded and untranslated strings from the UI:

- **Dashboard & Instances** — action dialogs, resource cards, status labels, and bulk-operation descriptions are now fully translated (EN + RU)
- **Images page** — image browser, download/import dialogs, mirrors manager fully translated
- **App Store** — missing keys added for install wizard, operation states, and app detail labels
- **Nodes** — power action labels (`Start`, `Reboot`, `Shutdown`) and common node-admin strings translated
- **Scripts & Certs** — Scripts catalog and TLS/certificates sections added to both locales
- **Roles** — permission labels for all RBAC entries, including `remote_migrate`, translated to Russian
- **Graphs, Wizard, Storages** — previously hardcoded Russian strings moved to locale files and dual-translated
- **Bulk migrate & Access realm** — new locale keys for bulk migration actions and Proxmox realm labels
- **Critical fixes** — several strings that were silently falling back to English in the Russian UI are now correctly translated

### 🔑 SSH Keys

- **Swap SSH keys on owner change** — when an admin reassigns a VM/LXC to a different user, the instance now automatically receives the new owner's SSH public key (and the previous owner's key is removed), keeping access credentials in sync with the current owner

### 📦 Cloud Images

- **`patch-cloud-images.sh`** — new helper script that patches official cloud images to include `qemu-guest-agent`, then publishes them to a GitHub release mirror; enables live metrics and guest-agent features without requiring a post-deploy agent install
- **amd64 image catalog** — default amd64 image entries now point to the GitHub mirror (qemu-guest-agent pre-installed) and include `sha256` checksums for integrity verification

### 🐛 Bug Fixes

- **VM storage overview** — loop devices (`/dev/loop*`) and system partitions (`/dev/sr*`, `/boot`, `/snap`) were included in per-disk usage cards; they are now filtered out so only data disks are shown
- **Cloud image re-download** — cached import images were served from disk even when a checksum was provided and the remote file had changed; the cache is now invalidated when a checksum mismatch is detected
- **Password visibility icon** — the eye icon across all password fields (login, change-password, SSH key passphrase, and API-key dialogs) was inverted: open eye was shown when the field was hidden and vice-versa; icon state now correctly reflects whether the value is visible

---

## [v1.15.0] - 2026-08-13

### 🚀 One-Command Bootstrap

- **`bootstrap.sh`** — installs the panel with a single command on either a plain Debian/Ubuntu host or directly on a Proxmox VE host. On a PVE host it provisions a Debian 12 LXC, installs the panel inside it via the existing `deploy.sh`, creates a local `root@pam` API token and registers that host as the panel's first node — no Proxmox password is ever transmitted
- Tolerates `apt-get update` failures from an unsubscribed PVE host's enterprise repo instead of aborting the whole bootstrap before `git`/`curl` are installed
- Registers the host at the correct `/proxmox/api/servers` path (the previous `/api/servers` 404'd, since the Proxmox router is mounted under `/proxmox`)
- Removed the insecure `pvemanager_secure_password` fallback for `POSTGRES_PASSWORD` in `compose.yml` — every deploy path already generates a real `.env` before the stack starts

### 💿 Images & Instances

- **ISO eject/mount rework** — the Images page now shows real ISO files already present on node storage (including ones that predate the node joining the panel); "Detach" is renamed **Eject**, the current image is shown inside the select, and a new **"boot from disk"** / **"boot from this ISO"** checkbox controls boot order on eject/mount
- Applying a new boot order reboots the guest via a **hybrid restart** — graceful ACPI shutdown with a forceStop fallback, then start — avoiding the guest-ping timeout seen on a freshly installed OS with no QEMU Guest Agent yet

### 🔒 RBAC & Access

- **`/profile` split from `/settings`** — account settings (profile, password, 2FA, SSH keys) moved to their own permission-free `/profile` page reached from the user menu and Ctrl+K, so revoking `setting:view` to keep a user out of panel settings no longer locks them out of their own account. `/settings` keeps the panel-wide tabs behind `setting:view`, with saving behind `setting:update`, security behind `setting:manage`, and SMTP/Telegram behind admin-only
- **Routes are now guarded by permission**, not just hidden from the sidebar — `/users`, `/logs` and `/settings` (and their tabs) previously rendered for anyone who typed the URL and let the backend answer with 403s; denied users are now redirected to the first page they can open
- Migration 42 drops `setting:view` from roles that don't also hold `setting:update`/`setting:manage`, since those grants only existed to reach the old combined settings page

### 👤 Ownership & Quotas

- **Owner assignment fixed at creation time** — newly created VMs/LXCs were silently ending up with `owner_id = NULL` because the sync job that registers a still-cloning VM beats the deploy flow to the database row; owner is now set on that update path too. A VM created by an admin on behalf of a user now also injects the **user's** SSH key instead of the admin's
- **Backfill migration** for instances created before the fix above, recovered from `deploy_tasks` and `audit_logs` and applied only when every piece of evidence agrees on the same existing user; ambiguous instances are left ownerless for `assign_instances_owner.py` to resolve by hand
- **Create Instance is disabled once a quota has no headroom left**, instead of only refusing inside the wizard — the button shows a tooltip naming which limits are full (skipped for admins, whose own quota doesn't apply to instances they create for someone else)
- The pre-wizard quota block is now distinct from the in-wizard one: no headroom at all keeps the wizard from opening; headroom that a chosen configuration would exceed opens the wizard but disables Next/Deploy with the offending metric named

### 🐛 Bug Fixes

- **Production deployments were bypassing TLS** — `compose.prod.yml`'s `ports: []` didn't actually drop the published `8000`/`3001` ports (Compose concatenates list-valued fields across `-f` files instead of overriding), so an SSL deployment kept answering unencrypted on both ports alongside HTTPS. Fixed with the `!reset` tag
- **SSL deployments reported "Failed to obtain SSL certificate" after successfully issuing one** — the success check couldn't read certbot's root-owned, mode-0700 output directory as a non-root user; the check now verifies the certificate from inside the container and falls back to HTTP only if nginx actually fails to come up with the new config
- **The frontend bundle's cold load could render a blank page** — the general request-rate limit (`10r/s`) applied to static assets too and started rejecting the SPA's ~86 preloaded chunks with 503s; and once that was fixed, `limit_conn addr 10` capped the whole page to 10 concurrent HTTP/2 streams (a browser multiplexes a page's entire load over one connection), producing the same blank page from a different limiter. Static assets now have their own rate-limit zone, and both `limit_conn` caps are raised
- **nginx workers were unable to open enough file descriptors** to match `worker_connections 2048` (Docker's default `RLIMIT_NOFILE` of 1024 quietly halved the real ceiling); raised to 8192
- Dropped `listen 443 ssl http2` (deprecated since nginx 1.25.1) and `ssl_stapling` (no OCSP responder in current Let's Encrypt certs) — both warned on nginx's periodic reload
- **Node uptime showed the wrong value on multi-node clusters** — the server card took `Math.max` over every cluster node's uptime instead of the current node's own, so rebooting one node could still show another node's higher uptime
- **The terminated-sessions toast never translated** — the message was assembled server-side in Python and returned verbatim, so the Russian UI showed "Terminated 0 sessions"; the count is now returned separately and the phrase is composed client-side (Russian needs four plural forms against English's two)

---

## [v1.14.0] - 2026-07-24

### 🐛 Bug Fixes

- **Workspace switch left stale instance filters behind** — the server, node and per-column filters on the Instances page kept whatever values were picked in the previously active workspace, which could filter the newly-scoped VM list down to nothing. Switching the active workspace now resets all of them
- **"Node" column filter never updated after the first render** — its checklist of available nodes was memoized against the TanStack `Column` object, which keeps the same identity across data updates, so the list stayed frozen at whatever nodes were visible when the popover first mounted — including nodes from workspaces you'd since switched away from. It now reads live from the table's faceted values on every render

---

## [v1.13.0] - 2026-07-24

### 🛒 App Store

- **Storage selection for the golden template** — the "Golden LXC template" dialog now offers dropdowns for template storage (`vztmpl` content) and rootfs storage (`rootdir` content) populated from the selected server/node, instead of free-text fields. Selection auto-picks `local`/`local-lvm` when present, falling back to the first available storage

### 📜 Scripts

- **Name search in the scripts catalog** — filter manual, Git and Community-Scripts entries by name directly from the Scripts page

---

## [v1.12.0] - 2026-07-24

### 🔗 Cluster

- **Import an existing cluster** — nodes already joined into a Proxmox cluster outside the panel (via `pvecm`/the Proxmox UI) can now be linked in the Cluster page instead of only via the panel's own Create/Join flow. Detection reads `cluster/config/join` on the selected node, matches its nodelist against known panel servers by IP/hostname, and links the matching panel records to that cluster group — no `pvecm` operation is run, only the panel's own bookkeeping is updated

---

## [v1.11.0] - 2026-07-24

### 🔌 Servers & Connectivity

- **Auto-provisioned API tokens** — connect a server with a login and password once; the panel authenticates, creates its own API token on the node (`privsep=0`) and uses it from then on. The token can be revoked from the Proxmox UI, cannot be used for SSH or web login, and appears in the PVE audit log under its own name. Existing password-auth servers can be migrated in place via **Provision token**
- **Rejected credentials vs. unreachable node** — an offline server used to report a single "Failed to connect"; now a stale/rejected token (401, typically left behind after a cluster join replaces `user.cfg`/`token.cfg`) shows its own `auth_error` state with a hint explaining the cause
- **Per-node storage availability** — a storage can be declared cluster-wide but missing on a given node (e.g. an absent volume group or pool); this is now surfaced instead of staying invisible
- **Unified status badges** — every server list (including cluster topology and user-assignment views) renders the same `ServerStatusBadge` instead of a repeated online/offline ternary
- **Workspace-scoped listings** — `/api/cluster/topology` and `/api/resources/all` now respect the active workspace instead of returning every server on the panel; live metrics are fetched in parallel instead of serially, so one unreachable cluster no longer stalls the whole request

### 📊 Monitoring

- **Node Graphs tab** — CPU, IO delay, memory, load average, network, swap and root filesystem charts on the node detail page, with `1h/24h/7d/30d/1y` presets

### 💿 Images & Instance Creation

- **Local file upload** — upload an ISO or `vztmpl` directly from your machine to node storage, alongside the existing URL-download flow
- **Blank VM for ISO installs** — a new instance kind in the create wizard: an empty-disk VM with the ISO mounted and booted first, so the guest starts straight into the installer over noVNC; picking Windows presets OVMF, a SATA disk and an e1000 NIC
- **ISO downloads** — fetch an ISO onto a node by URL from the Images page or from a saved mirror (mirrors now accept `kind=iso`)

### 🔄 Realtime & UX

- Instance and workspace lists now update immediately after assigning servers/users, deploying, or deleting a VM/container — no manual refresh needed
- The node overview no longer flashes "no data" while the instance list is still loading

### 🛠 Reliability

- **Panel self-update recovers from a stalled rebuild** instead of hanging at "restarting — 80%" forever; after a timeout it surfaces a clear "failed" state pointing at the host update logs

### 🎨 Theme

- Dark theme surfaces (background, card, popover, sidebar, borders) shifted from neutral to a blue-graphite hue

### 📖 Documentation

- Expanded the RBAC/Security guide (permission format, evaluation order, full permission catalogue, per-page map) and added a complete Russian translation, **WIKI.ru.md**

---

## [v1.10.2] - 2026-07-23

### 🐛 Fixes

- **Templates** — after an auto-import, a clear message is shown instead of `undefined`

---

## [v1.10.0] - 2026-07-22

### 🧭 Navigation & Instances

- **Command palette (Ctrl+K)** — searchable palette over a shared registry of navigation items (its own store), for jumping to any page from the keyboard
- **Unified navigation** — the sidebar and command palette are driven by one shared nav-item registry
- **Expanded instance menu** — the per-instance row menu gains **Options**, **Migrate**, **Change resources**, **Create backup** (new `BackupDialog`) and **Delete**; the menu no longer closes on every live list refresh (stable `columns` + `getRowId`)
- **Instance tab reorganization** — for VMs, *Compute resources*, *CPU options*, *Disk management* and *Disk resize* moved from **Settings** to **Hardware**; LXC (which has no Hardware tab) keeps them in **Settings**. Shared cards extracted into `ResourceCards.tsx`
- **New shared UI primitives** — `alert`, `breadcrumb`, `checkbox`, `progress`, `switch`, `textarea`; RU/EN locale keys for the new menu items and backup

### 🛒 App Store

- **Global minimized-operations tray** — minimized long-running operations (app install, golden-template build) now live in a global zustand store (persisted to `localStorage`) and are rendered by a `MinimizedOpsTray` in the app layout instead of by the dialog itself:
  - the chip **survives page navigation and a tab reload**; the tray polls progress on its own (REST, every 3 s) independently of the dialog
  - clicking a chip returns to the operation's page and reopens the dialog — install resumes in `InstallWizard` (resume mode via navigation state), template reopens the golden dialog with the server preselected
  - **Minimize** adds the operation to the tray and closes the dialog; the chip's ✕ only removes the chip (the operation keeps running on the server); chips for operations that no longer exist (404 after reload) are cleared automatically
- **App data files** — files bundled with a catalog app (`data_files`) are delivered before `compose up`; Umbrel `APP_PASSWORD` / `default_credentials` are honored
- **Minimizable install/build dialogs** — long operations can be collapsed via `MinimizableDialog`
- **Install/uninstall reliability**:
  - **teardown** — a CT that no longer exists in Proxmox is treated as removed, so an `orphaned` app can finally be deleted from the DB
  - **install** — the VMID search skips already-occupied records (`nextid` does not reserve the number, so a retry could grab another container)
  - **retry** — install parameters (`cores/memory/disk/storage/bridge/ip_config/port`) are saved to `installed_apps.install_params` (JSONB, new migration) and reused on retry instead of falling back to hard-coded defaults
  - **reconcile** — an unreachable server no longer flags all of its apps as `orphaned`
  - **update** — the actually-published port is recorded in the DB (`url`/health)
  - **.env** — values containing `#` are quoted (the dotenv parser was trimming them as an inline comment)
  - port `80`, when chosen explicitly in the wizard, is published as-is
  - `useCatalog` — `available_only` is part of the query key (fixes a cache collision)

### 🔒 Owner Isolation

- **Live Proxmox resources are now owner-scoped** — the instance list (`/api/virtual-machines`) was already filtered by `owner_id`, but the live-resource endpoints `/api/resources/all` (dashboard) and `/api/{server_id}/resources` (node/server page) returned **all** VMs/LXC. A regular user (`proxmox.view`) could see other owners' instances through the node page. Both endpoints now filter VM/LXC by owner for non-privileged users (matching `vmid` against the `vm_instances` cache); admins and roles with `vm:manage` still see everything
- **`assign_instances_owner.py`** — script to bulk-assign an owner to existing instances whose `owner_id` is `NULL` (instances synced from Proxmox before ownership existed)

### 🐛 Fixes

- **React Query cache cleared on login/logout** — logging in as a different user in the same tab no longer serves the previous user's cached data (instance list, etc.) until a hard reload. `queryClient` was moved out of `App.tsx` into `lib/query-client.ts` so it is reachable outside React, and `auth-store` calls `queryClient.clear()` on successful login and on logout

---

## [v1.9.1] - 2026-07-17

### 🛒 App Store

- **Per-server golden template** — the golden vztmpl is now stored and resolved per Proxmox `server_id` instead of one global pointer; building it on one server no longer overwrites/breaks the template configured for another. `APPSTORE_GOLDEN_TEMPLATE` is now only the last-resort fallback for servers without their own build. Migration 37 adds `app_operations.server_id`
- **Golden template build fix** — the automated build (`App Store → золотой шаблон`) no longer fails at the final `vzdump` export step (`unable to parse directory volume name 'vztmpl/probe'`)
- **DNS reliability** — App Store LXCs (and the golden-template build CT) now get an explicit `nameserver` (`APPSTORE_NAMESERVER`, default `1.1.1.1 8.8.8.8`) instead of inheriting the node's resolver, which fixed image-pull timeouts on nodes whose DNS is only reachable from the host (e.g. over Tailscale)
- **Bind-mount permissions** — data directories referenced in an app's `volumes:` (`${APP_DATA_DIR}/...` or relative paths) are pre-created before `up`, fixing `Permission denied` for containers that write as a non-root user (e.g. MariaDB)
- **Delete/reinstall race fixed** — deleting an app now waits for Proxmox to actually confirm the container is gone (up to 30 min, covers slow disk wipes) before freeing the VMID, instead of assuming success after a short timeout
- **`pct create` lock retry** — transient "can't lock file ... got lock" errors right after a delete are retried a few times instead of failing the install immediately
- **Full error output** — failed pipeline steps now surface the last 4000 characters of command output (previously the first 2000), so the actual failure reason isn't buried under `docker compose pull` progress noise
- **Install error UI** — the error box in the install dialog now wraps long unbroken text (image digests, paths) and scrolls within its own bounded height instead of stretching the dialog

---

## [v1.9.0] - 2026-07-09

### 🌐 IPAM

- **Networks bound to server/node and workspace** — an IPAM network can now be tied to a specific Proxmox server/node and to a workspace; each workspace uses one subnet, and the network is auto-selected by the active workspace in the Create Instance and App Store install wizards, so users no longer pick a network manually in the common case
- **Auto-selection in wizards** — the deploy wizards resolve the target network/pool from the current scope and pre-fill it; a missing binding falls back to manual selection
- Migration: IPAM network workspace-binding columns + default flag added via `backend/migrations/migrations.py`

### 🔧 Nodes

- **FQDN/domain accepted in the Proxmox host field** — the Add/Edit Proxmox server form now accepts a domain name or FQDN (not only an IP), resolved when connecting

---

## [v1.8.1] - 2026-07-09

### 🛒 App Store

- **Umbrel as a second catalog source** — the catalog is now multi-source: alongside `runtipi/runtipi-appstore` you can enable `getumbrel/umbrel-apps` (~380 apps). Sources are configured via `APPSTORE_SOURCES` (comma-separated, default `runtipi`); Umbrel app ids are namespaced (`umbrel-<id>`) so catalogs never collide
- **Umbrel compose adapter** — install understands Umbrel manifests: the container port is taken from the `app_proxy` service (`APP_PORT`), the Umbrel runtime services (`app_proxy`, `tor`) are stripped, dangling `depends_on` are cleaned, and the main service port is published on the LXC host so the health-check passes; Runtipi behaviour is unchanged
- **Umbrel icons** — icons are fetched from the separate `getumbrel/umbrel-apps-gallery` repository (`<app-id>/icon.svg`) during sync and cached like Runtipi logos; a gallery fetch failure never fails the catalog sync
- **Multi-source sync** — each source syncs independently (a network failure of one doesn't cancel the others), and the *disappeared → unavailable* pass is scoped per source so a Runtipi sync never touches Umbrel rows
- **Source filter & badge (UI)** — the App Store adds a source filter and a per-card source badge, shown only when more than one source is enabled; catalog metadata now exposes the available `sources`
- **Custom install port** — the install wizard accepts a custom host port (validated 1..65535), overriding the catalog default
- **Instant instance cleanup** — deleting an app soft-deletes the linked VM instance immediately for a snappier UI
- **Data model & settings** — `catalog_apps.source` column (migration 34); new env settings `APPSTORE_SOURCES`, `UMBREL_APPSTORE_REPO`, `UMBREL_APPSTORE_REF`, `UMBREL_APPSTORE_GALLERY_REPO`

---

## [v1.8.0] - 2026-07-07

### 🛒 App Store

- **Self-hosted app catalog with one-click install** — new App Store module: each application is deployed into its own **unprivileged LXC** container running **Docker Compose** (1 app = 1 LXC). The catalog is imported from the `runtipi/runtipi-appstore` repository (~265 apps pass the filter)
- **Catalog Service** — imports app metadata, logos, descriptions and compose files into the new `catalog_apps` table; idempotent sync (manual *Refresh catalog* button + automatic every 24 h via APScheduler); a broken `config.json` is skipped without failing the whole sync; the `update_available` flag is computed from `tipi_version`
- **Install pipeline** — clone golden LXC template → `pct push` compose/`.env` → `docker compose up -d` → HTTP health-check, with a **live step journal over WebSocket**, dynamic `form_fields` rendering (text/password/email/number/boolean/fqdn), auto-generated `random` secrets, and Fernet-encrypted env storage; generated credentials are shown once
- **Lifecycle management** — start / stop / restart / logs (`docker compose logs`) / delete, plus background **state reconciliation** every 60 s (containers removed outside PVEmanager are flagged `orphaned`)
- **Update & Rollback** — update takes a pre-update Proxmox snapshot, then `docker compose pull && up -d` with health-check; rollback restores the snapshot (data included); only the latest pre-update snapshot is kept
- **UI** — App Store grid (search, categories), app detail page with a source disclaimer and compose preview, install wizard with advanced LXC options and live progress, and a **My Apps** management screen; RU/EN locales; *Powered by Runtipi appstore* attribution
- **Security** — unprivileged LXC only; `pct exec` / `pct push` via SSH with base64 file delivery and escaped `.env` values (no shell injection); new RBAC permissions `app:view` / `app:install` / `app:manage`
- **Data model & settings** — `catalog_apps`, `installed_apps`, `app_operations` tables (migrations 31–32); new env settings `APPSTORE_GOLDEN_TEMPLATE`, `RUNTIPI_APPSTORE_REF`, `APPSTORE_DATA_DIR`, `CATALOG_SYNC_INTERVAL_HOURS`, `APPSTORE_HOST_ARCH`
- **Docs** — `docs/golden-template.md` (golden LXC template preparation) and `docs/appstore-poc.md` / `appstore-catalog.md` / `appstore-engine.md`

---

## [v1.7.0] - 2026-06-26

### 🎯 Quotas

- **Per-user resource quotas** — limit each user's number of instances and the summed vCPU, RAM and disk across their VMs/LXC. A missing quota row or a `NULL` column means *unlimited* for that metric, so admins and unrestricted users are never blocked
  - Backend: new `UserQuota` model (`user_quotas` table) and `quota_service` (`get_user_usage` / `check_quota`); usage is computed from the `vm_instances` cache (`owner_id`) — non-deleted, non-template instances only; enforcement raises **HTTP 429** on VM/LXC deploy when a limit would be exceeded
  - Permissions: new `quota:view` and `quota:manage` RBAC permissions (category *User Management*, both require `user:view`)
  - API: `GET/PUT /api/users/{id}/quota` (admin) returns limits plus current usage; `GET /api/quota` (self-service) returns the current user's own limits and usage
  - Frontend: admin quota dialog with usage bars on the Users page; own-limits card on the Settings page; remaining-quota hint in the Create Instance wizard config step; `useUserQuota` / `useSetUserQuota` / `useMyQuota` hooks; RU/EN locales
  - Migration: `user_quotas` table added via `backend/migrations/migrations.py`

---

## [v1.6.0] - 2026-06-23

### ✨ Images

- **Cloud image catalog** — browse and download OS images from multiple sources (default Proxmox mirror and custom mirrors); images are downloaded to Proxmox storage
- **Auto-convert to VM template** — option to automatically convert downloaded `.qcow2` images to Proxmox VM templates on completion
- **Custom mirrors for admins** — admin-only UI to add, edit, list and delete custom mirror sources for image discovery
- **Architecture-aware filtering** — images filtered by node platform architecture (x86-64, aarch64); default architecture matches the selected node's platform
- **Image download with progress** — download dialog with real-time progress tracking and optional template conversion toggle

### 🐛 Bug Fixes

- **Image import filename normalization** — `.img` extension automatically normalized to `.qcow2` during import
- **VM sanitization on image import** — VM names are sanitized during image import to prevent creation errors; improved error messages for transparent user feedback

---

## [v1.5.2-1] - 2026-06-20

### 🖥️ UI

- **Styled confirmation dialogs** — all native browser `confirm()` popups replaced with a consistent themed modal (`ConfirmDialog`). Added global `ConfirmDialogProvider` + `useConfirm` hook (Promise-based API). Affected: update panel, delete template/server/storage/SSH key/HA resource/SDN zone/VNet/subnet/IPAM pool/allocation, apply/revert network changes, IPAM orphan cleanup (16 call-sites across 11 files).

---

## [v1.5.2] - 2026-06-20

### 📊 Metrics

- **Real-time VM disk fill, IOPS and network I/O** — live per-disk usage, disk read/write and network in/out rates added to the instance Overview tab, instances list and dashboard top-loaded list
  - Backend: `get_vm_fsinfo()` pulls per-filesystem usage via QEMU guest agent; `metrics_broadcaster` computes I/O rates (bytes/sec) from cumulative counters; instances-list loop now refreshes each running VM via `status/current` (1 s granularity) instead of `cluster/resources` (10 s pvestatd), with bounded concurrency and 1 s cadence
  - Frontend: per-disk cards, Network I/O and I/O-rate cards on OverviewTab via WebSocket; Net and Disk I/O columns in InstancesPage; Disk% shown for LXC; realtime metrics overlay in DashboardPage top-loaded list

### ⚙️ Settings

- **Per-user language preference** — each user account now has its own `language` column (migration 26); the language selector moved from the Panel tab to the Profile tab; language is saved to DB and applied via `i18n.changeLanguage()` immediately on save
- **Panel tab improvements** — `panel_name` field is read-only in UI (falls back to env `PANEL_NAME`); `log_retention_days` field exposed; Panel API accepts `panel_name` in `PUT` to allow DB override without touching the env var

### 🐛 Bug Fixes

- **SMTP/security settings fields** — renamed SMTP request/response fields (`host`, `port`, `username`, `password`, `from_email`, `tls`) and pointed security settings hooks at `/admin/api/security/settings` with a typed `SecuritySettings` shape; added `minutes` / `security_saved` i18n keys
- **Notifications form UX** — added success/error toasts to SMTP and Telegram save buttons; Profile tab language selector wired up; password-change fields cleared only on success
- **Telegram admin section** — removed the Chat ID field from the global Telegram section (it was never persisted to DB); testing is now done via the personal "My notifications → Send test" flow
- **Webhook URL field** — removed the unimplemented Webhook URL field from the notifications settings to avoid misleading users

---

## [v1.5.1] - 2026-06-04

### 🐛 Bug Fixes

- **Frontend build: missing LogsPage component** — `src/router.tsx` referenced `./features/logs/LogsPage.tsx`, but the file was missing, so Vite 8.0.5 / rolldown failed to resolve the dynamic import. Added `frontend/src/features/logs/LogsPage.tsx` with stats cards (24h total, errors, failed logins, page count), level filter, search, pagination and i18n strings. Wires up to existing `useLogs` / `useLogStats` / `useLogLevels` hooks.
- **Frontend build: `.gitignore` collision** — `frontend/.gitignore` had a bare `logs` entry that matched both the root `logs/` directory and `src/features/logs/`, hiding the new page from VCS. Scoped the rule to `/logs` so the features tree is no longer ignored.

### 📚 Documentation

- WIKI: bumped to v1.5.1 and added a new **Logs & Audit** section with page layout, log-level colour mapping, API reference (`/logs/api/logs`, `/logs/api/stats`, `/logs/api/levels`, `/logs/api/categories`), query parameters, response shape and access-control notes
- README: updated version badge to 1.5.1

---

## [v1.5.0] - 2026-05-07

### 🐧 LXC — CT Template Deployment

- **Create LXC from CT templates** — full deployment workflow for creating LXC containers directly from Proxmox CT template files (`.tar.zst`, `.tar.gz`); node/storage selection, IPAM integration, SSH keys support
- **Unified Create Instance Wizard** — redesigned wizard (`CreateInstanceWizard`) supports both VM templates (QEMU) and CT templates (LXC) in a single multi-step flow with type selector, template browser, config step, and confirmation
- **Reinstall LXC from CT template** — reinstall support for containers created from CT templates; hostname, password, SSH keys, and IP address are preserved across reinstall
- **Preserve credentials/IP across LXC reinstall** — root password, SSH keys, IP configuration, and gateway are carried over automatically when reinstalling an LXC container
- **nesting=1 by default** — all newly created LXC containers get `nesting=1` enabled to ensure compatibility with systemd 255+ (cgroup v2)
- **Fix: double URL-encoding of SSH public keys** — SSH keys passed to Proxmox API during LXC creation no longer get double-encoded

### 🔑 SSH Keys Management

- **SSH Keys API** — full CRUD for user SSH key library: `GET /api/ssh-keys`, `POST /api/ssh-keys`, `PUT /api/ssh-keys/{id}`, `DELETE /api/ssh-keys/{id}`
- **Admin key management** — `GET /api/ssh-keys/user/{user_id}` — admins can view and manage SSH keys of any user
- **Key fingerprint** — SHA-256 fingerprint is computed and stored automatically on key creation/update
- **Private key storage** — optional encrypted private key storage alongside the public key
- **SSH Keys Manager UI** — new tab in Settings page for adding, editing, and deleting SSH keys; shows key name, fingerprint, comment, and creation date
- **Key selector in wizard** — when deploying a VM or LXC, SSH keys can be selected from the personal library; admins deploying on behalf of another user can pick from that user's keys
- **Per-user isolation** — regular users manage only their own keys; users with `users.manage` permission can manage keys for any account

### 🗑️ VM/LXC Operations

- **Auto-stop before destroy** — if a VM or LXC container is running when a delete request is issued, it is automatically stopped first; prevents Proxmox `can't lock file` errors on destruction

### 🖥️ Frontend Improvements

- **InstancesPage** — refreshed layout, improved status badges and action menus
- **NodesPage** — updated node cards with cleaner metrics display
- **TemplatesPage** — improved template list with better filtering and group management
- **BackupsPage** — fixed several edge-cases in backup listing, restore dialog, and job scheduling UI
- **UsersPage** — improved user table with better role display and server assignment indicators
- **SettingsPage** — added SSH Keys Manager section; general UX polish
- **`api-client.ts`** — added SSH keys API methods and LXC template endpoints
- **`websocket.ts`** — connection stability improvements
- **`types/index.ts`** — added `SSHKey`, `LXCTemplate`, `LXCDeployRequest` types

### 🛠️ Developer Experience

- **Dev mode** — new `compose.dev.yml` override for Vite HMR (hot module replacement); use `pve dev` or `docker compose -f compose.yml -f compose.dev.yml up -d frontend`; source files are mounted as volumes so changes apply instantly without rebuilding
- **`.dev-mode` marker** — signals to the `pve` CLI that the frontend is running in dev mode

---

## [v1.4.0] - 2026-04-06

### 🌐 Networks — SDN Management & Node Interfaces

- **Networks page** — new unified page (`/proxmox/networks`) for managing SDN zones, VNets, subnets, and node-level network interfaces; added to sidebar navigation with dedicated icon
- **Edit SDN Zone** — `PUT /api/servers/{server_id}/sdn/zones/{zone}` — update zone properties (mtu, dns, reversedns, ipam, etc.)
- **Edit SDN VNet** — `PUT /api/servers/{server_id}/sdn/vnets/{vnet}` — update VNet alias, VLAN tag, vlanaware, etc.
- **Delete SDN Subnet** — `DELETE /api/servers/{server_id}/sdn/vnets/{vnet}/subnets/{subnet_cidr}` — delete a subnet; optionally delete linked IPAM network (`?delete_ipam_network=true`)
- **Auto-create IPAM network on subnet creation** — `POST .../subnets` now accepts `create_ipam_network: true`; creates a matching `IPAMNetwork` linked to the server and VNet, or returns the existing one
- **Node network interfaces** — full CRUD for node-level interfaces (bridges, bonds, VLANs, ethernet): list, get, create, update, delete via `proxmox_client.py`
- **Apply / Revert node network config** — `PUT` and `DELETE` on `/nodes/{node}/network` to activate or roll back pending changes
- **Node list endpoint** — `GET /api/servers/{server_id}/nodes` — returns nodes for a Proxmox server (used by Networks page selectors)

### 🧹 Code cleanup

- **SDN module imports cleaned up** — removed unused imports (`ssl`, `asyncio`, `httpx`, `websockets`, `func`, `List`, etc.) from `sdn.py`
- **Networks router** — new `networks.py` module under `api/proxmox/` with HTML page route and node API endpoints; registered in `proxmox/__init__.py`

### 🌍 i18n

- **49 new translation keys** (EN + RU) for Networks page: interface types, bridge/bond/VLAN fields, apply/revert config, IPAM integration labels, edit zone/vnet, subnet deletion, and more

---

## [v1.3.1] - 2026-04-05

### 🔍 VM List — Node Filtering & Responsive Toolbar

- **Server filter** — new dropdown to filter the VM list by Proxmox server/cluster; populated dynamically from loaded VMs
- **Node filter** — new dropdown to filter by Proxmox node; automatically narrows to nodes belonging to the selected server
- Both filters are saved in URL params (`?server=...&node=...`) and restored on page reload
- **Toolbar redesigned into 2 rows** — row 1: all filters (type / status / server / node / search with icon + stats); row 2: select-all checkbox, pagination, action buttons
- **Responsive breakpoints** — at ≤900px button labels hide; at ≤768px filters wrap 2-per-row; at ≤480px each filter is full-width; stats counter hidden on mobile
- `populateServerFilter()` and `populateNodeFilter()` helper functions populate dropdowns from loaded VM data
- Snapshots toolbar migrated from legacy `.vm-toolbar-left/.vm-toolbar-right` classes to inline flex, unaffected by toolbar refactor

---

## [v1.3.0] - 2026-03-27

### 🖥️ Console — VNC & Terminal Fixes

- **VNC «Connection closed» fixed** — `RFBClass` (noVNC) now receives the WebSocket URL string directly instead of a pre-opened `WebSocket` object; the `onFirstMessage` hack that broke the RFB handshake before the first frame is fully removed from both `virtual_machines.html` and `instance_detail.html`
- **VNCAuth fixed for QEMU** — `vncproxy` is now called with `"generate-password": 1`; the returned 8-character DES key is passed to noVNC as the VNC password; falls back to `ticket` when `password` is absent (Proxmox 9.x NoAuth mode)
- **LXC `vncproxy` 500 fixed** — LXC `vncproxy` API does not accept `generate-password`; the parameter is omitted for LXC, which uses `ticket` as the VNC password
- **LXC context menu opens xterm.js terminal** — the right-click context menu in the VM list now shows **Terminal** for `lxc` type (calls `openTerminalConsole()`) and **VNC Console** for `qemu` type; previously all types used `openVNCConsole()`
- **Terminal modal added to VM list page** — `#terminalModal` with full xterm.js (FitAddon, WebLinksAddon, Dracula theme) added to `virtual_machines.html`; reuses existing backend endpoints `GET /api/{server_id}/container/{vmid}/terminal` and `WebSocket /proxmox/ws/terminal/{server_id}/{node}/{vmid}`

---

## [v1.2.0] - 2026-03-16

### 🐛 Bug Fix — Disk Resize Lock Conflict

- **Root cause fixed** — `resize.put()` in the Proxmox API is asynchronous and returns a UPID task; the old code did not wait for this task to finish before starting the VM, causing Proxmox to report `can't lock file '/var/lock/qemu-server/lock-xxx.conf' — got timeout` and `command '/usr/bin/qemu-img resize' failed: got timeout`
- **`configure_vm` (VM creation from template)** — now captures the resize UPID and calls `wait_for_task(120 s)` before returning; the VM can only be started after the disk has been fully resized
- **`resize_vm_disk`** — same fix applied; the resize task completes before the function returns
- **`resize_container_disk` (both LXC methods)** — same fix; the restart of the container is now issued only after the resize task is confirmed complete
- **Removed unnecessary post-resize VM restart** — QEMU disk resize operates at the storage layer and does not require a VM reboot; the automatic `restart_vm()` call after manual disk resize has been removed to prevent a second lock conflict

---

## [v1.1.8] - 2026-03-16

### 👤 User → Server Assignment

- **`user_servers` table** (Migration 20) — новая таблица-связка `User ↔ ProxmoxServer`; позволяет напрямую закреплять конкретные серверы за пользователями
- **API `GET /api/users/{id}/servers`** — список серверов, назначенных пользователю
- **API `GET /api/users/{id}/server-assignments`** — расширенный список всех серверов с флагами `assigned` и `compatible` (совместимость по воркспейсам)
- **API `PUT /api/users/{id}/servers`** — установка набора серверов для пользователя; проверяет пересечение воркспейсов — если сервер и пользователь не состоят ни в одном общем воркспейсе, возвращает `409 workspace_conflict`
- **Фильтрация серверов для non-admin** — обычный пользователь видит только серверы, которые ему назначены **и** входят в его воркспейс

### 🖥️ VM / LXC Owner

- **API `GET /api/{server_id}/vm/{vmid}/owner`** — получить текущего владельца VM/LXC и список доступных пользователей
- **API `PUT /api/{server_id}/vm/{vmid}/owner`** — назначить или снять владельца (admin-only); действие логируется в audit

### 🔐 RBAC & Роли

- **Migration 21** — `dashboard:view` принудительно выставлен в `false` для роли `user` (новый формат ключей)
- **Migration 22** — ключ `proxmox.cluster.manage` → `cluster:manage` в роли `admin`; старый dot-формат удаляется, чтобы не сбрасывать весь permission-map
- **`user_count`** в ответе `GET /api/roles` — для каждой роли возвращается количество активных пользователей

### 🚀 Умный редирект после логина

- После успешного входа производится запрос `GET /api/auth/me` — пользователь автоматически перенаправляется на первую страницу, к которой у него есть доступ (`/dashboard` → `/virtual-machines` → `/vms` → `/backups` → `/ipam` → `/logs` → `/settings`)
- Поддержка параметра `?returnUrl=` — возврат на запрошенную страницу после логина

### 🔧 Прочие улучшения

- `GET /api/servers` для не-привилегированного пользователя объединяет фильтры `assigned_servers` и `WorkspaceServer` — пользователь видит только серверы из своего воркспейса, которые ему назначены
- `node_uptime` добавлен в ответ детального API сервера
- Удалена лишняя проверка `PermissionChecker` на GET-роуте страницы `/users` (HTML)

---

## [v1.1.7] - 2026-03-16

### 🌍 i18n — Translation Files Refactored

- **Translations moved to JSON locale files** — all 1 149 translation keys extracted from the Python source into `backend/app/locales/ru.json` and `backend/app/locales/en.json`
- **`i18n.py` reduced from 5 287 → 128 lines** — now a lightweight JSON loader with lazy initialisation; zero changes to the public API (`t()`, `I18nService.get()`, `.get_all()`, `.add_translation()`)
- **Adding a new language requires no code changes** — drop a `xx.json` file into `backend/app/locales/` and it is picked up automatically on next startup
- **`I18nService.reload()`** — new method that reloads all locale files at runtime without a container restart; useful for live translation editing
- **`I18nService.available_languages()`** — returns the list of language codes currently loaded from disk

---

## [v1.1.6] - 2026-03-16

### 🖥️ LXC Terminal — xterm.js Console Fixed

- **Root cause identified and fixed** — Proxmox `termproxy` protocol requires a mandatory auth handshake: the client must send `"USERNAME:VNCTICKET\n"` as the very first WebSocket message; Proxmox responds with `"OK"` before starting the PTY. The backend handler was missing this step entirely, causing Proxmox to wait indefinitely and the browser to show "Disconnected".
- **Auth handshake added** — after connecting to the Proxmox `vncwebsocket` endpoint the backend now sends the auth message, validates the `"OK"` response, and only then starts the bidirectional proxy.
- **Initial terminal data forwarded** — any terminal data arriving in the same `"OK"` frame is immediately forwarded to the browser.
- **Graceful error handling** — auth timeout (>10 s) and explicit rejection close the WebSocket with a descriptive reason code instead of silently hanging.

### 🔧 Fixes

- Fixed `'ProxmoxServer' object has no attribute 'username'` — replaced `server.username` with correct `server.api_user` (with token suffix stripping)
- Fixed `CSRFPreventionToken` being sent in the request body — now sent only as an HTTP header
- Fixed `additional_headers` → `extra_headers` (websockets v12 API rename)
- Added URL-encoding for `vncticket` in the WebSocket URL (`urllib.parse.quote`)
- Added `PVEAuthCookie` header in the WebSocket upgrade request
- Added `subprotocols=["binary"]` required by Proxmox vncwebsocket

---

## [v1.1.5] - 2026-03-15

### 🗂️ Task Drawer — UPID Tracking & Clear Completed

- **UPID-based task tracking** — VM/LXC control methods (`start`, `stop`, `restart`) in `proxmox_client.py` now return the Proxmox task UPID instead of a boolean; each action automatically registers a `ProxmoxTask` entry for real-time Proxmox-side progress polling
- **`DELETE /proxmox/api/all-tasks/completed`** — new API endpoint: removes all `completed` / `failed` / `cancelled` tasks (both `ProxmoxTask` and `TaskQueue`) for the current user in one request
- **Clear button in Task Drawer** — trash icon appears in the drawer header whenever there are finished tasks; clicking it calls the new endpoint and updates the drawer instantly
- **Auto-expire old tasks in Drawer** — tasks older than 24 h are hidden from the Task Drawer on load and every 5 minutes; they remain accessible on the `/tasks` page
- **`openTaskDrawer()` global helper** — bulk VM operations now open the Task Drawer instead of showing a bottom progress toast, providing a unified task-tracking experience
- **Human-readable bulk task descriptions** — `TaskQueue.to_dict()` now maps internal `task_type` keys (`bulk_start`, `bulk_stop`, etc.) to localised Russian descriptions

### 🔧 Fixes

- **`pve` CLI nginx detection** — mode resolution now checks for the nginx config file (`nginx/conf.d/serverpanel.conf`) in addition to the running `serverpanel-nginx` container, fixing prod-mode detection on already-deployed hosts after container restarts

---

## [v1.1.4] - 2026-03-15

### 🗂️ Workspaces — Scoped Server Groups

- New **Workspace** entity: named group of Proxmox servers with scoped user access
- DB models: `Workspace`, `WorkspaceServer`, `WorkspaceUser` with automatic DB migration
- Full CRUD REST API (`/api/workspaces`, `/api/workspaces/{id}`) — admin-only create/update/delete, filtered list for regular users
- `X-Active-Workspace` request header propagated from frontend — filters server/VM/backup API responses to the active workspace
- Default workspace created on first run; all existing servers assigned to it
- Admin workspace management page (`/workspaces`) — create/edit/delete workspaces, assign servers and users
- **Sidebar workspace selector** — active workspace label shown in sidebar, switch without page reload
- `proxmox_server_detail.html` — dedicated server detail page (new template)
- i18n keys added for all workspace UI strings (EN + RU)

### 🔧 Fixes

- Fixed `PermissionChecker` import error in `dashboard.py`

---

## [v1.1.3] - 2026-03-10

### 🚨 Dashboard — Alerts Panel & Real-time Node Stats

- New API endpoint `GET /api/dashboard/alerts` — returns error/critical `AuditLog` entries for the last 24 h (up to 50), protected by `logs.view` permission
- Alerts modal (`openAlertsModal()`) on the dashboard status chip — click to view recent errors
- Mini sparkline charts (Chart.js) for **CPU, RAM, Storage** per node — updates in-place without re-rendering
- Node stats fetched in parallel via `Promise.all` for all online servers
- Full i18n coverage for new dashboard elements: `nodes_label`, `clusters_label`, `alerts_label`, `all_online`, `all_healthy`, `no_alerts`, `all_systems_operational`, `view_all`, `recent_audit_events`, `no_audit_events`, `last_24h`, `ram`, `ram_usage`, `storage`, `paused`, and more

### 🔒 Security — Log sanitisation

- Removed IP addresses from structured log messages in `servers.py` to prevent credential leakage via logs
- `logging_middleware.py` now suppresses noisy GET polling traffic — requests to `/proxmox/api/`, `/api/notifications/unread-count`, `/settings/api/panel` etc. are only logged on HTTP errors (≥ 400) or slow responses (> 5 s)

### 🎨 UI — CSS variables & GitHub stars

- `base.html`: GitHub stars counter switched from Gitea API to GitHub API (`api.github.com`, `stargazers_count`)
- Hard-coded `rgba()` colours in `backups.html` and `proxmox_vms.html` replaced with CSS theme variables (`var(--info-light)`, `var(--warning-light)`)
- Backups server selector redesigned — added icon and label for better clarity

### 📖 WIKI — Fixes & new content

- Fixed broken anchor links in Table of Contents (`#deployment-guide` → `#installation-and-deployment`)
- Added **Snapshots** section with creation guide, management table, and full API reference
- Added **Main Features / Dashboard** section
- Fixed broken emoji characters in section headings (`🛠️ pve CLI Tool`, `🔌 API Reference`)

---

## [v1.1.2] - 2026-03-08

### 🎨 Dashboard Redesign

- New 5-column stat grid: **Nodes, VMs, Containers, Clusters, Alerts**
- Real-time VM/CT count loaded via API on page open
- Resource cards row with mini sparkline charts: **CPU, RAM, Storage**
- Status chip in header: `All systems operational` / warnings / critical
- Header datetime display (live clock)
- Cluster count computed from DB (named clusters + standalone nodes)
- Alerts count sourced from `AuditLog` — errors + criticals in last 24 h
- Recent audit events (last 5) now passed to dashboard context

### 🖥️ Sidebar & Layout (`base.html`)

- **Cluster Selector** widget in sidebar — shows active cluster/standalone name
- **Version badge** in header (`v{{ version }}`)
- `page_subtitle` and `header_status` Jinja2 blocks for per-page overrides
- `loadSidebarCluster()` JS helper — fetches known servers, picks named cluster

### 🛠️ `pve` CLI Tool

- New `pve` shell script at project root — management shortcuts (logs, restart, update, exec, etc.)
- `deploy.sh --install-cli` — installs `pve` to `/usr/local/bin/pve`, patches `PVE_DIR` to project path
- Help visible after installation: `pve help`

### 🎨 Theme CSS

- New CSS classes for redesigned dashboard: `.stats-grid-5`, `.stat-card-v2`, `.resource-cards-row`, `.resource-card`, `.version-badge`, `.status-chip`, `.header-datetime`, `.sidebar-cluster-section`
- Minor CSS fixes across login, logs, tasks, os_templates, users, proxmox_vms, virtual_machines, ipam_dashboard

---

## [v1.1.1] - 2026-03-08

### 🔧 Fix: Update System — Host-side Rebuild

- **Root cause fixed**: `docker compose down` убивал cgroup контейнера вместе с `nohup`-скриптом внутри него — rebuild никогда не завершался, был виден только результат `git pull` (обновлённый `VERSION`)
- **Новый подход**: нажатие «Обновить панель» теперь записывает файл-триггер `.update_trigger` в корень проекта
- Host-side watchdog (`pvemanager-update.service` / `update_host.sh`) обнаруживает триггер и выполняет на хосте:
  `git pull → docker compose down → docker compose build --no-cache app → docker compose up -d`
- Watchdog работает как systemd-сервис на хосте — не зависит от жизни контейнера
- `PROJECT_DIR` определяется динамически в момент установки (не хардкодится)
- `User=` в systemd-юните совпадает с пользователем, запустившим `deploy.sh --watchdog`
- Установка / переустановка watchdog: `sudo ./deploy.sh --watchdog`
- Лог обновления: `./logs/update_host.log`

---

## [v1.1.0] - 2026-03-08

### 🚀 New Features

#### Backup Module
- New **Backups** page with 3 tabs: backup files, storages, scheduled jobs
- Proxmox Backup Server (PBS) support
- APScheduler-based backup job scheduler with cron expressions
- Backup retention policies (keep_last, keep_daily, keep_weekly, keep_monthly)
- Storage pool management (add, edit, delete)
- Task progress tracking via Proxmox UPID
- RBAC permissions for backup resources

#### IPAM Improvements
- Networks now bound to a specific **Proxmox server and node** — prevents cross-server IP assignment
- Manual server + network selection modal when linking IP to a VM/LXC
- Dynamic server and node dropdowns in network creation forms
- New `proxmox_node` field in IPAMNetwork model and schema
- Database migration #16

### 🔧 Fixes

- **Update system**: now correctly rebuilds Docker image after `git pull`
- Auto-detect current git branch (no more hardcoded `main`)
- Update logs saved to persistent `/app/logs/update.log` volume
- `docker-compose` package added to Dockerfile (was missing)

### 🎨 Theme

- All hardcoded colors (`#fff`, `rgba(...)`, `color: white`) replaced with CSS variables across all major templates

### 🌍 Localization

- Added Uzbek language keys for: `proxmox_node`, node selector, link-allocations modal

---

## [v1.0] - 2025-12-15

### 🎉 Initial Production Release

Production-ready version with complete feature set for Proxmox management.

---
