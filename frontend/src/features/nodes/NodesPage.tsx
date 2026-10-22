import { useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Server, Monitor, Container, Plus, Pencil, Trash2, Wifi, Loader2, Clock, Layers, LayoutGrid, ExternalLink, KeyRound, FingerprintPattern } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogClose } from '@/components/ui/dialog';
import { useServers, useCreateServer, useUpdateServer, useDeleteServer, useTestServerCredentials, useProvisionServerToken, useResetSshHostKey, useNodes } from '@/hooks/use-nodes';
import { useVirtualMachines } from '@/hooks/use-instances';
import type { ProxmoxServer, ProxmoxServerCreate } from '@/types';
import { formatUptime } from '@/lib/format';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { ServerStatusBadge } from '@/components/shared/ServerStatusBadge';
import ServerUpdatesBadge from './ServerUpdatesBadge';

/**
 * Сводка по узлу сервера: версия Proxmox, число нод в кластере и аптайм.
 * Тянет `/nodes` (кэш переиспользуется с ServerUpdatesBadge), поэтому
 * дополнительного запроса не создаёт.
 */
function ServerMeta({ serverId, online }: { serverId: number; online?: boolean }) {
  const { t } = useTranslation();
  const { data } = useNodes(online ? serverId : 0);

  if (!online) return null;
  const nodes = data?.nodes ?? [];
  if (nodes.length === 0) return null;

  // Аптайм показываем только для standalone-сервера (1 нода): для кластера
  // из нескольких нод единое число вводит в заблуждение — при перезагрузке
  // одной ноды отображался бы аптайм другой, ещё не перезагруженной.
  const uptime = nodes.length === 1 ? (nodes[0].uptime ?? 0) : 0;
  const version = data?.version;

  if (!version && nodes.length <= 1 && uptime <= 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {version && (
        <Badge variant="outline" className="gap-1 font-mono text-[11px]">
          <Layers className="h-3 w-3" />PVE {version}
        </Badge>
      )}
      {nodes.length > 1 && (
        <Badge variant="secondary" className="gap-1 text-[11px]">
          <Server className="h-3 w-3" />{t('nodes.cluster_nodes_count', { count: nodes.length })}
        </Badge>
      )}
      {uptime > 0 && (
        <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
          <Clock className="h-3 w-3" />{formatUptime(uptime)}
        </span>
      )}
    </div>
  );
}

/** Чипы рабочих областей, в которых состоит сервер. */
function WorkspaceChips({ server }: { server: ProxmoxServer }) {
  const { t } = useTranslation();
  const workspaces = server.workspaces ?? [];
  if (workspaces.length === 0) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <LayoutGrid className="h-3 w-3" />{t('nodes.no_workspace')}
      </span>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {workspaces.map(ws => (
        <span
          key={ws.id}
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] font-medium"
        >
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: ws.color || 'var(--muted-foreground)' }} />
          {ws.name}
        </span>
      ))}
    </div>
  );
}

/**
 * How the panel authenticates to Proxmox:
 * - auto     — log in with the password once, panel creates its own API token
 * - token    — an API token created by hand in the PVE UI
 * - password — password auth on every call (legacy)
 */
type AuthMode = 'auto' | 'token' | 'password';

interface ServerFormData {
  name: string;
  hostname: string;
  ip_address: string;
  port: string;
  api_user: string;
  auth_mode: AuthMode;
  api_token_name: string;
  api_token_value: string;
  password: string;
  verify_ssl: boolean;
  description: string;
}

const emptyForm: ServerFormData = {
  name: '',
  hostname: '',
  ip_address: '',
  port: '8006',
  api_user: 'root@pam',
  auth_mode: 'auto',
  api_token_name: '',
  api_token_value: '',
  password: '',
  verify_ssl: false,
  description: '',
};

