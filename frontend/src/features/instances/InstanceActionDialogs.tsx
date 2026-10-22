import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Disc, Eye, EyeOff, Sparkles, Copy, KeyRound, FileText, Terminal as TerminalIcon, Play, Square, RotateCcw, Power, AlertTriangle, ArrowRightLeft } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  useCloneInstance,
  useMigrateInstance,
  useRemoteMigrateInstance,
  useVMStatus,
  useReinstallInstance,
  useChangePassword,
  useUpdateNotes,
  useExecuteCommand,
  useNodeIsos,
  useAttachIso,
  useDetachIso,
  useVMConfig,
} from '@/hooks/use-instances';
import { useNodes, useServers, useNodeNetworks } from '@/hooks/use-nodes';
import { useBackupStorages, useCreateBackup } from '@/hooks/use-backups';
import { useLXCStorages } from '@/hooks/use-lxc-templates';
import { useUsers } from '@/hooks/use-users';
import { useProfile } from '@/hooks/use-settings';
import { toast } from 'sonner';
import { useDeployTasksStore } from '@/stores/deploy-tasks-store';

export type PowerAction = 'start' | 'stop' | 'restart' | 'shutdown';

export type InstanceAction =
  | 'clone'
  | 'migrate'
  | 'remote-migrate'
  | 'reinstall'
  | 'change-password'
  | 'notes'
  | 'execute'
  | 'iso'
  | 'backup'
  | null;

interface Props {
  open: InstanceAction;
  onOpenChange: (open: InstanceAction) => void;
  serverId: number;
  vmid: number;
  type: string; // 'qemu' | 'lxc'
  node: string;
  name?: string;
  description?: string;
}

export default function InstanceActionDialogs(props: Props) {
  const { open, onOpenChange, serverId, vmid, type, node, name, description } = props;
  const close = () => onOpenChange(null);
  const shared = { serverId, vmid, type, node, name, description };

  return (
    <>
      <CloneDialog open={open === 'clone'} onClose={close} {...shared} />
      <MigrateDialog open={open === 'migrate'} onClose={close} {...shared} />
      <RemoteMigrateDialog open={open === 'remote-migrate'} onClose={close} {...shared} />
      <ReinstallDialog open={open === 'reinstall'} onClose={close} {...shared} />
      <ChangePasswordDialog open={open === 'change-password'} onClose={close} {...shared} />
      <NotesDialog open={open === 'notes'} onClose={close} {...shared} />
      <ExecuteCommandDialog open={open === 'execute'} onClose={close} {...shared} />
      <IsoDialog open={open === 'iso'} onClose={close} {...shared} />
      <BackupDialog open={open === 'backup'} onClose={close} {...shared} />
    </>
  );
}

// ==================== Clone ====================

const CLONE_AUTO = '__auto__';

