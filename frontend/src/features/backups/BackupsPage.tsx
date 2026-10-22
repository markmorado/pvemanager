import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, Play, Calendar, RotateCcw, Trash2, Loader2, Plus, Pencil } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogClose } from '@/components/ui/dialog';
import { useNodes, useServers } from '@/hooks/use-nodes';
import { useVirtualMachines } from '@/hooks/use-instances';
import {
  useBackupJobs,
  useBackupList,
  useBackupStorages,
  useToggleBackupJob,
  useRunBackupJob,
  useDeleteBackup,
  useRestoreBackup,
  useBulkRestoreBackup,
  useCreateBackupJob,
  useUpdateBackupJob,
  useDeleteBackupJob,
  type BackupJobInput,
} from '@/hooks/use-backups';
import { formatBytes } from '@/lib/format';
import { toast } from 'sonner';
import StoragesTab from './StoragesTab';

const ALL_NODES = '__all__';
// Sentinel for the target-storage selects: restore disks to the storage recorded
// in the backup config (i.e. omit the `storage` param), rather than a chosen one.
const FROM_BACKUP = '__from_backup__';

type BackupItem = {
  volid: string;
  vmid: number;
  size: number;
  ctime: number;
  format: string;
  notes?: string;
  node?: string;
};

type BackupJob = {
  id: number;
  server_id: number;
  node: string;
  vmids: (number | string)[];
  storage: string;
  mode: string;
  compress: string;
  notes?: string | null;
  keep_last: number;
  keep_daily: number;
  keep_weekly: number;
  keep_monthly: number;
  cron_expression: string;
  enabled: boolean;
};

const CRON_PRESETS: { label: string; value: string }[] = [
  { label: 'daily_2am', value: '0 2 * * *' },
  { label: 'daily_midnight', value: '0 0 * * *' },
  { label: 'every_6h', value: '0 */6 * * *' },
  { label: 'weekly_sunday', value: '0 3 * * 0' },
  { label: 'monthly_1st', value: '0 4 1 * *' },
];

const emptyJobForm = (serverId: number, node: string, storage: string): BackupJobInput => ({
  server_id: serverId,
  node,
  vmids: [],
  storage,
  mode: 'snapshot',
  compress: 'zstd',
  notes: '',
  keep_last: 3,
  keep_daily: 7,
  keep_weekly: 4,
  keep_monthly: 6,
  cron_expression: '0 2 * * *',
  enabled: true,
});

// Detect VM type from a backup volid like
//   local:backup/vzdump-qemu-100-2024_01_01-00_00_00.vma.zst
//   local:backup/vzdump-lxc-101-...tar.zst
function detectVmType(volid: string): 'qemu' | 'lxc' {
  const v = (volid || '').toLowerCase();
  if (v.includes('vzdump-lxc-') || v.includes('-lxc-')) return 'lxc';
  return 'qemu';
}