function ServerFormDialog({
  open,
  onOpenChange,
  title,
  initialData,
  onSubmit,
  isPending,
  serverId,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  initialData: ServerFormData;
  onSubmit: (data: ProxmoxServerCreate) => void;
  isPending: boolean;
  serverId?: number;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<ServerFormData>(initialData);
  const testServer = useTestServerCredentials();

  // Refill the form each time the dialog opens (or gets other initial data).
  const [filledFor, setFilledFor] = useState({ open: false, initialData: null as ServerFormData | null });
  if (filledFor.open !== open || filledFor.initialData !== initialData) {
    setFilledFor({ open, initialData });
    if (open) setForm(initialData);
  }

  const set = (field: keyof ServerFormData, value: string | boolean) =>
    setForm(prev => ({ ...prev, [field]: value }));

  const buildPayload = (): ProxmoxServerCreate => {
    const payload: ProxmoxServerCreate = {
      name: form.name,
      hostname: form.hostname || form.ip_address,
      ip_address: form.ip_address,
      port: form.port ? Number(form.port) : 8006,
      api_user: form.api_user || 'root@pam',
      verify_ssl: form.verify_ssl,
      description: form.description || undefined,
    };
    // При редактировании поля учётных данных приходят в форму пустыми
    // (бэкенд не возвращает секреты), поэтому пустое значение здесь значит
    // "не менять" — отправлять его нельзя, иначе бэкенд затрёт реальный
    // токен/пароль пустой строкой и сервер уйдёт в offline.
    const isEdit = serverId != null;
    if (form.auth_mode === 'auto') {
      // Пароль уходит на бэкенд, тот подключается им один раз и создаёт токен.
      payload.auto_create_token = true;
      payload.password = form.password;
    } else if (form.auth_mode === 'password') {
      if (!isEdit || form.password) {
        payload.use_password = true;
        payload.password = form.password;
      }
    } else if (!isEdit || form.api_token_name || form.api_token_value) {
      payload.api_token_name = form.api_token_name;
      payload.api_token_value = form.api_token_value;
    }
    return payload;
  };

  const handleSubmit = () => {
    onSubmit(buildPayload());
  };

  const credentialsMissing =
    form.auth_mode === 'token'
      ? !form.api_token_name || !form.api_token_value
      : !form.password;

  const handleTest = () => {
    if (credentialsMissing) {
      toast.error(t('nodes.connection_failed'));
      return;
    }
    testServer.mutate(buildPayload(), {
      onSuccess: (res) => {
        if (res.success) toast.success(t('nodes.connection_ok'));
        else toast.error(t('nodes.connection_failed') + (res.message ? `: ${res.message}` : ''));
      },
      onError: (err) => toast.error(err.message),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2">
              <Label>{t('common.name')}</Label>
              <Input value={form.name} onChange={e => set('name', e.target.value)} className="mt-1" placeholder={t('common.my_proxmox_server')} />
            </div>
            <div>
              <Label>{t('nodes.hostname')}</Label>
              <Input value={form.ip_address} onChange={e => { set('ip_address', e.target.value); set('hostname', e.target.value); }} className="mt-1" placeholder="192.168.1.100" />
            </div>
            <div>
              <Label>{t('nodes.port')}</Label>
              <Input type="number" value={form.port} onChange={e => set('port', e.target.value)} className="mt-1" placeholder="8006" />
            </div>
            <div>
              <Label>{t('nodes.api_user')}</Label>
              <Input value={form.api_user} onChange={e => set('api_user', e.target.value)} className="mt-1" placeholder={t('common.placeholder_root_pam')} />
            </div>
          </div>

          <div>
            <Label>{t('nodes.auth_mode')}</Label>
            <select
              value={form.auth_mode}
              onChange={e => set('auth_mode', e.target.value as AuthMode)}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none"
            >
              {/* Перевыпуск токена для существующего сервера — отдельным
                  действием на карточке, а не через форму редактирования. */}
              {serverId == null && <option value="auto">{t('nodes.auth_mode_auto')}</option>}
              <option value="token">{t('nodes.auth_mode_token')}</option>
              <option value="password">{t('nodes.auth_mode_password')}</option>
            </select>
          </div>

          {form.auth_mode === 'token' ? (
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>{t('nodes.api_token_name')}</Label>
                <Input value={form.api_token_name} onChange={e => set('api_token_name', e.target.value)} className="mt-1" placeholder="mytoken" />
              </div>
              <div>
                <Label>{t('nodes.api_token_value')}</Label>
                <Input value={form.api_token_value} onChange={e => set('api_token_value', e.target.value)} className="mt-1" placeholder="xxxxxxxx-xxxx-..." />
              </div>
            </div>
          ) : (
            <div>
              <Label>{t('wizard.password')}</Label>
              <Input type="password" value={form.password} onChange={e => set('password', e.target.value)} className="mt-1" />
              {form.auth_mode === 'auto' && (
                <p className="mt-1 text-xs text-muted-foreground">{t('nodes.auth_mode_auto_hint')}</p>
              )}
            </div>
          )}

          <div className="flex items-center gap-2">
            <Checkbox
              id="verify_ssl"
              checked={form.verify_ssl}
              onChange={e => set('verify_ssl', e.target.checked)}
            />
            <Label htmlFor="verify_ssl" className="cursor-pointer">{t('nodes.verify_ssl')}</Label>
          </div>

          <div>
            <Label>{t('ipam.description')}</Label>
            <Input value={form.description} onChange={e => set('description', e.target.value)} className="mt-1" />
          </div>
        </div>
        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleTest}
            disabled={testServer.isPending || !form.ip_address}
          >
            {testServer.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wifi className="h-4 w-4 mr-1" />}
            {t('nodes.test_connection')}
          </Button>
          <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
          <Button onClick={handleSubmit} disabled={isPending || !form.name || !form.ip_address}>
            {isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function NodesPage() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data: servers = [], isLoading } = useServers();
  const { data: vms = [] } = useVirtualMachines();
  const createServer = useCreateServer();
  const updateServer = useUpdateServer();
  const deleteServer = useDeleteServer();
  const provisionToken = useProvisionServerToken();
  const resetHostKey = useResetSshHostKey();

  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editServerId, setEditServerId] = useState<number | null>(null);
  const [editInitial, setEditInitial] = useState<ServerFormData>(emptyForm);

  if (isLoading) {
    return <div className="flex items-center justify-center py-20 text-muted-foreground">{t('common.loading')}</div>;
  }

  const handleCreate = (data: ProxmoxServerCreate) => {
    createServer.mutate(data, {
      onSuccess: () => { toast.success(t('common.save')); setCreateOpen(false); },
      onError: (err) => toast.error(err.message),
    });
  };

  const handleEdit = (data: ProxmoxServerCreate) => {
    if (!editServerId) return;
    updateServer.mutate({ id: editServerId, ...data }, {
      onSuccess: () => { toast.success(t('common.save')); setEditOpen(false); },
      onError: (err) => toast.error(err.message),
    });
  };

  const handleDelete = async (id: number, name: string) => {
    if (!await confirm(`${t('common.confirm_delete')} "${name}"?`)) return;
    deleteServer.mutate(id, {
      onError: (err) => toast.error(err.message),
    });
  };

  const handleProvisionToken = async (id: number, name: string) => {
    if (!await confirm(t('nodes.switch_to_token_confirm', { name }), { title: t('nodes.switch_to_token') })) return;
    provisionToken.mutate(id, {
      onSuccess: (res) => toast.success(t('nodes.switch_to_token_ok', { token: res.token_name })),
      onError: (err) => toast.error(err.message),
    });
  };

  const handleResetHostKey = async (id: number, name: string) => {
    if (!await confirm(t('nodes.reset_ssh_host_key_confirm', { name }), { title: t('nodes.reset_ssh_host_key') })) return;
    resetHostKey.mutate(id, {
      onSuccess: (res) => toast.success(t('nodes.reset_ssh_host_key_ok', { count: res.removed })),
      onError: (err) => toast.error(err.message),
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t('nav.nodes')}</h1>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4 mr-1" />{t('nodes.add_server')}
        </Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {servers.map(srv => {
          const srvVMs = vms.filter(v => v.server_id === srv.id && v.type === 'qemu');
          const srvCTs = vms.filter(v => v.server_id === srv.id && v.type === 'lxc');
          return (
            <Card key={srv.id} className="relative group">
              <CardContent className="p-5 space-y-4">
                <div className="flex items-start justify-between">
                  <Link to={`/nodes/${srv.id}`} className="flex items-center gap-3 flex-1 min-w-0">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted shrink-0">
                      <Server className="h-5 w-5" />
                    </div>
                    <div className="min-w-0">
                      <p className="font-semibold truncate">{srv.name}</p>
                      <p className="text-xs text-muted-foreground font-mono truncate">{srv.ip_address}</p>
                    </div>
                  </Link>
                  <div className="flex items-center gap-1 ml-2 opacity-0 group-hover:opacity-100 transition-opacity">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      title={t('nodes.open_pve_ui')}
                      render={
                        <a
                          href={`https://${srv.ip_address}:${srv.port || 8006}/`}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                        />
                      }
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                    </Button>
                    {srv.use_password && (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        title={t('nodes.switch_to_token')}
                        disabled={provisionToken.isPending}
                        onClick={() => handleProvisionToken(srv.id, srv.name)}
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      title={t('nodes.reset_ssh_host_key')}
                      disabled={resetHostKey.isPending}
                      onClick={() => handleResetHostKey(srv.id, srv.name)}
                    >
                      <FingerprintPattern className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => {
                        setEditServerId(srv.id);
                        setEditInitial({
                          name: srv.name,
                          hostname: srv.hostname || srv.ip_address,
                          ip_address: srv.ip_address,
                          port: String(srv.port || 8006),
                          api_user: srv.api_user || 'root@pam',
                          auth_mode: srv.use_password ? 'password' : 'token',
                          api_token_name: '',
                          api_token_value: '',
                          password: '',
                          verify_ssl: srv.verify_ssl || false,
                          description: srv.description || '',
                        });
                        setEditOpen(true);
                      }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-destructive"
                      onClick={() => handleDelete(srv.id, srv.name)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4 text-sm text-muted-foreground">
                    <div className="flex items-center gap-1.5">
                      <Monitor className="h-3.5 w-3.5" />
                      <span className="font-mono tabular-nums">{srvVMs.length} VM</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Container className="h-3.5 w-3.5" />
                      <span className="font-mono tabular-nums">{srvCTs.length} LXC</span>
                    </div>
                  </div>
                  <ServerStatusBadge
                    isOnline={srv.is_online}
                    errorKind={srv.last_error_kind}
                    lastError={srv.last_error}
                  />
                </div>

                <ServerMeta serverId={srv.id} online={srv.is_online} />

                <div className="border-t border-border/60 pt-3">
                  <WorkspaceChips server={srv} />
                </div>

                {srv.description && (
                  <p className="text-xs text-muted-foreground truncate">{srv.description}</p>
                )}

                <ServerUpdatesBadge serverId={srv.id} online={srv.is_online} />
              </CardContent>
            </Card>
          );
        })}
      </div>

      {servers.length === 0 && (
        <div className="text-center py-16 text-muted-foreground">
          <Server className="mx-auto h-12 w-12 mb-3 opacity-50" />
          <p className="mb-4">{t('nodes.no_servers')}</p>
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4 mr-1" />{t('nodes.add_server')}
          </Button>
        </div>
      )}

      <ServerFormDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        title={t('nodes.add_server')}
        initialData={emptyForm}
        onSubmit={handleCreate}
        isPending={createServer.isPending}
      />

      <ServerFormDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        title={t('nodes.edit_server')}
        initialData={editInitial}
        onSubmit={handleEdit}
        isPending={updateServer.isPending}
        serverId={editServerId ?? undefined}
      />

    </div>
  );
}