function CloneDialog({ open, onClose, serverId, vmid, type, node, name }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [newName, setNewName] = useState('');
  const [full, setFull] = useState(true);
  const [targetNode, setTargetNode] = useState('');
  const [targetStorage, setTargetStorage] = useState('');
  const [description, setDescription] = useState('');
  const [ownerId, setOwnerId] = useState<number | null>(null);
  const clone = useCloneInstance(serverId, vmid, type, node);
  const addDeployTask = useDeployTasksStore((s) => s.addTask);

  const { data: profile } = useProfile();
  const isAdmin = !!profile?.is_admin;
  const { data: allUsers = [] } = useUsers();
  const { data: nodesResp } = useNodes(serverId);
  const nodes = nodesResp?.nodes ?? [];
  const effectiveNode = targetNode || node;
  const { data: storages = [] } = useLXCStorages(serverId, effectiveNode);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setNewName('');
      setFull(true);
      setTargetNode('');
      setTargetStorage('');
      setDescription('');
      setOwnerId(null);
    }
  }

  const submit = () => {
    if (!newName.trim()) return;
    clone.mutate(
      {
        new_name: newName.trim(),
        full,
        target_node: targetNode || undefined,
        target_storage: targetStorage || undefined,
        description: description.trim() || undefined,
        owner_id: isAdmin && ownerId ? ownerId : undefined,
      },
      {
        onSuccess: (data) => {
          addDeployTask({
            id: data.task_id,
            name: data.name,
            status: 'pending',
            step: t('common.queued'),
            progress: 0,
            vmid: null,
            node: targetNode || node,
            error_message: null,
            kind: 'clone',
            server_id: serverId,
          });
          toast.success(t('instances.clone_started'));
          onClose();
        },
        onError: (e) => toast.error(e.message),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Copy className="h-4 w-4" /> {t('instances.clone_title')}</DialogTitle>
          <DialogDescription>{t('instances.clone_desc', { name: name ?? `#${vmid}` })}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="clone-name">{t('instances.clone_new_name')}</Label>
            <Input id="clone-name" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder={`${name || 'instance'}-clone`} />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('instances.clone_target_node')}</Label>
              <Select value={targetNode || CLONE_AUTO} onValueChange={(v) => { setTargetNode(v && v !== CLONE_AUTO ? v : ''); setTargetStorage(''); }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={CLONE_AUTO}>{t('instances.clone_same_node', { node })}</SelectItem>
                  {nodes.filter((n) => n.node !== node).map((n) => (
                    <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label>{t('instances.clone_storage')}</Label>
              <Select value={targetStorage || CLONE_AUTO} onValueChange={(v) => setTargetStorage(v && v !== CLONE_AUTO ? v : '')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={CLONE_AUTO}>{t('instances.clone_same_storage')}</SelectItem>
                  {storages.map((s) => (
                    <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {isAdmin && (
            <div className="space-y-1.5">
              <Label>{t('instances.clone_owner')}</Label>
              <Select value={ownerId ? String(ownerId) : CLONE_AUTO} onValueChange={(v) => setOwnerId(v === CLONE_AUTO ? null : Number(v))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={CLONE_AUTO}>{t('instances.clone_owner_me', { username: profile?.username })}</SelectItem>
                  {allUsers.map((u) => (
                    <SelectItem key={u.id} value={String(u.id)}>
                      {u.username}{u.full_name ? ` (${u.full_name})` : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="space-y-1.5">
            <Label htmlFor="clone-desc">{t('instances.clone_desc_label')} <span className="text-muted-foreground">({t('common.optional')})</span></Label>
            <Input id="clone-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t('instances.clone_desc_placeholder')} />
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={full} onChange={(e) => setFull(e.target.checked)} />
            <span>{t('instances.clone_full')}</span>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!newName.trim() || clone.isPending}>
            {clone.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.clone_btn')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Migrate ====================

const MIGRATE_SAME_STORAGE = '__same__';

function MigrateDialog({ open, onClose, serverId, vmid, type, node, name }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [targetNode, setTargetNode] = useState('');
  const [targetStorage, setTargetStorage] = useState('');
  const [online, setOnline] = useState(false);
  const migrate = useMigrateInstance(serverId, vmid, type, node);
  const addDeployTask = useDeployTasksStore((s) => s.addTask);

  const { data: nodesResp } = useNodes(serverId);
  const nodes = (nodesResp?.nodes ?? []).filter((n) => n.node !== node);
  const { data: status } = useVMStatus(serverId, vmid, type, node, open);
  const isRunning = status?.status === 'running';
  const { data: storages = [] } = useLXCStorages(serverId, targetNode || node);

  // Fresh form on every opening; "online" follows the guest's state, which
  // may only arrive after the dialog opened.
  const [filledFor, setFilledFor] = useState({ open: false, isRunning: false });
  if (filledFor.open !== open || filledFor.isRunning !== isRunning) {
    setFilledFor({ open, isRunning });
    if (open && !filledFor.open) {
      setTargetNode('');
      setTargetStorage('');
    }
    if (open) setOnline(isRunning);
  }

  const submit = () => {
    if (!targetNode) return;
    migrate.mutate(
      {
        target_node: targetNode,
        target_storage: targetStorage || undefined,
        online,
      },
      {
        onSuccess: (data) => {
          addDeployTask({
            id: data.task_id,
            name: data.name,
            status: 'pending',
            step: t('common.queued'),
            progress: 0,
            vmid,
            node: targetNode,
            error_message: null,
            kind: 'migrate',
            server_id: serverId,
          });
          toast.success(t('instances.migrate_started'));
          onClose();
        },
        onError: (e) => toast.error(e.message),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><ArrowRightLeft className="h-4 w-4" /> {t('instances.migrate')}</DialogTitle>
          <DialogDescription>{t('instances.migrate_desc')} <strong>{name ?? `#${vmid}`}</strong> ({node})</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>{t('instances.migrate_target_node')}</Label>
            <Select value={targetNode} onValueChange={(v) => { if (v) { setTargetNode(v); setTargetStorage(''); } }}>
              <SelectTrigger><SelectValue placeholder={t('instances.migrate_select_node')} /></SelectTrigger>
              <SelectContent>
                {nodes.map((n) => (
                  <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            {nodes.length === 0 && (
              <p className="text-xs text-warning">{t('instances.migrate_no_targets')}</p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label>{t('instances.migrate_target_storage')}</Label>
            <Select value={targetStorage || MIGRATE_SAME_STORAGE} onValueChange={(v) => setTargetStorage(!v || v === MIGRATE_SAME_STORAGE ? '' : v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={MIGRATE_SAME_STORAGE}>{t('instances.migrate_same_storage')}</SelectItem>
                {storages.map((s) => (
                  <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <label className="flex items-center gap-2.5 cursor-pointer">
            <Checkbox checked={online} onChange={(e) => setOnline(e.target.checked)} />
            <span className="text-sm">{t('instances.migrate_online')}</span>
          </label>
          <p className="text-xs text-muted-foreground -mt-2">
            {isRunning ? t('instances.migrate_online_hint_running') : t('instances.migrate_online_hint_stopped')}
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!targetNode || migrate.isPending}>
            {migrate.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.migrate')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Remote Migrate (to a different cluster) ====================

const REMOTE_MIGRATE_SAME = '__same__';

function RemoteMigrateDialog({ open, onClose, serverId, vmid, type, node, name }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [targetServerId, setTargetServerId] = useState<number | null>(null);
  const [targetNode, setTargetNode] = useState('');
  const [targetStorage, setTargetStorage] = useState('');
  const [targetBridge, setTargetBridge] = useState('');
  const [online, setOnline] = useState(false);
  const [deleteSource, setDeleteSource] = useState(true);
  const migrate = useRemoteMigrateInstance(serverId, vmid, type, node);
  const addDeployTask = useDeployTasksStore((s) => s.addTask);

  const { data: allServers = [] } = useServers();
  const targetServers = allServers.filter((s) => s.id !== serverId && !s.use_password);

  const { data: nodesResp } = useNodes(targetServerId ?? 0);
  const nodes = nodesResp?.nodes ?? [];
  const { data: status } = useVMStatus(serverId, vmid, type, node, open);
  const isRunning = status?.status === 'running';
  const { data: storages = [] } = useLXCStorages(targetServerId ?? undefined, targetNode || undefined);
  const { data: networksResp } = useNodeNetworks(targetServerId ?? 0, targetNode);
  const bridges = (networksResp?.interfaces ?? []).filter((i) => i.type === 'bridge');

  // Fresh form on every opening; "online" follows the guest's state, which
  // may only arrive after the dialog opened.
  const [filledFor, setFilledFor] = useState({ open: false, isRunning: false });
  if (filledFor.open !== open || filledFor.isRunning !== isRunning) {
    setFilledFor({ open, isRunning });
    if (open && !filledFor.open) {
      setTargetServerId(null);
      setTargetNode('');
      setTargetStorage('');
      setTargetBridge('');
      setDeleteSource(true);
    }
    if (open) setOnline(isRunning);
  }

  const submit = () => {
    if (!targetServerId || !targetNode) return;
    migrate.mutate(
      {
        target_server_id: targetServerId,
        target_node: targetNode,
        target_storage: targetStorage || undefined,
        target_bridge: targetBridge || undefined,
        online,
        delete_source: deleteSource,
      },
      {
        onSuccess: (data) => {
          addDeployTask({
            id: data.task_id,
            name: data.name,
            status: 'pending',
            step: t('common.queued'),
            progress: 0,
            vmid,
            node: targetNode,
            error_message: null,
            kind: 'remote_migrate',
            server_id: serverId,
          });
          toast.success(t('instances.remote_migrate_started'));
          onClose();
        },
        onError: (e) => toast.error(e.message),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ArrowRightLeft className="h-4 w-4" /> {t('instances.remote_migrate')}
          </DialogTitle>
          <DialogDescription>
            {t('instances.remote_migrate_desc')} <strong>{name ?? `#${vmid}`}</strong>
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>{t('instances.remote_migrate_target_server')}</Label>
            <Select
              value={targetServerId ? String(targetServerId) : ''}
              onValueChange={(v) => { if (v) { setTargetServerId(Number(v)); setTargetNode(''); setTargetStorage(''); setTargetBridge(''); } }}
            >
              <SelectTrigger><SelectValue placeholder={t('instances.remote_migrate_select_server')} /></SelectTrigger>
              <SelectContent>
                {targetServers.map((s) => (
                  <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            {targetServers.length === 0 && (
              <p className="text-xs text-warning">
                {t('instances.remote_migrate_no_targets')}
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label>{t('instances.migrate_target_node')}</Label>
            <Select value={targetNode} onValueChange={(v) => { if (v) { setTargetNode(v); setTargetStorage(''); setTargetBridge(''); } }}>
              <SelectTrigger><SelectValue placeholder={t('instances.migrate_select_node')} /></SelectTrigger>
              <SelectContent>
                {nodes.map((n) => (
                  <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('instances.migrate_target_storage')}</Label>
              <Select value={targetStorage || REMOTE_MIGRATE_SAME} onValueChange={(v) => setTargetStorage(!v || v === REMOTE_MIGRATE_SAME ? '' : v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={REMOTE_MIGRATE_SAME}>{t('instances.migrate_same_storage')}</SelectItem>
                  {storages.map((s) => (
                    <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>{t('instances.remote_migrate_target_bridge')}</Label>
              <Select value={targetBridge || REMOTE_MIGRATE_SAME} onValueChange={(v) => setTargetBridge(!v || v === REMOTE_MIGRATE_SAME ? '' : v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={REMOTE_MIGRATE_SAME}>{t('instances.remote_migrate_same_bridge')}</SelectItem>
                  {bridges.map((b) => (
                    <SelectItem key={b.iface} value={b.iface}>{b.iface}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <label className="flex items-center gap-2.5 cursor-pointer">
            <Checkbox checked={online} onChange={(e) => setOnline(e.target.checked)} />
            <span className="text-sm">{t('instances.migrate_online')}</span>
          </label>
          <p className="text-xs text-muted-foreground -mt-2">
            {isRunning ? t('instances.migrate_online_hint_running') : t('instances.migrate_online_hint_stopped')}
          </p>

          <label className="flex items-center gap-2.5 cursor-pointer">
            <Checkbox checked={deleteSource} onChange={(e) => setDeleteSource(e.target.checked)} />
            <span className="text-sm">{t('instances.remote_migrate_delete_source')}</span>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!targetServerId || !targetNode || migrate.isPending}>
            {migrate.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.remote_migrate')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Reinstall ====================

function ReinstallDialog({ open, onClose, serverId, vmid, node, name, type }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [confirm, setConfirm] = useState('');
  const reinstall = useReinstallInstance(serverId, vmid, node);
  const addDeployTask = useDeployTasksStore((s) => s.addTask);
  const expected = name || String(vmid);

  const submit = () => {
    if (confirm !== expected) return;
    reinstall.mutate(undefined, {
      onSuccess: (data) => {
        addDeployTask({
          id: data.task_id,
          name: data.name || expected,
          status: 'pending',
          step: t('common.queued'),
          progress: 0,
          vmid,
          node,
          error_message: null,
          kind: 'reinstall',
          server_id: serverId,
        });
        toast.success(t('instances.reinstall_started'));
        setConfirm('');
        onClose();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Sparkles className="h-4 w-4" /> {t('instances.reinstall_title')}</DialogTitle>
          <DialogDescription>
            {t('instances.reinstall_desc', { name: expected })}
            {type === 'lxc' && (
              <span className="mt-2 block text-warning">{t('instances.reinstall_lxc_warn')}</span>
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>{t('instances.reinstall_confirm_label')} <strong className="text-foreground">{expected}</strong></Label>
            <Input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={expected} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="destructive" onClick={submit} disabled={confirm !== expected || reinstall.isPending}>
            {reinstall.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.reinstall_btn')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Change Password ====================

function ChangePasswordDialog({ open, onClose, serverId, vmid, type, node, name }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [username, setUsername] = useState('root');
  const [password, setPassword] = useState('');
  const [show, setShow] = useState(false);
  const mut = useChangePassword(serverId, vmid, type, node);
  const addDeployTask = useDeployTasksStore((s) => s.addTask);

  const submit = () => {
    if (!password || password.length < 4) {
      toast.error('Password must be at least 4 chars');
      return;
    }
    mut.mutate(
      { username, password },
      {
        onSuccess: (data) => {
          addDeployTask({
            id: data.task_id,
            name: data.name || (name || `#${vmid}`),
            status: 'pending',
            step: t('common.queued'),
            progress: 0,
            vmid,
            node,
            error_message: null,
            kind: 'change_password',
            server_id: serverId,
          });
          toast.success(t('instances.password_started'));
          setPassword('');
          onClose();
        },
        onError: (e) => toast.error(e.message),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4" /> {t('instances.change_password_title')}</DialogTitle>
          <DialogDescription>
            {type === 'qemu'
              ? t('instances.change_password_qemu_hint')
              : t('instances.change_password_lxc_hint')}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>{t('instances.change_password_user')}</Label>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder={t('common.placeholder_root')} />
          </div>
          <div>
            <Label>{t('instances.change_password_new')}</Label>
            <div className="relative">
              <Input
                type={show ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="absolute right-1 top-1 h-7 w-7"
                onClick={() => setShow((s) => !s)}
              >
                {show ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
              </Button>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!password || mut.isPending}>
            {mut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.change_password_btn')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Notes ====================

function NotesDialog({ open, onClose, serverId, vmid, type, node, description }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const config = useVMConfig(serverId, vmid, type, node, open);
  const remote = (config.data?.description as string | undefined) ?? description ?? '';
  // Until the user types, the field mirrors the stored notes (which may load
  // after the dialog opened); closing the dialog discards the edit.
  const [edited, setEdited] = useState('');
  const [touched, setTouched] = useState(false);
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (!open) setTouched(false);
  }
  const value = touched ? edited : remote;
  const setValue = setEdited;

  const mut = useUpdateNotes(serverId, vmid, type, node);

  const submit = () => {
    mut.mutate(
      { description: value },
      {
        onSuccess: () => {
          toast.success('Notes saved');
          setTouched(false);
          onClose();
        },
        onError: (e) => toast.error(e.message),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><FileText className="h-4 w-4" /> {t('instances.notes_title')}</DialogTitle>
          <DialogDescription>{t('instances.notes_desc')}</DialogDescription>
        </DialogHeader>
        <textarea
          value={value}
          onChange={(e) => { setTouched(true); setValue(e.target.value); }}
          rows={8}
          className="min-h-32 w-full rounded-md border bg-background p-2 text-sm font-mono outline-none focus:ring-1 focus:ring-ring"
          placeholder={t('instances.notes_placeholder')}
        />
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={mut.isPending}>
            {mut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Execute Command ====================

function ExecuteCommandDialog({ open, onClose, serverId, vmid, node, type }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [command, setCommand] = useState('');
  const [timeout, setTimeoutVal] = useState(30);
  const exec = useExecuteCommand(serverId, vmid);
  const result = exec.data;

  const submit = () => {
    if (!command.trim()) return;
    exec.mutate({ node, command, timeout });
  };

  if (type !== 'qemu') {
    return (
      <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('instances.exec_unavailable_title')}</DialogTitle>
            <DialogDescription>{t('instances.exec_unavailable_desc')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={onClose}>OK</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><TerminalIcon className="h-4 w-4" /> {t('instances.exec_title')}</DialogTitle>
          <DialogDescription>{t('instances.exec_desc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-[1fr_120px]">
            <div>
              <Label>{t('instances.exec_command')}</Label>
              <Input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder={t('common.placeholder_command')}
                onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
              />
            </div>
            <div>
              <Label>{t('instances.exec_timeout')}</Label>
              <Input
                type="number"
                min={1}
                max={300}
                value={timeout}
                onChange={(e) => setTimeoutVal(Number(e.target.value) || 30)}
              />
            </div>
          </div>
          {result && (
            <div className="space-y-2">
              <div className="text-xs">
                exit_code: <span className={result.exit_code === 0 ? 'text-success' : 'text-danger'}>{result.exit_code}</span>
              </div>
              {result.stdout && (
                <div>
                  <p className="mb-1 text-xs text-muted-foreground">stdout</p>
                  <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs font-mono">{result.stdout}</pre>
                </div>
              )}
              {result.stderr && (
                <div>
                  <p className="mb-1 text-xs text-muted-foreground">stderr</p>
                  <pre className="max-h-32 overflow-auto rounded-md bg-danger/10 p-2 text-xs font-mono text-danger">{result.stderr}</pre>
                </div>
              )}
              {result.error && <p className="text-xs text-danger">{result.error}</p>}
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.close')}</Button>
          <Button onClick={submit} disabled={!command.trim() || exec.isPending}>
            {exec.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.exec_run')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== ISO mount/unmount ====================

function IsoDialog({ open, onClose, serverId, vmid, node, type }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [device, setDevice] = useState('ide2');
  const [volid, setVolid] = useState<string>('');
  const [bootFromDisk, setBootFromDisk] = useState(true);
  const [bootFromIso, setBootFromIso] = useState(false);
  const isQemu = type === 'qemu';
  const isos = useNodeIsos(serverId, node, open && isQemu);
  const config = useVMConfig(serverId, vmid, type, node, open && isQemu);
  const attach = useAttachIso(serverId, vmid, node);
  const detach = useDetachIso(serverId, vmid, node);

  const currentVolid = useMemo(() => {
    const v = config.data?.[device];
    if (typeof v !== 'string' || v.startsWith('none')) return null;
    return v.split(',')[0] || null;
  }, [config.data, device]);

  // Preselect the ISO mounted on the chosen device whenever the dialog opens,
  // the device changes or the config (re)loads.
  const [filledFor, setFilledFor] = useState({ open: false, device: '', currentVolid: null as string | null });
  if (filledFor.open !== open || filledFor.device !== device || filledFor.currentVolid !== currentVolid) {
    setFilledFor({ open, device, currentVolid });
    if (open) setVolid(currentVolid || '');
  }

  if (!isQemu) {
    return (
      <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('instances.iso_lxc_title')}</DialogTitle>
            <DialogDescription>{t('instances.iso_lxc_desc')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={onClose}>OK</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Disc className="h-4 w-4" /> {t('instances.iso_title')}</DialogTitle>
          <DialogDescription>{t('instances.iso_desc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>{t('instances.iso_device')}</Label>
            <Select value={device} onValueChange={(v) => v && setDevice(v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="ide0">ide0</SelectItem>
                <SelectItem value="ide1">ide1</SelectItem>
                <SelectItem value="ide2">ide2</SelectItem>
                <SelectItem value="ide3">ide3</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label>{t('instances.iso_image')}</Label>
            <Select value={volid} onValueChange={(v) => v && setVolid(v)}>
              <SelectTrigger>
                <SelectValue placeholder={isos.isLoading ? t('common.loading') : t('instances.iso_select')} />
              </SelectTrigger>
              <SelectContent>
                {(isos.data?.isos || []).map((iso) => (
                  <SelectItem key={iso.volid} value={iso.volid}>
                    {iso.name || iso.volid}{iso.volid === currentVolid ? ` · ${t('instances.iso_current')}` : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {isos.data?.isos?.length === 0 && (
              <p className="mt-1 text-xs text-muted-foreground">{t('instances.iso_no_isos')}</p>
            )}
          </div>
          {currentVolid && (
            <label className="flex items-start gap-2 text-sm cursor-pointer">
              <Checkbox
                className="mt-0.5"
                checked={bootFromDisk}
                onChange={(e) => setBootFromDisk(e.target.checked)}
              />
              <span>
                {t('instances.iso_boot_disk')}
                <span className="block text-xs text-muted-foreground">
                  {t('instances.iso_boot_disk_hint')}
                </span>
              </span>
            </label>
          )}
          {volid && volid !== currentVolid && (
            <label className="flex items-start gap-2 text-sm cursor-pointer">
              <Checkbox
                className="mt-0.5"
                checked={bootFromIso}
                onChange={(e) => setBootFromIso(e.target.checked)}
              />
              <span>
                {t('instances.iso_boot_iso')}
                <span className="block text-xs text-muted-foreground">
                  {t('instances.iso_boot_iso_hint')}
                </span>
              </span>
            </label>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => detach.mutate({ device, boot_from_disk: bootFromDisk, reboot_after: bootFromDisk }, {
              onSuccess: (r) => {
                toast.success(
                  r.rebooted ? t('instances.iso_ejected_rebooting')
                    : r.boot_from_disk ? t('instances.iso_ejected_disk')
                    : t('instances.iso_ejected'),
                );
                onClose();
              },
              onError: (e) => toast.error(e.message),
            })}
            disabled={detach.isPending || !currentVolid}
          >
            {detach.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.iso_eject')}
          </Button>
          <Button
            onClick={() => attach.mutate({ volid, device, boot_from_iso: bootFromIso, reboot_after: bootFromIso }, {
              onSuccess: (r) => {
                toast.success(
                  r.rebooted ? t('instances.iso_attached_rebooting')
                    : r.boot_from_iso ? t('instances.iso_attached_booting')
                    : t('instances.iso_attached'),
                );
                onClose();
              },
              onError: (e) => toast.error(e.message),
            })}
            disabled={!volid || attach.isPending}
          >
            {attach.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.iso_attach')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Backup Now ====================
function BackupDialog({ open, onClose, serverId, vmid, type, node, name }: Omit<Props, 'open' | 'onOpenChange'> & { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { data: storagesData } = useBackupStorages(open ? serverId : 0);
  const createBackup = useCreateBackup();
  const [storagePick, setStorage] = useState('');
  const [mode, setMode] = useState('snapshot');
  const [compress, setCompress] = useState('zstd');

  const storages = useMemo(() => {
    const list = (storagesData?.storages || []) as { storage: string; type: string; content?: string; nodes?: string }[];
    return list
      .filter((s) => (s.content || '').includes('backup'))
      .filter((s) => !s.nodes || s.nodes.split(',').includes(node));
  }, [storagesData, node]);
  // The picked storage, or the first suitable one.
  const storage = storages.some((s) => s.storage === storagePick) ? storagePick : (storages[0]?.storage ?? '');

  const submit = () => {
    if (!storage) return;
    createBackup.mutate(
      { server_id: serverId, node, vmid, storage, mode, compress },
      {
        onSuccess: () => {
          toast.success(t('instances.backup_started'));
          onClose();
        },
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t('instances.backup_now')}</DialogTitle>
          <DialogDescription>
            {name || `${type === 'qemu' ? 'VM' : 'CT'} ${vmid}`} · {node}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>{t('backups.storage')}</Label>
            <Select value={storage} onValueChange={(v) => setStorage(v ?? '')}>
              <SelectTrigger>
                <SelectValue placeholder={t('backups.select_storage')} />
              </SelectTrigger>
              <SelectContent>
                {storages.map((s) => (
                  <SelectItem key={s.storage} value={s.storage}>
                    {s.storage} <span className="text-muted-foreground">({s.type})</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {storages.length === 0 && (
              <p className="text-xs text-warning">
                {t('instances.backup_no_storages')}
              </p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label>{t('backups.mode')}</Label>
              <Select value={mode} onValueChange={(v) => setMode(v ?? '')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="snapshot">Snapshot</SelectItem>
                  <SelectItem value="suspend">Suspend</SelectItem>
                  <SelectItem value="stop">Stop</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>{t('backups.compression')}</Label>
              <Select value={compress} onValueChange={(v) => setCompress(v ?? '')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="zstd">ZSTD</SelectItem>
                  <SelectItem value="gzip">GZIP</SelectItem>
                  <SelectItem value="lzo">LZO</SelectItem>
                  <SelectItem value="0">{t('backups.no_compression')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!storage || createBackup.isPending}>
            {createBackup.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.backup_create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Power Confirm ====================

type ActionConfig = {
  icon: React.ReactNode;
  titleKey: string;
  descKey: string;
  btnKey: string;
  btnVariant: 'default' | 'outline' | 'destructive';
  btnClass: string;
};

const POWER_ACTION_CONFIG: Record<PowerAction, ActionConfig> = {
  start: {
    icon: <Play className="h-4 w-4 text-success" />,
    titleKey: 'instances.power_start',
    descKey: 'instances.power_start_desc',
    btnKey: 'instances.power_start',
    btnVariant: 'default',
    btnClass: 'bg-success text-success-foreground hover:bg-success/90 border-0',
  },
  restart: {
    icon: <RotateCcw className="h-4 w-4 text-warning" />,
    titleKey: 'instances.power_restart',
    descKey: 'instances.power_restart_desc',
    btnKey: 'instances.power_restart',
    btnVariant: 'outline',
    btnClass: 'border-warning text-warning hover:bg-warning/10',
  },
  shutdown: {
    icon: <Power className="h-4 w-4 text-orange-500" />,
    titleKey: 'instances.power_shutdown',
    descKey: 'instances.power_shutdown_desc',
    btnKey: 'instances.power_shutdown',
    btnVariant: 'outline',
    btnClass: 'border-orange-500 text-orange-500 hover:bg-orange-500/10',
  },
  stop: {
    icon: <Square className="h-4 w-4" />,
    titleKey: 'instances.power_stop',
    descKey: 'instances.power_stop_desc',
    btnKey: 'instances.power_stop',
    btnVariant: 'destructive',
    btnClass: '',
  },
};

export function PowerConfirmDialog({
  open,
  onClose,
  onConfirm,
  action,
  vmName,
  isPending,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  action: PowerAction | null;
  vmName?: string;
  isPending?: boolean;
}) {
  const { t } = useTranslation();
  if (!action) return null;
  const cfg = POWER_ACTION_CONFIG[action];
  const displayName = vmName ? `«${vmName}»` : t('instances.power_unnamed');

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {cfg.icon}
            {t(cfg.titleKey)}
          </DialogTitle>
          <DialogDescription>
            {t(cfg.descKey)} {displayName}?
            {action === 'stop' && (
              <span className="mt-2 flex items-center gap-1.5 text-destructive">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                {t('instances.power_stop_warn')}
              </span>
            )}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={isPending}>
            {t('common.cancel')}
          </Button>
          <Button
            variant={cfg.btnVariant}
            className={cfg.btnClass || undefined}
            onClick={onConfirm}
            disabled={isPending}
          >
            {isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t(cfg.btnKey)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