export default function BackupsPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useState('files');
  const [serverPick, setSelectedServer] = useState<string>('');
  const [selectedNode, setSelectedNode] = useState<string>(ALL_NODES);
  const [storagePick, setSelectedStorage] = useState('');
  const [search, setSearch] = useState('');

  const { data: servers = [] } = useServers();
  // The picked server, or the first one when nothing (or a server no longer in
  // the active workspace) is picked — a stale id would silently empty the list
  // and make the Select show the raw id instead of a name.
  const selectedServer = servers.some(s => String(s.id) === serverPick)
    ? serverPick
    : (servers[0] ? String(servers[0].id) : '');
  const sid = selectedServer ? Number(selectedServer) : 0;
  // A different server (picked, or fallen back to) invalidates the node choice.
  const [nodesOfServer, setNodesOfServer] = useState(selectedServer);
  if (nodesOfServer !== selectedServer) {
    setNodesOfServer(selectedServer);
    setSelectedNode(ALL_NODES);
    setSelectedStorage('');
  }
  const { data: nodesData } = useNodes(sid);
  const { data: storagesData } = useBackupStorages(sid);
  const allStorages = useMemo(
    () => (storagesData?.storages || []) as { storage: string; type: string; content: string; nodes?: string; used?: number; total?: number }[],
    [storagesData],
  );
  const storages = useMemo(
    () => allStorages.filter(s => (s.content || '').split(',').includes('backup')),
    [allStorages],
  );
  // The picked backup storage, or the first backup-capable one.
  const selectedStorage = storages.some(s => s.storage === storagePick)
    ? storagePick
    : (storages[0]?.storage ?? '');
  const nodeParam = selectedNode === ALL_NODES ? '' : selectedNode;
  const { data: backupsData } = useBackupList(sid, nodeParam, selectedStorage);
  const { data: jobsData } = useBackupJobs();
  const toggleJob = useToggleBackupJob();
  const runJob = useRunBackupJob();
  const deleteBackup = useDeleteBackup();
  const restoreBackup = useRestoreBackup();
  const bulkRestore = useBulkRestoreBackup();
  const createJob = useCreateBackupJob();
  const updateJob = useUpdateBackupJob();
  const deleteJob = useDeleteBackupJob();

  // --- Job dialog state ---
  const [jobDialogOpen, setJobDialogOpen] = useState(false);
  const [editingJobId, setEditingJobId] = useState<number | null>(null);
  const [jobForm, setJobForm] = useState<BackupJobInput>(() => emptyJobForm(0, '', ''));
  // Selection state for the VMID picker. `selectAll` overrides the explicit Set.
  const [selectedVmids, setSelectedVmids] = useState<Set<number>>(new Set());
  const [selectAllVmids, setSelectAllVmids] = useState(false);
  const [vmFilter, setVmFilter] = useState('');
  const [deleteJobTarget, setDeleteJobTarget] = useState<BackupJob | null>(null);

  // --- Restore dialog state ---
  const [restoreTarget, setRestoreTarget] = useState<BackupItem | null>(null);
  const [restoreTargetNode, setRestoreTargetNode] = useState('');
  const [restoreTargetStorage, setRestoreTargetStorage] = useState(FROM_BACKUP);
  const [restoreNewVmid, setRestoreNewVmid] = useState('');
  const [restoreStart, setRestoreStart] = useState(false);
  const [restoreUnique, setRestoreUnique] = useState(true);

  // --- Delete confirmation state ---
  const [deleteTarget, setDeleteTarget] = useState<BackupItem | null>(null);

  // --- Bulk restore selection (one archive per VMID) ---
  const [selectedBackups, setSelectedBackups] = useState<Map<number, BackupItem>>(new Map());
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkMode, setBulkMode] = useState<'in_place' | 'new_vmid'>('in_place');
  const [bulkTargetNode, setBulkTargetNode] = useState('__source__');
  const [bulkTargetStorage, setBulkTargetStorage] = useState(FROM_BACKUP);
  const [bulkStart, setBulkStart] = useState(false);
  const [bulkConfirm, setBulkConfirm] = useState(false);

  const nodes = useMemo(() => (nodesData?.nodes || []), [nodesData]);
  // Storages that can hold restored guest disks on a given node.
  // qemu => content 'images', lxc => 'rootdir', 'any' => either (bulk, mixed types).
  const diskStoragesFor = (node: string, vmType: 'qemu' | 'lxc' | 'any') => {
    const needed = vmType === 'lxc' ? ['rootdir'] : vmType === 'qemu' ? ['images'] : ['images', 'rootdir'];
    return allStorages.filter(s => {
      const contents = (s.content || '').split(',');
      if (!needed.some(c => contents.includes(c))) return false;
      // `nodes` empty/absent => storage is available on all nodes.
      if (node && s.nodes) return s.nodes.split(',').includes(node);
      return true;
    });
  };
  const allBackups = useMemo(() => (backupsData?.backups || []) as BackupItem[], [backupsData]);
  const backups = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return allBackups;
    return allBackups.filter(b =>
      String(b.vmid).includes(q) ||
      (b.volid || '').toLowerCase().includes(q) ||
      (b.notes || '').toLowerCase().includes(q) ||
      (b.node || '').toLowerCase().includes(q),
    );
  }, [allBackups, search]);
  const jobs = (jobsData?.jobs || []) as BackupJob[];

  // Latest backup per VMID — used by the "select all" checkbox (one archive per VMID).
  const latestPerVmid = useMemo(() => {
    const m = new Map<number, BackupItem>();
    for (const b of backups) {
      const cur = m.get(b.vmid);
      if (!cur || b.ctime > cur.ctime) m.set(b.vmid, b);
    }
    return m;
  }, [backups]);
  const allVmidsSelected = latestPerVmid.size > 0 && selectedBackups.size === latestPerVmid.size;

  // Servers/storages for the job dialog (across all servers, not only the file-tab selection)
  const jobServerId = jobForm.server_id;
  const { data: jobNodesData } = useNodes(jobServerId || 0);
  const { data: jobStoragesData } = useBackupStorages(jobServerId || 0);
  const jobNodes = (jobNodesData?.nodes || []) as { node: string }[];
  const jobStorages = useMemo(() => {
    const list = (jobStoragesData?.storages || []) as { storage: string; type: string; content: string }[];
    return list.filter(s => (s.content || '').split(',').includes('backup'));
  }, [jobStoragesData]);

  // VM/CT list for the picker (across all servers; filtered by server+node below)
  const { data: allVms = [] } = useVirtualMachines();
  const pickerVms = useMemo(() => {
    if (!jobForm.server_id) return [];
    let list = allVms.filter(v => v.server_id === jobForm.server_id && !v.template);
    if (jobForm.node) list = list.filter(v => v.node === jobForm.node);
    if (vmFilter.trim()) {
      const q = vmFilter.trim().toLowerCase();
      list = list.filter(v => String(v.vmid).includes(q) || (v.name || '').toLowerCase().includes(q));
    }
    return list.sort((a, b) => a.vmid - b.vmid);
  }, [allVms, jobForm.server_id, jobForm.node, vmFilter]);

  const serverNameById = useMemo(() => {
    const map = new Map<number, string>();
    for (const s of servers) map.set(s.id, s.name);
    return map;
  }, [servers]);

  const openRestore = (b: BackupItem) => {
    setRestoreTarget(b);
    setRestoreTargetNode(b.node || (selectedNode === ALL_NODES ? '' : selectedNode) || nodes[0]?.node || '');
    setRestoreTargetStorage(FROM_BACKUP);
    setRestoreNewVmid('');
    setRestoreStart(false);
    setRestoreUnique(true);
  };

  const submitRestore = () => {
    if (!restoreTarget || !sid) return;
    const node = restoreTargetNode;
    if (!node) {
      toast.error(t('backups.node_required'));
      return;
    }
    const vmType = detectVmType(restoreTarget.volid);
    const newVmidNum = restoreNewVmid ? Number(restoreNewVmid) : undefined;
    if (restoreNewVmid && (!Number.isFinite(newVmidNum) || (newVmidNum as number) <= 0)) {
      toast.error(t('backups.invalid_vmid'));
      return;
    }
    restoreBackup.mutate(
      {
        server_id: sid,
        node,
        vmid: newVmidNum ?? restoreTarget.vmid,
        archive: restoreTarget.volid,
        ...(restoreTargetStorage !== FROM_BACKUP ? { storage: restoreTargetStorage } : {}),
        vm_type: vmType,
        new_vmid: newVmidNum,
        start: restoreStart,
        ...(vmType === 'qemu' ? { unique: restoreUnique } : {}),
      },
      {
        onSuccess: () => {
          toast.success(t('backups.restore_started'));
          setRestoreTarget(null);
        },
        onError: (e: any) => toast.error(e?.message || t('backups.restore_failed')),
      },
    );
  };

  // --- Bulk restore selection helpers (one archive per VMID) ---
  // Reset selection whenever the visible backup set changes (server/node/storage switch).
  const backupSetKey = `${sid}|${nodeParam}|${selectedStorage}`;
  const [selectionSetKey, setSelectionSetKey] = useState(backupSetKey);
  if (selectionSetKey !== backupSetKey) {
    setSelectionSetKey(backupSetKey);
    setSelectedBackups(new Map());
  }

  const toggleBackupSelected = (b: BackupItem) => {
    setSelectedBackups(prev => {
      const next = new Map(prev);
      const current = next.get(b.vmid);
      if (current && current.volid === b.volid) {
        next.delete(b.vmid);           // unselect this one
      } else {
        next.set(b.vmid, b);           // select / replace the archive chosen for this VMID
      }
      return next;
    });
  };

  const openBulkRestore = () => {
    if (selectedBackups.size === 0) return;
    setBulkMode('in_place');
    setBulkTargetNode('__source__');
    setBulkTargetStorage(FROM_BACKUP);
    setBulkStart(false);
    setBulkConfirm(false);
    setBulkOpen(true);
  };

  const submitBulkRestore = () => {
    if (!sid || selectedBackups.size === 0) return;
    const overrideNode = bulkTargetNode === '__source__' ? '' : bulkTargetNode;
    const items = Array.from(selectedBackups.values()).map(b => ({
      node: overrideNode || b.node || (selectedNode === ALL_NODES ? '' : selectedNode) || nodes[0]?.node || '',
      vmid: b.vmid,
      archive: b.volid,
      vm_type: detectVmType(b.volid),
    }));
    if (items.some(it => !it.node)) {
      toast.error(t('backups.node_required'));
      return;
    }
    bulkRestore.mutate(
      {
        server_id: sid,
        ...(bulkTargetStorage !== FROM_BACKUP ? { storage: bulkTargetStorage } : {}),
        mode: bulkMode,
        start: bulkStart,
        items,
      },
      {
        onSuccess: (res) => {
          if (res.failed === 0) {
            toast.success(t('backups.bulk_restore_done', { count: res.succeeded }));
          } else {
            toast.warning(t('backups.bulk_restore_partial', { ok: res.succeeded, fail: res.failed }));
          }
          setBulkOpen(false);
          setSelectedBackups(new Map());
        },
        onError: (e: any) => toast.error(e?.message || t('backups.restore_failed')),
      },
    );
  };

  const openCreateJob = () => {
    const initialServer = sid || (servers[0]?.id ?? 0);
    setEditingJobId(null);
    setJobForm(emptyJobForm(initialServer, '', ''));
    setSelectedVmids(new Set());
    setSelectAllVmids(false);
    setVmFilter('');
    setJobDialogOpen(true);
  };

  const openEditJob = (job: BackupJob) => {
    setEditingJobId(job.id);
    setJobForm({
      server_id: job.server_id,
      node: job.node,
      vmids: job.vmids,
      storage: job.storage,
      mode: job.mode,
      compress: job.compress,
      notes: job.notes || '',
      keep_last: job.keep_last,
      keep_daily: job.keep_daily,
      keep_weekly: job.keep_weekly,
      keep_monthly: job.keep_monthly,
      cron_expression: job.cron_expression,
      enabled: job.enabled,
    });
    const isAll = (job.vmids || []).some(v => String(v).toLowerCase() === 'all');
    setSelectAllVmids(isAll);
    setSelectedVmids(
      isAll
        ? new Set()
        : new Set((job.vmids || []).map(v => Number(v)).filter(n => Number.isFinite(n))),
    );
    setVmFilter('');
    setJobDialogOpen(true);
  };

  const submitJob = () => {
    const vmids: (number | string)[] = selectAllVmids
      ? ['all']
      : Array.from(selectedVmids).sort((a, b) => a - b);
    if (!jobForm.server_id) { toast.error(t('backups.select_server')); return; }
    if (!jobForm.node) { toast.error(t('backups.node_required')); return; }
    if (!jobForm.storage) { toast.error(t('backups.select_storage')); return; }
    if (vmids.length === 0) { toast.error(t('backups.vmids_required')); return; }
    if (!jobForm.cron_expression?.trim()) { toast.error(t('backups.cron_required')); return; }

    const payload: BackupJobInput = { ...jobForm, vmids };

    if (editingJobId != null) {
      updateJob.mutate(
        { id: editingJobId, data: payload },
        {
          onSuccess: () => { toast.success(t('backups.job_updated')); setJobDialogOpen(false); },
          onError: (e: any) => toast.error(e?.message || t('backups.job_save_failed')),
        },
      );
    } else {
      createJob.mutate(payload, {
        onSuccess: () => { toast.success(t('backups.job_created')); setJobDialogOpen(false); },
        onError: (e: any) => toast.error(e?.message || t('backups.job_save_failed')),
      });
    }
  };

  const submitDeleteJob = () => {
    if (!deleteJobTarget) return;
    deleteJob.mutate(deleteJobTarget.id, {
      onSuccess: () => { toast.success(t('backups.job_deleted')); setDeleteJobTarget(null); },
      onError: (e: any) => toast.error(e?.message || t('backups.job_delete_failed')),
    });
  };

  const submitDelete = () => {
    if (!deleteTarget || !sid) return;
    const node = deleteTarget.node || (selectedNode === ALL_NODES ? '' : selectedNode) || nodes[0]?.node;
    if (!node) {
      toast.error(t('backups.node_required'));
      return;
    }
    deleteBackup.mutate(
      { server_id: sid, node, storage: selectedStorage, volid: deleteTarget.volid },
      {
        onSuccess: () => {
          toast.success(t('backups.delete_success'));
          setDeleteTarget(null);
        },
        onError: (e: any) => toast.error(e?.message || t('backups.delete_failed')),
      },
    );
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('nav.backups')}</h1>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="files">{t('backups.files')}</TabsTrigger>
          <TabsTrigger value="jobs">{t('backups.scheduled_jobs')}</TabsTrigger>
          <TabsTrigger value="storages">{t('storages.title')}</TabsTrigger>
        </TabsList>

        <TabsContent value="files" className="space-y-4 mt-4">
          {/* Filters */}
          <div className="flex flex-wrap gap-3">
            <Select value={selectedServer} onValueChange={v => { if (v !== null) { setSelectedServer(v); setSelectedNode(ALL_NODES); setSelectedStorage(''); } }}>
              <SelectTrigger className="w-[200px]"><SelectValue placeholder={t('backups.select_server')} /></SelectTrigger>
              <SelectContent>
                {servers.map(s => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={selectedNode} onValueChange={v => { if (v !== null) setSelectedNode(v); }} disabled={!sid}>
              <SelectTrigger className="w-[200px]"><SelectValue placeholder={t('backups.node')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_NODES}>{t('backups.all_nodes')}</SelectItem>
                {nodes.map(n => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={selectedStorage} onValueChange={v => { if (v !== null) setSelectedStorage(v); }} disabled={!sid || storages.length === 0}>
              <SelectTrigger className="w-[220px]"><SelectValue placeholder={t('backups.select_storage')} /></SelectTrigger>
              <SelectContent>
                {storages.map(s => <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>)}
              </SelectContent>
            </Select>
            <Input
              className="w-[260px]"
              placeholder={t('backups.search_backups')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              disabled={!sid}
            />
          </div>

          {/* Bulk selection toolbar */}
          {selectedBackups.size > 0 && (
            <div className="flex items-center gap-2 rounded-lg border bg-muted/50 p-2">
              <span className="text-sm font-medium">
                {t('backups.selected_count', { count: selectedBackups.size })}
              </span>
              <Button size="sm" onClick={openBulkRestore}>
                <RotateCcw className="mr-1 h-3.5 w-3.5" />{t('backups.restore_selected')}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setSelectedBackups(new Map())}>
                {t('common.clear', 'Clear')}
              </Button>
            </div>
          )}

          {/* Backup files table */}
          {backups.length > 0 ? (
            <div className="rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8">
                      <Checkbox
                        checked={allVmidsSelected}
                        title={t('backups.select_all_one_per_vmid')}
                        onChange={() =>
                          setSelectedBackups(allVmidsSelected ? new Map() : new Map(latestPerVmid))
                        }
                      />
                    </TableHead>
                    <TableHead>VMID</TableHead>
                    <TableHead>{t('backups.type')}</TableHead>
                    <TableHead>{t('backups.node')}</TableHead>
                    <TableHead>{t('backups.volume')}</TableHead>
                    <TableHead>{t('backups.size')}</TableHead>
                    <TableHead>{t('backups.date')}</TableHead>
                    <TableHead>{t('backups.format')}</TableHead>
                    <TableHead>{t('backups.notes')}</TableHead>
                    <TableHead className="text-right">{t('common.actions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {backups.map((b, i) => {
                    const vmType = detectVmType(b.volid);
                    const isSelected = selectedBackups.get(b.vmid)?.volid === b.volid;
                    return (
                      <TableRow key={i} data-selected={isSelected}>
                        <TableCell>
                          <Checkbox
                            checked={isSelected}
                            onChange={() => toggleBackupSelected(b)}
                          />
                        </TableCell>
                        <TableCell className="font-mono">{b.vmid}</TableCell>
                        <TableCell>
                          <Badge variant="outline" className="uppercase text-xs">{vmType}</Badge>
                        </TableCell>
                        <TableCell className="font-mono text-xs">{b.node || '—'}</TableCell>
                        <TableCell className="font-mono text-xs max-w-[300px] truncate">{b.volid}</TableCell>
                        <TableCell>{formatBytes(b.size)}</TableCell>
                        <TableCell>{new Date(b.ctime * 1000).toLocaleString()}</TableCell>
                        <TableCell><Badge variant="outline">{b.format}</Badge></TableCell>
                        <TableCell className="text-xs text-muted-foreground">{b.notes || '—'}</TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              title={t('backups.restore')}
                              onClick={() => openRestore(b)}
                            >
                              <RotateCcw className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-destructive hover:text-destructive"
                              title={t('common.delete')}
                              onClick={() => setDeleteTarget(b)}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          ) : (
            <div className="text-center py-16 text-muted-foreground">
              <Archive className="mx-auto h-12 w-12 mb-3 opacity-50" />
              <p>
                {!selectedServer
                  ? t('backups.select_server_first')
                  : search.trim() && allBackups.length > 0
                    ? t('backups.no_backups_match')
                    : t('backups.no_backups')}
              </p>
            </div>
          )}
        </TabsContent>

        <TabsContent value="jobs" className="space-y-4 mt-4">
          <div className="flex justify-end">
            <Button size="sm" onClick={openCreateJob}>
              <Plus className="h-4 w-4 mr-1" /> {t('backups.create_job')}
            </Button>
          </div>
          {jobs.length > 0 ? (
            <div className="rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ID</TableHead>
                    <TableHead>{t('backups.server')}</TableHead>
                    <TableHead>{t('backups.node')}</TableHead>
                    <TableHead>{t('backups.vmids')}</TableHead>
                    <TableHead>{t('backups.storage_col')}</TableHead>
                    <TableHead>{t('backups.schedule')}</TableHead>
                    <TableHead>{t('common.status')}</TableHead>
                    <TableHead className="text-right">{t('common.actions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {jobs.map(job => (
                    <TableRow key={job.id}>
                      <TableCell>{job.id}</TableCell>
                      <TableCell>{serverNameById.get(job.server_id) || job.server_id}</TableCell>
                      <TableCell className="font-mono text-xs">{job.node}</TableCell>
                      <TableCell className="font-mono text-xs">{job.vmids?.join(', ') || '—'}</TableCell>
                      <TableCell>{job.storage}</TableCell>
                      <TableCell className="font-mono text-xs">{job.cron_expression}</TableCell>
                      <TableCell>
                        <Badge variant={job.enabled ? 'default' : 'secondary'}>
                          {job.enabled ? t('backups.enabled') : t('backups.disabled')}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="sm" title={job.enabled ? t('backups.disable') : t('backups.enable')} onClick={() => toggleJob.mutate(job.id)}>
                            {job.enabled ? t('backups.disable') : t('backups.enable')}
                          </Button>
                          <Button variant="ghost" size="sm" title={t('backups.run_now')} onClick={() => { runJob.mutate(job.id); toast.success(t('backups.job_started')); }}>
                            <Play className="h-3.5 w-3.5" />
                          </Button>
                          <Button variant="ghost" size="sm" title={t('common.edit')} onClick={() => openEditJob(job)}>
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button variant="ghost" size="sm" className="text-destructive hover:text-destructive" title={t('common.delete')} onClick={() => setDeleteJobTarget(job)}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <div className="text-center py-16 text-muted-foreground">
              <Calendar className="mx-auto h-12 w-12 mb-3 opacity-50" />
              <p>{t('backups.no_jobs')}</p>
            </div>
          )}
        </TabsContent>

        <TabsContent value="storages" className="mt-4">
          <StoragesTab />
        </TabsContent>
      </Tabs>

      {/* Restore dialog */}
      <Dialog open={!!restoreTarget} onOpenChange={(o) => { if (!o) setRestoreTarget(null); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('backups.restore_title')}</DialogTitle>
          </DialogHeader>
          {restoreTarget && (
            <div className="space-y-3 text-sm">
              <p className="text-muted-foreground">{t('backups.restore_warning')}</p>
              <div className="rounded-md border p-3 space-y-1 bg-muted/40">
                <div><span className="text-muted-foreground">{t('backups.volume')}:</span> <span className="font-mono text-xs break-all">{restoreTarget.volid}</span></div>
                <div><span className="text-muted-foreground">VMID:</span> <span className="font-mono">{restoreTarget.vmid}</span></div>
                <div><span className="text-muted-foreground">{t('backups.type')}:</span> <Badge variant="outline" className="uppercase ml-1">{detectVmType(restoreTarget.volid)}</Badge></div>
                <div><span className="text-muted-foreground">{t('backups.size')}:</span> {formatBytes(restoreTarget.size)}</div>
              </div>
              <div>
                <Label>{t('backups.restore_target_node')}</Label>
                <Select value={restoreTargetNode} onValueChange={v => { if (v !== null) setRestoreTargetNode(v); }}>
                  <SelectTrigger className="mt-1 w-full"><SelectValue /></SelectTrigger>
                  <SelectContent alignItemWithTrigger={false}>
                    {nodes.map(n => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>{t('backups.restore_target_storage')}</Label>
                <Select value={restoreTargetStorage} onValueChange={v => { if (v !== null) setRestoreTargetStorage(v); }}>
                  <SelectTrigger className="mt-1 w-full"><SelectValue /></SelectTrigger>
                  <SelectContent alignItemWithTrigger={false}>
                    <SelectItem value={FROM_BACKUP}>{t('backups.storage_from_backup')}</SelectItem>
                    {diskStoragesFor(restoreTargetNode, detectVmType(restoreTarget.volid)).map(s => (
                      <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="mt-1 text-xs text-muted-foreground">{t('backups.target_storage_hint')}</p>
              </div>
              <div>
                <Label>{t('backups.new_vmid_optional')}</Label>
                <Input
                  className="mt-1"
                  type="number"
                  min={100}
                  placeholder={String(restoreTarget.vmid)}
                  value={restoreNewVmid}
                  onChange={(e) => setRestoreNewVmid(e.target.value)}
                />
                <p className="mt-1 text-xs text-muted-foreground">{t('backups.new_vmid_hint')}</p>
              </div>
              <label className="flex items-center gap-2">
                <Checkbox
                  checked={restoreStart}
                  onChange={(e) => setRestoreStart(e.target.checked)}
                />
                <span>{t('backups.start_after_restore')}</span>
              </label>
              {detectVmType(restoreTarget.volid) === 'qemu' && (
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={restoreUnique}
                    onChange={(e) => setRestoreUnique(e.target.checked)}
                  />
                  <span>{t('backups.unique_mac')}</span>
                </label>
              )}
            </div>
          )}
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
            <Button onClick={submitRestore} disabled={restoreBackup.isPending}>
              {restoreBackup.isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {t('backups.restore')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Bulk restore dialog */}
      <Dialog open={bulkOpen} onOpenChange={(o) => { if (!o) setBulkOpen(false); }}>
        <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t('backups.bulk_restore_title')}</DialogTitle>
          </DialogHeader>
          <div className="min-w-0 space-y-4 text-sm">
            {/* Mode toggle */}
            <div>
              <Label>{t('backups.bulk_mode')}</Label>
              <div className="mt-1 grid grid-cols-2 gap-2">
                <Button
                  type="button"
                  variant={bulkMode === 'in_place' ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setBulkMode('in_place')}
                >
                  {t('backups.mode_in_place')}
                </Button>
                <Button
                  type="button"
                  variant={bulkMode === 'new_vmid' ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setBulkMode('new_vmid')}
                >
                  {t('backups.mode_new_vmid')}
                </Button>
              </div>
              <p className="mt-1.5 text-xs text-muted-foreground">
                {bulkMode === 'in_place' ? t('backups.mode_in_place_hint') : t('backups.mode_new_vmid_hint')}
              </p>
            </div>

            {/* Selected items */}
            <div>
              <Label>{t('backups.selected_count', { count: selectedBackups.size })}</Label>
              <div className="mt-1 max-h-44 overflow-y-auto overflow-x-hidden rounded-md border divide-y">
                {Array.from(selectedBackups.values()).map((b) => (
                  <div key={b.volid} className="flex items-center justify-between gap-2 px-2.5 py-1.5">
                    <span className="flex items-center gap-2 min-w-0">
                      <Badge variant="outline" className="uppercase text-2xs shrink-0">{detectVmType(b.volid)}</Badge>
                      <span className="font-mono shrink-0">#{b.vmid}</span>
                      <span className="font-mono text-xs text-muted-foreground truncate">{b.volid}</span>
                    </span>
                    <span className="text-xs text-muted-foreground shrink-0">{new Date(b.ctime * 1000).toLocaleDateString()}</span>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <Label>{t('backups.bulk_target_node')}</Label>
              <Select value={bulkTargetNode} onValueChange={v => setBulkTargetNode(v ?? "")}>
                <SelectTrigger className="mt-1 w-full"><SelectValue /></SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  <SelectItem value="__source__">— {t('backups.bulk_target_node_source')}</SelectItem>
                  {nodes.map(n => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
                </SelectContent>
              </Select>
              <p className="mt-1 text-xs text-muted-foreground">{t('backups.bulk_target_node_hint')}</p>
            </div>

            <div>
              <Label>{t('backups.restore_target_storage')}</Label>
              <Select value={bulkTargetStorage} onValueChange={v => setBulkTargetStorage(v ?? "")}>
                <SelectTrigger className="mt-1 w-full"><SelectValue /></SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  <SelectItem value={FROM_BACKUP}>{t('backups.storage_from_backup')}</SelectItem>
                  {diskStoragesFor(bulkTargetNode === '__source__' ? '' : bulkTargetNode, 'any').map(s => (
                    <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="mt-1 text-xs text-muted-foreground">{t('backups.target_storage_hint')}</p>
            </div>

            <label className="flex items-center gap-2">
              <Checkbox checked={bulkStart} onChange={(e) => setBulkStart(e.target.checked)} />
              <span>{t('backups.start_after_restore')}</span>
            </label>

            {/* In-place warning + confirm */}
            {bulkMode === 'in_place' && (
              <div className="space-y-2 rounded-md bg-destructive/10 p-3">
                <p className="text-xs">{t('backups.bulk_in_place_warning')}</p>
                <label className="flex items-center gap-2">
                  <Checkbox checked={bulkConfirm} onChange={(e) => setBulkConfirm(e.target.checked)} />
                  <span>{t('backups.confirm_overwrite')}</span>
                </label>
              </div>
            )}
          </div>
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
            <Button
              onClick={submitBulkRestore}
              disabled={bulkRestore.isPending || (bulkMode === 'in_place' && !bulkConfirm)}
              variant={bulkMode === 'in_place' ? 'destructive' : 'default'}
            >
              {bulkRestore.isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {t('backups.restore_selected')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Job create/edit dialog */}
      <Dialog open={jobDialogOpen} onOpenChange={(o) => { if (!o) setJobDialogOpen(false); }}>
        <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editingJobId != null ? t('backups.edit_job') : t('backups.create_job')}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>{t('backups.server')}</Label>
                <Select
                  value={jobForm.server_id ? String(jobForm.server_id) : ''}
                  onValueChange={(v) => { if (v !== null) setJobForm({ ...jobForm, server_id: Number(v), node: '', storage: '' }); }}
                >
                  <SelectTrigger className="mt-1"><SelectValue placeholder={t('backups.select_server')} /></SelectTrigger>
                  <SelectContent>
                    {servers.map(s => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>{t('backups.node')}</Label>
                <Select
                  value={jobForm.node}
                  onValueChange={(v) => { if (v !== null) setJobForm({ ...jobForm, node: v }); }}
                >
                  <SelectTrigger className="mt-1"><SelectValue placeholder={t('backups.node')} /></SelectTrigger>
                  <SelectContent>
                    {jobNodes.map(n => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="col-span-2">
                <Label>{t('backups.storage')}</Label>
                <Select
                  value={jobForm.storage}
                  onValueChange={(v) => { if (v !== null) setJobForm({ ...jobForm, storage: v }); }}
                >
                  <SelectTrigger className="mt-1"><SelectValue placeholder={t('backups.select_storage')} /></SelectTrigger>
                  <SelectContent>
                    {jobStorages.map(s => <SelectItem key={s.storage} value={s.storage}>{s.storage}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>{t('backups.mode')}</Label>
                <Select value={jobForm.mode} onValueChange={(v) => { if (v !== null) setJobForm({ ...jobForm, mode: v }); }}>
                  <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="snapshot">snapshot</SelectItem>
                    <SelectItem value="suspend">suspend</SelectItem>
                    <SelectItem value="stop">stop</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>{t('backups.compress')}</Label>
                <Select value={jobForm.compress} onValueChange={(v) => { if (v !== null) setJobForm({ ...jobForm, compress: v }); }}>
                  <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="zstd">zstd</SelectItem>
                    <SelectItem value="gzip">gzip</SelectItem>
                    <SelectItem value="lzo">lzo</SelectItem>
                    <SelectItem value="0">{t('backups.none')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <Label>{t('backups.vmids')}</Label>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Checkbox
                    className="accent-amber-500"
                    checked={selectAllVmids}
                    onChange={(e) => { setSelectAllVmids(e.target.checked); if (e.target.checked) setSelectedVmids(new Set()); }}
                  />
                  <span>{t('backups.all_vms')}</span>
                </label>
              </div>
              <div className="rounded-md border">
                <div className="p-2 border-b">
                  <Input
                    placeholder={t('common.search')}
                    value={vmFilter}
                    onChange={(e) => setVmFilter(e.target.value)}
                    disabled={selectAllVmids}
                    className="h-8"
                  />
                </div>
                <div className="max-h-56 overflow-y-auto p-2 space-y-1">
                  {!jobForm.server_id || !jobForm.node ? (
                    <p className="text-xs text-muted-foreground py-3 text-center">{t('backups.pick_server_node_first')}</p>
                  ) : pickerVms.length === 0 ? (
                    <p className="text-xs text-muted-foreground py-3 text-center">{t('backups.no_vms_found')}</p>
                  ) : (
                    <>
                      <div className="flex justify-between text-xs text-muted-foreground px-1 pb-1">
                        <button
                          type="button"
                          className="hover:text-foreground disabled:opacity-50"
                          disabled={selectAllVmids}
                          onClick={() => setSelectedVmids(new Set(pickerVms.map(v => v.vmid)))}
                        >
                          {t('backups.select_all_visible')}
                        </button>
                        <button
                          type="button"
                          className="hover:text-foreground disabled:opacity-50"
                          disabled={selectAllVmids}
                          onClick={() => setSelectedVmids(new Set())}
                        >
                          {t('backups.clear_selection')}
                        </button>
                      </div>
                      {pickerVms.map(vm => {
                        const checked = selectAllVmids || selectedVmids.has(vm.vmid);
                        return (
                          <label
                            key={`${vm.server_id}-${vm.vmid}`}
                            className="flex items-center gap-2 px-2 py-1 rounded hover:bg-muted cursor-pointer text-sm"
                          >
                            <Checkbox
                              className="accent-amber-500"
                              checked={checked}
                              disabled={selectAllVmids}
                              onChange={(e) => {
                                setSelectedVmids(prev => {
                                  const next = new Set(prev);
                                  if (e.target.checked) next.add(vm.vmid);
                                  else next.delete(vm.vmid);
                                  return next;
                                });
                              }}
                            />
                            <Badge variant="outline" className="uppercase text-2xs px-1">{vm.type}</Badge>
                            <span className="font-mono text-xs w-12">{vm.vmid}</span>
                            <span className="truncate">{vm.name || '—'}</span>
                            <span className="ml-auto text-xs text-muted-foreground font-mono">{vm.node}</span>
                          </label>
                        );
                      })}
                    </>
                  )}
                </div>
                <div className="px-2 py-1 border-t text-xs text-muted-foreground">
                  {selectAllVmids
                    ? t('backups.all_vms_selected')
                    : t('backups.selected_count', { count: selectedVmids.size })}
                </div>
              </div>
            </div>

            <div>
              <Label>{t('backups.schedule')} (cron)</Label>
              <div className="mt-1 flex gap-2">
                <Input
                  className="font-mono"
                  value={jobForm.cron_expression}
                  onChange={(e) => setJobForm({ ...jobForm, cron_expression: e.target.value })}
                  placeholder="0 2 * * *"
                />
                <Select value="" onValueChange={(v) => { if (v) setJobForm({ ...jobForm, cron_expression: v }); }}>
                  <SelectTrigger className="w-[180px]"><SelectValue placeholder={t('backups.preset')} /></SelectTrigger>
                  <SelectContent>
                    {CRON_PRESETS.map(p => (
                      <SelectItem key={p.value} value={p.value}>{t(`backups.cron.${p.label}`)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{t('backups.cron_hint')}</p>
            </div>

            <div className="grid grid-cols-4 gap-3 items-end">
              <div>
                <Label className="whitespace-nowrap">keep-last</Label>
                <Input className="mt-1" type="number" min={0} value={jobForm.keep_last ?? 0}
                  onChange={(e) => setJobForm({ ...jobForm, keep_last: Number(e.target.value) })} />
              </div>
              <div>
                <Label className="whitespace-nowrap">keep-daily</Label>
                <Input className="mt-1" type="number" min={0} value={jobForm.keep_daily ?? 0}
                  onChange={(e) => setJobForm({ ...jobForm, keep_daily: Number(e.target.value) })} />
              </div>
              <div>
                <Label className="whitespace-nowrap">keep-weekly</Label>
                <Input className="mt-1" type="number" min={0} value={jobForm.keep_weekly ?? 0}
                  onChange={(e) => setJobForm({ ...jobForm, keep_weekly: Number(e.target.value) })} />
              </div>
              <div>
                <Label className="whitespace-nowrap">keep-monthly</Label>
                <Input className="mt-1" type="number" min={0} value={jobForm.keep_monthly ?? 0}
                  onChange={(e) => setJobForm({ ...jobForm, keep_monthly: Number(e.target.value) })} />
              </div>
            </div>

            <div>
              <Label>{t('backups.notes')}</Label>
              <Input className="mt-1" value={jobForm.notes ?? ''} onChange={(e) => setJobForm({ ...jobForm, notes: e.target.value })} />
            </div>

            <label className="flex items-center gap-2">
              <Checkbox
                checked={!!jobForm.enabled}
                onChange={(e) => setJobForm({ ...jobForm, enabled: e.target.checked })}
              />
              <span>{t('backups.enabled')}</span>
            </label>
          </div>
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
            <Button onClick={submitJob} disabled={createJob.isPending || updateJob.isPending}>
              {(createJob.isPending || updateJob.isPending) && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {editingJobId != null ? t('common.save') : t('common.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Job delete confirmation */}
      <Dialog open={!!deleteJobTarget} onOpenChange={(o) => { if (!o) setDeleteJobTarget(null); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('backups.delete_job_title')}</DialogTitle>
          </DialogHeader>
          {deleteJobTarget && (
            <div className="space-y-3 text-sm">
              <p className="text-muted-foreground">{t('backups.delete_job_confirm')}</p>
              <div className="rounded-md border p-3 space-y-1 bg-muted/40">
                <div><span className="text-muted-foreground">ID:</span> {deleteJobTarget.id}</div>
                <div><span className="text-muted-foreground">{t('backups.schedule')}:</span> <span className="font-mono text-xs">{deleteJobTarget.cron_expression}</span></div>
                <div><span className="text-muted-foreground">{t('backups.vmids')}:</span> <span className="font-mono text-xs">{deleteJobTarget.vmids?.join(', ')}</span></div>
              </div>
            </div>
          )}
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
            <Button variant="destructive" onClick={submitDeleteJob} disabled={deleteJob.isPending}>
              {deleteJob.isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {t('common.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete backup confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => { if (!o) setDeleteTarget(null); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('backups.delete_title')}</DialogTitle>
          </DialogHeader>
          {deleteTarget && (
            <div className="space-y-3 text-sm">
              <p className="text-muted-foreground">{t('backups.delete_confirm')}</p>
              <div className="rounded-md border p-3 space-y-1 bg-muted/40">
                <div><span className="text-muted-foreground">{t('backups.volume')}:</span> <span className="font-mono text-xs break-all">{deleteTarget.volid}</span></div>
                <div><span className="text-muted-foreground">{t('backups.size')}:</span> {formatBytes(deleteTarget.size)}</div>
              </div>
            </div>
          )}
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
            <Button variant="destructive" onClick={submitDelete} disabled={deleteBackup.isPending}>
              {deleteBackup.isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {t('common.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
