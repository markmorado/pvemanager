import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Pencil, Trash2, Database, CalendarClock, Power, PowerOff, AlertTriangle, Search } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useServers } from '@/hooks/use-nodes';
import {
  useBackupStorages, useProxmoxBackupJobs,
  useCreateStorage, useUpdateStorage, useDeleteStorage,
  useCreateProxmoxJob, useUpdateProxmoxJob, useDeleteProxmoxJob,
  useScanNfsExports,
  type ProxmoxJobInput,
} from '@/hooks/use-backups';
import { formatBytes } from '@/lib/format';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';

const STORAGE_TYPES = ['dir', 'nfs', 'cifs', 'pbs', 'lvm', 'lvmthin', 'zfspool', 'rbd', 'cephfs'];

/** Разумный content по умолчанию для типа хранилища. */
const DEFAULT_CONTENT: Record<string, string> = {
  dir: 'images,iso,vztmpl,backup',
  nfs: 'images,iso,vztmpl,backup',
  cifs: 'images,iso,vztmpl,backup',
  pbs: 'backup',
  lvm: 'images,rootdir',
  lvmthin: 'images,rootdir',
  zfspool: 'images,rootdir',
  rbd: 'images,rootdir',
  cephfs: 'backup,iso,vztmpl',
};

interface StorageRow {
  storage: string;
  type: string;
  content?: string;
  nodes?: string;
  path?: string;
  server?: string;
  export?: string;
  share?: string;
  datastore?: string;
  disable?: number;
  used?: number;
  total?: number;
  avail?: number;
  /** Ноды, где хранилище объявлено, но недоступно (нет VG/пула, отвалился mount). */
  inactive_nodes?: string[];
}

interface StorageForm {
  storage: string;
  type: string;
  content: string;
  nodes: string;
  path: string;
  server: string;
  export: string;
  share: string;
  datastore: string;
  username: string;
  password: string;
  fingerprint: string;
  vgname: string;    // lvm / lvmthin — volume group
  thinpool: string;  // lvmthin — thin pool
  pool: string;      // zfspool — пул/датасет; rbd — ceph pool
  monhost: string;   // rbd / cephfs (внешний ceph)
  fsname: string;    // cephfs — имя файловой системы
  sparse: boolean;   // zfspool — тонкое выделение
}

const emptyForm: StorageForm = {
  storage: '', type: 'dir', content: 'images,iso,vztmpl,backup', nodes: '',
  path: '', server: '', export: '', share: '', datastore: '',
  username: '', password: '', fingerprint: '',
  vgname: '', thinpool: '', pool: '', monhost: '', fsname: '', sparse: false,
};

function StorageDialog({ serverId, open, onOpenChange, editing }: {
  serverId: number; open: boolean; onOpenChange: (v: boolean) => void; editing: StorageRow | null;
}) {
  const { t } = useTranslation();
  const createStorage = useCreateStorage(serverId);
  const updateStorage = useUpdateStorage(serverId);
  const scanNfs = useScanNfsExports(serverId);
  const [nfsExports, setNfsExports] = useState<string[]>([]);
  const [form, setForm] = useState<StorageForm>(() => editing ? {
    ...emptyForm,
    storage: editing.storage,
    type: editing.type,
    content: editing.content || 'backup',
    nodes: editing.nodes || '',
    path: editing.path || '',
    server: editing.server || '',
    export: editing.export || '',
    share: editing.share || '',
    datastore: editing.datastore || '',
  } : emptyForm);
  const set = <K extends keyof StorageForm>(k: K, v: StorageForm[K]) => setForm(p => ({ ...p, [k]: v }));

  const handleScanNfs = () => {
    if (!form.server.trim()) { toast.error(t('storages.server_required')); return; }
    setNfsExports([]);
    scanNfs.mutate(form.server.trim(), {
      onSuccess: (data) => {
        const paths = (data.exports ?? []).map(e => e.path).filter(Boolean);
        setNfsExports(paths);
        if (paths.length === 0) toast.error(t('storages.no_exports_found'));
        else if (!paths.includes(form.export)) set('export', paths[0]);
      },
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const submit = () => {
    if (!editing && !form.storage.trim()) { toast.error(t('storages.id_required')); return; }
    const done = (msg: string) => { toast.success(msg); onOpenChange(false); };

    if (editing) {
      // Only a subset is updatable; id and type are immutable in PVE.
      const payload: Record<string, unknown> = { content: form.content.trim() || 'backup' };
      if (form.nodes.trim()) payload.nodes = form.nodes.trim();
      updateStorage.mutate({ storage: editing.storage, ...payload }, {
        onSuccess: () => done(t('storages.saved')),
        onError: (e: Error) => toast.error(e.message),
      });
      return;
    }

    const payload: Record<string, unknown> = {
      storage: form.storage.trim(),
      type: form.type,
      content: form.content.trim() || 'backup',
    };
    if (form.nodes.trim()) payload.nodes = form.nodes.trim();
    if (form.type === 'dir') payload.path = form.path.trim();
    if (form.type === 'nfs') { payload.server = form.server.trim(); payload.export = form.export.trim(); }
    if (form.type === 'cifs') {
      payload.server = form.server.trim(); payload.share = form.share.trim();
      if (form.username.trim()) payload.username = form.username.trim();
      if (form.password) payload.password = form.password;
    }
    if (form.type === 'pbs') {
      payload.server = form.server.trim(); payload.datastore = form.datastore.trim();
      if (form.username.trim()) payload.username = form.username.trim();
      if (form.password) payload.password = form.password;
      if (form.fingerprint.trim()) payload.fingerprint = form.fingerprint.trim();
    }
    if (form.type === 'lvm') payload.vgname = form.vgname.trim();
    if (form.type === 'lvmthin') { payload.vgname = form.vgname.trim(); payload.thinpool = form.thinpool.trim(); }
    if (form.type === 'zfspool') { payload.pool = form.pool.trim(); if (form.sparse) payload.sparse = 1; }
    if (form.type === 'rbd') {
      payload.pool = form.pool.trim();
      if (form.monhost.trim()) payload.monhost = form.monhost.trim();
      if (form.username.trim()) payload.username = form.username.trim();
    }
    if (form.type === 'cephfs') {
      if (form.fsname.trim()) payload['fs-name'] = form.fsname.trim();
      if (form.monhost.trim()) payload.monhost = form.monhost.trim();
      if (form.username.trim()) payload.username = form.username.trim();
    }
    createStorage.mutate(payload, {
      onSuccess: () => done(t('storages.created')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const pending = createStorage.isPending || updateStorage.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>{editing ? `${t('storages.edit')} ${editing.storage}` : t('storages.add')}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>{t('storages.id')}</Label>
              <Input value={form.storage} onChange={e => set('storage', e.target.value)} disabled={!!editing} className="mt-1" placeholder="backup-nfs" />
            </div>
            <div>
              <Label>{t('storages.type')}</Label>
              <Select
                value={form.type}
                onValueChange={v => { if (v) setForm(p => ({ ...p, type: v, content: DEFAULT_CONTENT[v] ?? p.content })); }}
                disabled={!!editing}
              >
                <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                <SelectContent>{STORAGE_TYPES.map(ty => <SelectItem key={ty} value={ty}>{ty.toUpperCase()}</SelectItem>)}</SelectContent>
              </Select>
            </div>
          </div>

          {!editing && form.type === 'dir' && (
            <div><Label>{t('storages.path')}</Label><Input value={form.path} onChange={e => set('path', e.target.value)} className="mt-1" placeholder="/mnt/backups" /></div>
          )}
          {!editing && form.type === 'nfs' && (
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>{t('storages.server')}</Label>
                <div className="mt-1 flex gap-1.5">
                  <Input value={form.server} onChange={e => { set('server', e.target.value); setNfsExports([]); }} placeholder="10.0.0.5" />
                  <Button type="button" variant="outline" size="icon" className="shrink-0" title={t('storages.scan_exports')}
                    disabled={scanNfs.isPending} onClick={handleScanNfs}>
                    <Search className="h-4 w-4" />
                  </Button>
                </div>
              </div>
              <div>
                <Label>{t('storages.export')}</Label>
                {nfsExports.length > 0 ? (
                  <Select value={form.export} onValueChange={v => v && set('export', v)}>
                    <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>{nfsExports.map(p => <SelectItem key={p} value={p}>{p}</SelectItem>)}</SelectContent>
                  </Select>
                ) : (
                  <Input value={form.export} onChange={e => set('export', e.target.value)} className="mt-1" placeholder="/exports/backup" />
                )}
              </div>
            </div>
          )}
          {!editing && form.type === 'cifs' && (
            <div className="grid grid-cols-2 gap-3">
              <div><Label>{t('storages.server')}</Label><Input value={form.server} onChange={e => set('server', e.target.value)} className="mt-1" /></div>
              <div><Label>{t('storages.share')}</Label><Input value={form.share} onChange={e => set('share', e.target.value)} className="mt-1" /></div>
              <div><Label>{t('storages.username')}</Label><Input value={form.username} onChange={e => set('username', e.target.value)} className="mt-1" /></div>
              <div><Label>{t('storages.password')}</Label><Input type="password" value={form.password} onChange={e => set('password', e.target.value)} className="mt-1" /></div>
            </div>
          )}
          {!editing && form.type === 'pbs' && (
            <div className="grid grid-cols-2 gap-3">
              <div><Label>{t('storages.server')}</Label><Input value={form.server} onChange={e => set('server', e.target.value)} className="mt-1" /></div>
              <div><Label>{t('storages.datastore')}</Label><Input value={form.datastore} onChange={e => set('datastore', e.target.value)} className="mt-1" /></div>
              <div><Label>{t('storages.username')}</Label><Input value={form.username} onChange={e => set('username', e.target.value)} className="mt-1" placeholder="user@pbs" /></div>
              <div><Label>{t('storages.password')}</Label><Input type="password" value={form.password} onChange={e => set('password', e.target.value)} className="mt-1" /></div>
              <div className="col-span-2"><Label>{t('storages.fingerprint')}</Label><Input value={form.fingerprint} onChange={e => set('fingerprint', e.target.value)} className="mt-1 font-mono" /></div>
            </div>
          )}
          {!editing && form.type === 'lvm' && (
            <div><Label>{t('storages.vgname')}</Label><Input value={form.vgname} onChange={e => set('vgname', e.target.value)} className="mt-1 font-mono" placeholder="pve" /></div>
          )}
          {!editing && form.type === 'lvmthin' && (
            <div className="grid grid-cols-2 gap-3">
              <div><Label>{t('storages.vgname')}</Label><Input value={form.vgname} onChange={e => set('vgname', e.target.value)} className="mt-1 font-mono" placeholder="pve" /></div>
              <div><Label>{t('storages.thinpool')}</Label><Input value={form.thinpool} onChange={e => set('thinpool', e.target.value)} className="mt-1 font-mono" placeholder="data" /></div>
            </div>
          )}
          {!editing && form.type === 'zfspool' && (
            <div className="grid grid-cols-2 gap-3">
              <div><Label>{t('storages.zfs_pool')}</Label><Input value={form.pool} onChange={e => set('pool', e.target.value)} className="mt-1 font-mono" placeholder="rpool/data" /></div>
              <label className="flex items-end gap-2 pb-1.5 text-sm">
                <Checkbox checked={form.sparse} onChange={e => set('sparse', e.target.checked)} />
                {t('storages.sparse')}
              </label>
            </div>
          )}
          {!editing && form.type === 'rbd' && (
            <div className="grid grid-cols-2 gap-3">
              <div><Label>{t('storages.ceph_pool')}</Label><Input value={form.pool} onChange={e => set('pool', e.target.value)} className="mt-1 font-mono" placeholder="vm-pool" /></div>
              <div><Label>{t('storages.username')}</Label><Input value={form.username} onChange={e => set('username', e.target.value)} className="mt-1" placeholder="admin" /></div>
              <div className="col-span-2"><Label>{t('storages.monhost')}</Label><Input value={form.monhost} onChange={e => set('monhost', e.target.value)} className="mt-1 font-mono" placeholder={t('storages.monhost_placeholder')} /></div>
            </div>
          )}
          {!editing && form.type === 'cephfs' && (
            <div className="grid grid-cols-2 gap-3">
              <div><Label>{t('storages.fsname')}</Label><Input value={form.fsname} onChange={e => set('fsname', e.target.value)} className="mt-1 font-mono" placeholder="cephfs" /></div>
              <div><Label>{t('storages.username')}</Label><Input value={form.username} onChange={e => set('username', e.target.value)} className="mt-1" placeholder="admin" /></div>
              <div className="col-span-2"><Label>{t('storages.monhost')}</Label><Input value={form.monhost} onChange={e => set('monhost', e.target.value)} className="mt-1 font-mono" placeholder={t('storages.monhost_placeholder')} /></div>
            </div>
          )}

          <div><Label>{t('storages.content')}</Label><Input value={form.content} onChange={e => set('content', e.target.value)} className="mt-1" placeholder="backup,images,iso" /></div>
          <div><Label>{t('storages.nodes')}</Label><Input value={form.nodes} onChange={e => set('nodes', e.target.value)} className="mt-1" placeholder={t('storages.nodes_placeholder')} /></div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={pending || (!editing && !form.storage.trim())}>
            {editing ? t('common.save') : t('common.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Native PVE backup job ====================

type NativeJob = Record<string, unknown>;

const BACKUP_MODES = ['snapshot', 'suspend', 'stop'];
const COMPRESS_OPTS = ['zstd', 'gzip', 'lzo', '0'];

/** Разобрать prune-backups="keep-last=3,keep-daily=7" в отдельные поля. */
function parsePrune(spec: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (typeof spec !== 'string') return out;
  for (const part of spec.split(',')) {
    const [k, v] = part.split('=');
    const n = Number(v);
    if (k && !Number.isNaN(n)) out[k.trim()] = n;
  }
  return out;
}

interface NativeJobForm {
  schedule: string;
  storage: string;
  mode: string;
  compress: string;
  enabled: boolean;
  selection_mode: 'all' | 'vmid';
  vmid: string;
  keep_last: string;
  keep_daily: string;
  keep_weekly: string;
  keep_monthly: string;
  comment: string;
  mailto: string;
}

function jobToForm(job: NativeJob | null): NativeJobForm {
  const prune = parsePrune(job?.['prune-backups']);
  const hasVmid = !!(job && (job.vmid || job.pool) && job.all !== 1 && job.all !== '1');
  return {
    schedule: String(job?.schedule ?? ''),
    storage: String(job?.storage ?? ''),
    mode: String(job?.mode ?? 'snapshot'),
    compress: String(job?.compress ?? 'zstd'),
    enabled: !(job && (job.enabled === 0 || job.enabled === '0')),
    selection_mode: hasVmid ? 'vmid' : 'all',
    vmid: String(job?.vmid ?? ''),
    keep_last: prune['keep-last'] != null ? String(prune['keep-last']) : '',
    keep_daily: prune['keep-daily'] != null ? String(prune['keep-daily']) : '',
    keep_weekly: prune['keep-weekly'] != null ? String(prune['keep-weekly']) : '',
    keep_monthly: prune['keep-monthly'] != null ? String(prune['keep-monthly']) : '',
    comment: String(job?.comment ?? ''),
    mailto: String(job?.mailto ?? ''),
  };
}

function NativeJobDialog({ serverId, open, onOpenChange, editing, backupStorages }: {
  serverId: number;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  editing: NativeJob | null;
  backupStorages: string[];
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<NativeJobForm>(() => jobToForm(editing));
  const create = useCreateProxmoxJob(serverId);
  const update = useUpdateProxmoxJob(serverId);
  const pending = create.isPending || update.isPending;

  // Refill the form each time the dialog opens (or is pointed at another job).
  const [filledFor, setFilledFor] = useState({ open: false, editing: null as NativeJob | null });
  if (filledFor.open !== open || filledFor.editing !== editing) {
    setFilledFor({ open, editing });
    if (open) setForm(jobToForm(editing));
  }

  const set = <K extends keyof NativeJobForm>(k: K, v: NativeJobForm[K]) =>
    setForm(prev => ({ ...prev, [k]: v }));

  const submit = () => {
    if (!form.schedule.trim() || !form.storage) {
      toast.error(t('storages.job_schedule_storage_required'));
      return;
    }
    const payload: ProxmoxJobInput = {
      schedule: form.schedule.trim(),
      storage: form.storage,
      mode: form.mode,
      compress: form.compress,
      enabled: form.enabled,
      selection_mode: form.selection_mode,
      vmid: form.selection_mode === 'vmid' ? form.vmid.trim() : undefined,
      keep_last: form.keep_last ? Number(form.keep_last) : undefined,
      keep_daily: form.keep_daily ? Number(form.keep_daily) : undefined,
      keep_weekly: form.keep_weekly ? Number(form.keep_weekly) : undefined,
      keep_monthly: form.keep_monthly ? Number(form.keep_monthly) : undefined,
      comment: form.comment.trim() || undefined,
      mailto: form.mailto.trim() || undefined,
    };
    const opts = {
      onSuccess: () => { toast.success(editing ? t('storages.job_updated') : t('storages.job_created')); onOpenChange(false); },
      onError: (e: Error) => toast.error(e.message),
    };
    if (editing) update.mutate({ id: String(editing.id), data: payload }, opts);
    else create.mutate(payload, opts);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <CalendarClock className="h-5 w-5" />
            {editing ? t('storages.edit_native_job') : t('storages.add_native_job')}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('storages.schedule')}</Label>
              <Input value={form.schedule} onChange={e => set('schedule', e.target.value)} placeholder="sat 03:00" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('storages.id')}</Label>
              <Select value={form.storage} onValueChange={v => v && set('storage', v)}>
                <SelectTrigger className="w-full"><SelectValue placeholder={t('backups.select_storage')} /></SelectTrigger>
                <SelectContent>
                  {backupStorages.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          </div>
          <p className="-mt-1 text-xs text-muted-foreground">{t('storages.schedule_hint')}</p>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('backups.mode')}</Label>
              <Select value={form.mode} onValueChange={v => v && set('mode', v)}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{BACKUP_MODES.map(m => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>{t('storages.compress')}</Label>
              <Select value={form.compress} onValueChange={v => v && set('compress', v)}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{COMPRESS_OPTS.map(c => <SelectItem key={c} value={c}>{c === '0' ? t('storages.no_compress') : c}</SelectItem>)}</SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>{t('storages.selection')}</Label>
            <Select value={form.selection_mode} onValueChange={v => v && set('selection_mode', v as 'all' | 'vmid')}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('storages.select_all_guests')}</SelectItem>
                <SelectItem value="vmid">{t('storages.select_vmids')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {form.selection_mode === 'vmid' && (
            <div className="space-y-1.5">
              <Label>{t('backups.vmids')}</Label>
              <Input value={form.vmid} onChange={e => set('vmid', e.target.value)} placeholder="100,101,102" />
            </div>
          )}

          <div>
            <Label className="text-xs text-muted-foreground">{t('storages.retention')}</Label>
            <div className="mt-1.5 grid grid-cols-4 gap-2">
              <div className="space-y-1"><Label className="text-2xs">{t('backups.keep_last')}</Label><Input type="number" min={0} value={form.keep_last} onChange={e => set('keep_last', e.target.value)} /></div>
              <div className="space-y-1"><Label className="text-2xs">{t('backups.keep_daily')}</Label><Input type="number" min={0} value={form.keep_daily} onChange={e => set('keep_daily', e.target.value)} /></div>
              <div className="space-y-1"><Label className="text-2xs">{t('backups.keep_weekly')}</Label><Input type="number" min={0} value={form.keep_weekly} onChange={e => set('keep_weekly', e.target.value)} /></div>
              <div className="space-y-1"><Label className="text-2xs">{t('backups.keep_monthly')}</Label><Input type="number" min={0} value={form.keep_monthly} onChange={e => set('keep_monthly', e.target.value)} /></div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('storages.comment')}</Label>
              <Input value={form.comment} onChange={e => set('comment', e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>{t('storages.mailto')}</Label>
              <Input value={form.mailto} onChange={e => set('mailto', e.target.value)} placeholder="admin@example.com" />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={form.enabled} onChange={e => set('enabled', e.target.checked)} />
            <span>{t('storages.enabled')}</span>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={pending}>
            {editing ? t('common.save') : t('common.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function StoragesTab() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data: servers = [] } = useServers();
  const [serverPick, setServerId] = useState(0);
  // Выбранный сервер или первый из списка. Протухший выбор (сервера нет в
  // активной рабочей области, например после её переключения) не держим —
  // иначе Select показывает голый id сервера вместо имени.
  const serverId = servers.some((s) => s.id === serverPick) ? serverPick : (servers[0]?.id ?? 0);

  const { data: storagesData, isLoading } = useBackupStorages(serverId);
  const { data: jobsData } = useProxmoxBackupJobs(serverId);
  const deleteStorage = useDeleteStorage(serverId);
  const updateStorage = useUpdateStorage(serverId);
  const updateJob = useUpdateProxmoxJob(serverId);
  const deleteJob = useDeleteProxmoxJob(serverId);

  const storages = (storagesData?.storages ?? []) as StorageRow[];
  const nativeJobs = (jobsData?.jobs ?? []) as Array<Record<string, unknown>>;

  // Хранилища, поддерживающие бэкапы — для селекта в диалоге задания
  const backupStorages = storages
    .filter(s => (s.content ?? '').split(',').map(c => c.trim()).includes('backup'))
    .map(s => s.storage);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<StorageRow | null>(null);
  const [jobDialogOpen, setJobDialogOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<NativeJob | null>(null);

  const handleDelete = async (s: string) => {
    if (!await confirm(`${t('storages.delete_confirm')} "${s}"?`)) return;
    deleteStorage.mutate(s, {
      onSuccess: () => toast.success(t('storages.deleted')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const handleToggleStorage = (s: StorageRow) => {
    const disabling = s.disable !== 1;
    updateStorage.mutate({ storage: s.storage, disable: disabling ? 1 : 0 }, {
      onSuccess: () => toast.success(disabling ? t('storages.disabled_ok', 'Хранилище отключено') : t('storages.enabled_ok', 'Хранилище включено')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const handleDeleteJob = async (job: NativeJob) => {
    if (!await confirm(`${t('storages.delete_job_confirm')} "${String(job.id)}"?`)) return;
    deleteJob.mutate(String(job.id), {
      onSuccess: () => toast.success(t('storages.job_deleted')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const handleToggleJob = (job: NativeJob) => {
    const currentlyEnabled = !(job.enabled === 0 || job.enabled === '0');
    const form = jobToForm(job);
    updateJob.mutate(
      {
        id: String(job.id),
        data: {
          schedule: form.schedule,
          storage: form.storage,
          mode: form.mode,
          compress: form.compress,
          enabled: !currentlyEnabled,
          selection_mode: form.selection_mode,
          vmid: form.selection_mode === 'vmid' ? form.vmid : undefined,
          keep_last: form.keep_last ? Number(form.keep_last) : undefined,
          keep_daily: form.keep_daily ? Number(form.keep_daily) : undefined,
          keep_weekly: form.keep_weekly ? Number(form.keep_weekly) : undefined,
          keep_monthly: form.keep_monthly ? Number(form.keep_monthly) : undefined,
          comment: form.comment || undefined,
          mailto: form.mailto || undefined,
        },
      },
      {
        onSuccess: () => toast.success(currentlyEnabled ? t('storages.job_disabled') : t('storages.job_enabled')),
        onError: (e: Error) => toast.error(e.message),
      }
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Select value={serverId ? String(serverId) : ''} onValueChange={v => { if (v) setServerId(Number(v)); }}>
          <SelectTrigger className="w-[220px]"><SelectValue placeholder={t('backups.select_server')} /></SelectTrigger>
          <SelectContent>{servers.map(s => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}</SelectContent>
        </Select>
        <Button size="sm" className="ml-auto" onClick={() => { setEditing(null); setDialogOpen(true); }} disabled={!serverId}>
          <Plus className="mr-1 h-4 w-4" />{t('storages.add')}
        </Button>
      </div>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-lg"><Database className="h-5 w-5" />{t('storages.title')}</CardTitle></CardHeader>
        <CardContent>
          {isLoading ? <p className="py-6 text-center text-sm text-muted-foreground">{t('common.loading')}</p>
            : storages.length === 0 ? <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data')}</p>
            : (
              <Table>
                <TableHeader><TableRow>
                  <TableHead>{t('storages.id')}</TableHead>
                  <TableHead>{t('storages.type')}</TableHead>
                  <TableHead>{t('storages.content')}</TableHead>
                  <TableHead>{t('storages.nodes')}</TableHead>
                  <TableHead>{t('storages.usage')}</TableHead>
                  <TableHead className="text-right">{t('common.actions')}</TableHead>
                </TableRow></TableHeader>
                <TableBody>
                  {storages.map(s => {
                    const inactiveNodes = s.inactive_nodes ?? [];
                    return (
                    <TableRow key={s.storage} className={inactiveNodes.length > 0 ? 'bg-destructive/5' : undefined}>
                      <TableCell className="font-mono font-medium">
                        {s.storage}
                        {s.disable === 1 && <Badge variant="secondary" className="ml-2 text-2xs">{t('storages.disabled')}</Badge>}
                        {inactiveNodes.length > 0 && (
                          <Badge
                            variant="destructive"
                            className="ml-2 text-2xs"
                            title={t('storages.inactive_hint', { nodes: inactiveNodes.join(', ') })}
                          >
                            <AlertTriangle className="h-3 w-3" />
                            {t('storages.inactive')}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell><Badge variant="outline">{s.type}</Badge></TableCell>
                      <TableCell className="text-xs">{s.content || '—'}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {s.nodes || t('storages.all_nodes')}
                        {inactiveNodes.length > 0 && (
                          <span className="block text-2xs text-destructive">
                            {t('storages.inactive_on', { nodes: inactiveNodes.join(', ') })}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {s.total ? `${formatBytes(s.used || 0)} / ${formatBytes(s.total)}` : '—'}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="icon" className="h-7 w-7"
                            title={s.disable === 1 ? t('backups.enable') : t('backups.disable')}
                            onClick={() => handleToggleStorage(s)}>
                            {s.disable === 1 ? <Power className="h-3.5 w-3.5" /> : <PowerOff className="h-3.5 w-3.5" />}
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => { setEditing(s); setDialogOpen(true); }}>
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => handleDelete(s.storage)}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
        </CardContent>
      </Card>

      {/* Native PVE backup schedules */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="flex items-center gap-2 text-lg"><CalendarClock className="h-5 w-5" />{t('storages.native_jobs')}</CardTitle>
          <Button size="sm" variant="outline" onClick={() => { setEditingJob(null); setJobDialogOpen(true); }} disabled={!serverId}>
            <Plus className="mr-1 h-4 w-4" />{t('storages.add_native_job')}
          </Button>
        </CardHeader>
        <CardContent>
          {nativeJobs.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">{t('storages.no_native_jobs')}</p>
          ) : (
            <Table>
              <TableHeader><TableRow>
                <TableHead>ID</TableHead><TableHead>{t('storages.schedule')}</TableHead>
                <TableHead>{t('storages.id')}</TableHead><TableHead>{t('backups.mode')}</TableHead>
                <TableHead>{t('common.status')}</TableHead>
                <TableHead className="text-right">{t('common.actions')}</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {nativeJobs.map((j, i) => {
                  const enabled = !(j.enabled === 0 || j.enabled === '0');
                  return (
                    <TableRow key={String(j.id ?? i)}>
                      <TableCell className="font-mono text-xs">{String(j.id ?? '—')}</TableCell>
                      <TableCell className="font-mono text-xs">{String(j.schedule ?? j.starttime ?? '—')}</TableCell>
                      <TableCell className="text-xs">{String(j.storage ?? '—')}</TableCell>
                      <TableCell className="text-xs">{String(j.mode ?? '—')}</TableCell>
                      <TableCell>
                        <Badge variant={enabled ? 'default' : 'secondary'}>
                          {enabled ? t('storages.enabled') : t('storages.disabled')}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="icon" className="h-7 w-7" title={enabled ? t('backups.disable') : t('backups.enable')} onClick={() => handleToggleJob(j)}>
                            {enabled ? <PowerOff className="h-3.5 w-3.5" /> : <Power className="h-3.5 w-3.5" />}
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7" title={t('common.edit')} onClick={() => { setEditingJob(j); setJobDialogOpen(true); }}>
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" title={t('common.delete')} onClick={() => handleDeleteJob(j)}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {dialogOpen && (
        <StorageDialog
          serverId={serverId}
          open={dialogOpen}
          onOpenChange={(v) => { setDialogOpen(v); if (!v) setEditing(null); }}
          editing={editing}
        />
      )}
      {jobDialogOpen && (
        <NativeJobDialog
          serverId={serverId}
          open={jobDialogOpen}
          onOpenChange={(v) => { setJobDialogOpen(v); if (!v) setEditingJob(null); }}
          editing={editingJob}
          backupStorages={backupStorages}
        />
      )}
    </div>
  );
}
