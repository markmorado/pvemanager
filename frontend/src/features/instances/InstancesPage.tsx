import { Fragment, useState, useMemo, useEffect, useCallback, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
  useReactTable,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  getFacetedRowModel,
  getFacetedUniqueValues,
  flexRender,
  type ColumnDef,
  type SortingState,
  type RowSelectionState,
  type ColumnFiltersState,
} from '@tanstack/react-table';
import {
  Plus,
  Search,
  Play,
  Square,
  Trash2,
  MoreHorizontal,
  Terminal,
  RotateCcw,
  Camera,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Monitor,
  Container,
  Loader2,
  Power,
  Copy,
  Sparkles,
  KeyRound,
  FileText,
  Disc,
  ArrowDown,
  ArrowUp,
  HardDrive,
  Lock,
  ArrowRightLeft,
  Settings2,
  Archive,
  Cpu,
  ChevronDown,
  ChevronUp,
  Globe,
  Network,
} from 'lucide-react';
import InstanceActionDialogs, { PowerConfirmDialog, type InstanceAction, type PowerAction } from './InstanceActionDialogs';
import BulkMigrateDialog from './BulkMigrateDialog';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { StatusDot } from '@/components/shared/status-dot';
import { ColumnFilter, multiSelectFilter } from '@/components/shared/column-filter';
import { useVirtualMachines, useBulkOperation, usePowerAction, useVMStatusSync, useInstancesMetricsSync, useBulkTasksSync, vmKeys } from '@/hooks/use-instances';
import { useServers } from '@/hooks/use-nodes';
import { useProfile, useMyQuota } from '@/hooks/use-settings';
import { buildQuotaMetrics, exhaustedMetrics } from './quota';
import { formatBytes, vmTypeLabel, formatUptime, formatVmConfig } from '@/lib/format';
import { apiClient } from '@/lib/api-client';
import { useDeployTasksStore } from '@/stores/deploy-tasks-store';
import { useBulkTasksStore } from '@/stores/bulk-tasks-store';
import { useWorkspaceStore } from '@/stores/workspace-store';
import type { VMInstance } from '@/types';
import { toast } from 'sonner';

// ==================== Inline Progress Bar ====================
function InlineProgress({
  value,
  max,
  barClassName = 'w-16',
  textClassName = 'text-xs',
}: {
  value: number;
  max: number;
  barClassName?: string;
  textClassName?: string;
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const color = pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warning' : 'bg-primary';
  return (
    <div className="flex items-center gap-2">
      <div className={`h-1.5 overflow-hidden rounded-full bg-muted ${barClassName}`}>
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`${textClassName} tabular-nums text-muted-foreground`}>{pct.toFixed(0)}%</span>
    </div>
  );
}

// ==================== Circular Progress ====================
function CircularProgress({ value }: { value: number }) {
  const r = 10;
  const circ = 2 * Math.PI * r;
  const offset = circ - (value / 100) * circ;
  return (
    <svg width="28" height="28" viewBox="0 0 28 28" className="shrink-0">
      <circle cx="14" cy="14" r={r} fill="none" stroke="currentColor" strokeWidth="3" className="text-muted" />
      <circle
        cx="14" cy="14" r={r} fill="none"
        stroke="currentColor" strokeWidth="3"
        strokeDasharray={circ} strokeDashoffset={offset}
        strokeLinecap="round"
        className="text-primary transition-all duration-500"
        transform="rotate(-90 14 14)"
      />
    </svg>
  );
}

// Stable row identity: real VMs keyed by server:vmid, ghost rows by task id.
// Without this row.id falls back to the array index, so live data updates
// re-assign selection checkboxes and row menus to a different VM.
const vmRowId = (vm: VMInstance) =>
  vm._deployTaskId ? `ghost:${vm._deployTaskId}` : `${vm.server_id}:${vm.vmid}`;

// ==================== Row Action Menu ====================
function RowActionMenu({ vm }: { vm: VMInstance }) {
  const { t } = useTranslation();
  const power = usePowerAction(vm.server_id!, vm.vmid, vm.type, vm.node);
  const isRunning = vm.status === 'running';
  const isQemu = vm.type === 'qemu';
  const [dialog, setDialog] = useState<InstanceAction>(null);
  const { data: allServers = [] } = useServers();
  const hasRemoteMigrateTargets = allServers.some((s) => s.id !== vm.server_id && !s.use_password);
  const [pendingPower, setPendingPower] = useState<PowerAction | null>(null);

  const handleAction = (action: string) => {
    setPendingPower(action as PowerAction);
  };

  const confirmPower = () => {
    if (!pendingPower) return;
    power.mutate(
      { action: pendingPower },
      {
        onSuccess: () => {
          toast.success(t('console.action_sent'), { description: vm.name || String(vm.vmid) });
          setPendingPower(null);
        },
        onError: (err) => {
          toast.error(err.message);
          setPendingPower(null);
        },
      }
    );
  };

  return (
    <>
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="icon" className="h-8 w-8" />}>
        <MoreHorizontal className="h-4 w-4" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuItem render={<Link to={`/instances/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}&tab=settings`} />}>
            <Settings2 className="mr-2 h-4 w-4" /> {t('instances.parameters', 'Параметры')}
        </DropdownMenuItem>
        <DropdownMenuItem render={<Link to={`/console/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}`} target="_blank" rel="noopener noreferrer" />}>
            <Terminal className="mr-2 h-4 w-4" /> {t('common.console', 'Console')}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {!isRunning ? (
          <DropdownMenuItem onClick={() => handleAction('start')}>
            <Play className="mr-2 h-4 w-4" /> {t('common.start', 'Start')}
          </DropdownMenuItem>
        ) : (
          <>
            <DropdownMenuItem onClick={() => handleAction('restart')}>
              <RotateCcw className="mr-2 h-4 w-4" /> {t('common.restart', 'Restart')}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => handleAction('shutdown')}>
              <Power className="mr-2 h-4 w-4" /> {t('common.shutdown', 'Shutdown')}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => handleAction('stop')}>
              <Square className="mr-2 h-4 w-4" /> {t('common.stop', 'Stop')}
            </DropdownMenuItem>
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => setDialog('migrate')}>
          <ArrowRightLeft className="mr-2 h-4 w-4" /> {t('instances.migrate', 'Мигрировать')}
        </DropdownMenuItem>
        {hasRemoteMigrateTargets && (
          <DropdownMenuItem onClick={() => setDialog('remote-migrate')}>
            <ArrowRightLeft className="mr-2 h-4 w-4" /> {t('instances.remote_migrate', 'Миграция на другой кластер')}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onClick={() => setDialog('clone')}>
          <Copy className="mr-2 h-4 w-4" /> {t('common.clone', 'Клонировать')}
        </DropdownMenuItem>
        <DropdownMenuItem render={<Link to={`/instances/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}&tab=${isQemu ? 'hardware' : 'settings'}`} />}>
            <Cpu className="mr-2 h-4 w-4" /> {t('instances.edit_resources', 'Изменить ресурсы')}
        </DropdownMenuItem>
        {isQemu && (
          <DropdownMenuItem onClick={() => setDialog('iso')}>
            <Disc className="mr-2 h-4 w-4" /> {t('common.iso', 'ISO образ')}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onClick={() => setDialog('change-password')}>
          <KeyRound className="mr-2 h-4 w-4" /> {t('common.changePassword', 'Изменить пароль')}
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => setDialog('notes')}>
          <FileText className="mr-2 h-4 w-4" /> {t('common.notes', 'Примечание')}
        </DropdownMenuItem>
        {isQemu && (
          <DropdownMenuItem onClick={() => setDialog('execute')}>
            <Terminal className="mr-2 h-4 w-4" /> {t('common.runCommand', 'Запуск команд')}
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => setDialog('backup')}>
          <Archive className="mr-2 h-4 w-4" /> {t('instances.backup_now', 'Создать резервную копию')}
        </DropdownMenuItem>
        <DropdownMenuItem render={<Link to={`/instances/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}&tab=snapshots`} />}>
            <Camera className="mr-2 h-4 w-4" /> {t('common.snapshots', 'Snapshots')}
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => setDialog('reinstall')}>
          <Sparkles className="mr-2 h-4 w-4" /> {t('common.reinstall', 'Переустановить')}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          render={<Link to={`/instances/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}&tab=destroy`} />}
        >
            <Trash2 className="mr-2 h-4 w-4" /> {t('common.delete', 'Удалить')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    <InstanceActionDialogs
      open={dialog}
      onOpenChange={setDialog}
      serverId={vm.server_id!}
      vmid={vm.vmid}
      type={vm.type}
      node={vm.node}
      name={vm.name}
      description={vm.description}
    />
    <PowerConfirmDialog
      open={pendingPower !== null}
      onClose={() => setPendingPower(null)}
      onConfirm={confirmPower}
      action={pendingPower}
      vmName={vm.name}
      isPending={power.isPending}
    />
    </>
  );
}

// ==================== Metric row (label + progress bar + detail) ====================
function MetricRow({ label, value, max, detail }: { label: string; value: number | null | undefined; max: number; detail: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-16 shrink-0 text-sm font-medium text-muted-foreground">{label}</span>
      {value != null && max ? (
        <InlineProgress value={value} max={max} barClassName="w-32 sm:w-44" textClassName="text-sm" />
      ) : (
        <span className="text-sm text-muted-foreground">—</span>
      )}
      <span className="text-sm text-muted-foreground">{detail}</span>
    </div>
  );
}

// ==================== Info item (label + value) ====================
function InfoItem({ label, value, mono, icon: Icon }: { label: string; value: string; mono?: boolean; icon?: typeof Globe }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`flex items-center gap-1 ${mono ? 'font-mono text-sm' : 'text-sm'}`}>
        {Icon && <Icon className="h-3 w-3 shrink-0 text-muted-foreground" />}
        {value}
      </div>
    </div>
  );
}

// ==================== Expandable instance metrics panel ====================
function InstanceMetricsPanel({ vm, nodeLabelName }: { vm: VMInstance; nodeLabelName: string }) {
  const { t } = useTranslation();
  const isQemu = vm.type === 'qemu';
  const owner =
    vm.owner_user?.email || vm.owner_user?.full_name || vm.owner_user?.username || vm.owner || '—';
  const os = vm.os || vm.os_template || '—';
  const isRunning = vm.status === 'running';

  return (
    <div className="border-t bg-muted/30">
      <div className="flex flex-wrap items-center gap-4 border-b px-4 py-2 text-sm">
        <Link
          to={`/instances/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}&tab=settings`}
          className="flex items-center gap-1.5 text-primary hover:underline"
        >
          <Settings2 className="h-3.5 w-3.5" /> {t('instances.parameters', 'Параметры')}
        </Link>
        <Link
          to={`/console/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}`}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
        >
          <Terminal className="h-3.5 w-3.5" /> {t('common.console', 'Console')}
        </Link>
      </div>
      <div className="grid gap-6 p-4 sm:grid-cols-2">
        <div className="space-y-2.5">
          <MetricRow
            label="vCPU"
            value={vm.cpu != null ? vm.cpu * 100 : null}
            max={100}
            detail={vm.cores ? `${vm.cores} vCPU` : '—'}
          />
          <MetricRow
            label="RAM"
            value={vm.mem ?? null}
            max={vm.maxmem || 0}
            detail={vm.mem != null && vm.maxmem ? `${formatBytes(vm.mem)} / ${formatBytes(vm.maxmem)}` : '—'}
          />
          {!isQemu && (
            <MetricRow
              label="Storage"
              value={vm.disk ?? null}
              max={vm.maxdisk || 0}
              detail={vm.disk != null && vm.maxdisk ? `${formatBytes(vm.disk)} / ${formatBytes(vm.maxdisk)}` : '—'}
            />
          )}
        </div>
        <div className="grid grid-cols-2 content-start gap-x-6 gap-y-3">
          <InfoItem label={t('instances.ip_address', 'IP-адрес')} value={vm.ip_address || '—'} mono icon={Globe} />
          <InfoItem label={t('common.node', 'Узел')} value={nodeLabelName} />
          <InfoItem label={t('instances.os_config', 'ОС')} value={os} />
          <InfoItem label={t('instances.owner', 'Владелец')} value={owner} />
          <InfoItem label={t('instances.uptime', 'Аптайм')} value={vm.uptime ? formatUptime(vm.uptime) : '—'} />
        </div>
      </div>
      {isRunning && (
        <div className="grid gap-3 border-t px-4 py-4 sm:grid-cols-2">
          <IoStatCard
            icon={Network}
            label={t('instances.network', 'Сеть')}
            rows={[
              { icon: ArrowDown, iconClass: 'text-primary', label: t('instances.net_in', 'Входящий'), value: vm.netin_rate },
              { icon: ArrowUp, iconClass: 'text-violet-500', label: t('instances.net_out', 'Исходящий'), value: vm.netout_rate },
            ]}
          />
          <IoStatCard
            icon={HardDrive}
            label={t('instances.disk_io', 'Диск I/O')}
            rows={[
              { badge: 'R', badgeClass: 'text-cyan-500', label: t('instances.disk_read', 'Чтение'), value: vm.diskread_rate },
              { badge: 'W', badgeClass: 'text-orange-500', label: t('instances.disk_write', 'Запись'), value: vm.diskwrite_rate },
            ]}
          />
        </div>
      )}
    </div>
  );
}

// ==================== I/O stat card (network / disk throughput) ====================
type IoStatRow = {
  label: string;
  value?: number;
  icon?: typeof ArrowDown;
  iconClass?: string;
  badge?: string;
  badgeClass?: string;
};

function IoStatCard({ icon: Icon, label, rows }: { icon: typeof Network; label: string; rows: IoStatRow[] }) {
  return (
    <div className="rounded-lg border bg-background p-3">
      <div className="mb-2.5 flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <Icon className="h-4 w-4" /> {label}
      </div>
      <div className="grid grid-cols-2 gap-3">
        {rows.map((row) => (
          <div key={row.label} className="flex items-center gap-2">
            {row.icon ? (
              <row.icon className={`h-4 w-4 shrink-0 ${row.iconClass}`} />
            ) : (
              <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-sm bg-muted text-[10px] font-bold ${row.badgeClass}`}>
                {row.badge}
              </span>
            )}
            <div className="leading-tight">
              <div className="text-sm font-semibold tabular-nums">
                {row.value != null ? `${formatBytes(row.value)}/s` : '—'}
              </div>
              <div className="text-2xs text-muted-foreground">{row.label}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ==================== Instances Page ====================
export default function InstancesPage() {
  const { t } = useTranslation();
  const { data: vms, isLoading } = useVirtualMachines();
  useVMStatusSync();
  useBulkTasksSync();
  const metricsMap = useInstancesMetricsSync();
  const bulkOp = useBulkOperation();
  const bulkTasks = useBulkTasksStore((s) => s.tasks);
  const addBulkTask = useBulkTasksStore((s) => s.addTask);
  const [migrateOpen, setMigrateOpen] = useState(false);

  // Human-readable label for an in-flight bulk action (status overlay / progress).
  const bulkActionLabel = useCallback((action: string) => {
    switch (action) {
      case 'start': return t('instances.status_starting', 'Запуск');
      case 'stop': return t('instances.status_stopping', 'Остановка');
      case 'restart': return t('instances.status_restarting', 'Перезагрузка');
      case 'shutdown': return t('instances.status_shutting_down', 'Выключение');
      case 'delete': return t('instances.status_deleting', 'Удаление');
      case 'migrate': return t('instances.status_migrating', 'Миграция');
      default: return action;
    }
  }, [t]);

  // ── Deploy task polling ──────────────────────────────────────────
  const qc = useQueryClient();
  const { tasks: deployTasks, updateTask, removeTask } = useDeployTasksStore();
  const activeTasks = deployTasks.filter((t) => t.status === 'pending' || t.status === 'running');

  useEffect(() => {
    if (activeTasks.length === 0) return;
    const poll = async () => {
      for (const task of activeTasks) {
        try {
          const data = await apiClient.get<{
            id: number; status: string; step: string | null; progress: number;
            vmid: number | null; node: string | null; error_message: string | null;
          }>(`/templates/api/deploy/${task.id}`);
          updateTask(task.id, {
            status: data.status as 'pending' | 'running' | 'completed' | 'failed',
            step: data.step,
            progress: data.progress,
            vmid: data.vmid ?? undefined,
            node: data.node ?? undefined,
            error_message: data.error_message ?? undefined,
          });
          if (data.status === 'completed') {
            if (data.error_message) {
              toast.warning(
                t('instances.deploy_completed_with_warnings', 'ВМ создана, но часть настроек не применилась'),
                { description: data.error_message, duration: 15000 },
              );
            }
            // Новая VM уже сохранена в кэше бэкендом — подтягиваем список сразу,
            // не дожидаясь фонового vm_created от 10-секундного sync_vm_cache.
            qc.invalidateQueries({ queryKey: vmKeys.all });
            qc.invalidateQueries({ queryKey: vmKeys.resourcesAll });
            setTimeout(() => removeTask(task.id), 5000);
          }
        } catch (e) {
          // Persisted task the backend no longer has (or another user's after
          // re-login): drop it instead of polling it every 2 s forever.
          const status = (e as { status?: number }).status;
          if (status === 404 || status === 403) removeTask(task.id);
        }
      }
    };
    poll();
    const timer = setInterval(poll, 2000);
    return () => clearInterval(timer);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTasks.length, activeTasks.map((t) => t.id).join(',')]);

  const { data: servers = [] } = useServers();
  const { data: profile } = useProfile();

  // Квота исчерпана — мастер создания всё равно откажется открываться, так что
  // не ведём туда пользователя вхолостую. Условие то же, что в мастере:
  // размер будущего инстанса не важен (сравниваем использовано/лимит), а на
  // админов не распространяется — их лимит не про инстанс, который они создают
  // другому пользователю.
  const { data: myQuota } = useMyQuota();
  const quotaBlockers = profile?.is_admin
    ? []
    : exhaustedMetrics(buildQuotaMetrics(myQuota, { cores: 0, memoryMb: 0, diskGb: 0 }));

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [serverFilter, setServerFilter] = useState<string>(() => {
    return localStorage.getItem('instances-server-filter') ?? 'all';
  });
  const [nodeFilter, setNodeFilter] = useState<string>(() => {
    return localStorage.getItem('instances-node-filter') ?? 'all';
  });
  const [typeTab, setTypeTab] = useState<string>('all');
  const [sorting, setSorting] = useState<SortingState>(() => {
    try {
      const raw = localStorage.getItem('instances-sorting');
      return raw ? (JSON.parse(raw) as SortingState) : [];
    } catch {
      return [];
    }
  });
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>(() => {
    try {
      const raw = localStorage.getItem('instances-column-filters');
      return raw ? (JSON.parse(raw) as ColumnFiltersState) : [];
    } catch {
      return [];
    }
  });
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const toggleExpand = useCallback((rowId: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(rowId)) next.delete(rowId);
      else next.add(rowId);
      return next;
    });
  }, []);

  // Reset every filter that can reference a server/node identity when the
  // active workspace changes — those values belong to whatever workspace was
  // active when they were picked, and silently keeping them can filter the
  // new workspace's (now-scoped) VM list down to nothing.
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  // Compares against the previous *value* (not a one-shot "first render" flag)
  // so React 18 StrictMode's dev-only double effect invocation on mount can't
  // misfire this as a real workspace change.
  const prevWorkspaceIdRef = useRef(activeWorkspaceId);
  useEffect(() => {
    if (prevWorkspaceIdRef.current === activeWorkspaceId) return;
    prevWorkspaceIdRef.current = activeWorkspaceId;
    setServerFilter('all');
    localStorage.setItem('instances-server-filter', 'all');
    setNodeFilter('all');
    localStorage.setItem('instances-node-filter', 'all');
    setColumnFilters([]);
    setRowSelection({});
  }, [activeWorkspaceId]);

  // Persist sorting + column filters across reloads
  useEffect(() => {
    localStorage.setItem('instances-sorting', JSON.stringify(sorting));
  }, [sorting]);
  useEffect(() => {
    localStorage.setItem('instances-column-filters', JSON.stringify(columnFilters));
  }, [columnFilters]);

  // Drop a stale persisted server filter when its server is not in the active
  // workspace anymore (otherwise the list silently empties and the Select shows
  // the raw server id instead of a name).
  useEffect(() => {
    if (serverFilter !== 'all' && servers.length > 0 && !servers.some((s) => String(s.id) === serverFilter)) {
      setServerFilter('all');
      localStorage.setItem('instances-server-filter', 'all');
    }
  }, [servers, serverFilter]);

  // Filter real VMs
  const filteredVMs = useMemo(() => {
    if (!vms) return [];
    return vms.filter((vm) => {
      if (typeTab === 'vm' && vm.type !== 'qemu') return false;
      if (typeTab === 'lxc' && vm.type !== 'lxc') return false;
      if (statusFilter !== 'all' && vm.status !== statusFilter) return false;
      if (serverFilter !== 'all' && vm.server_id !== Number(serverFilter)) return false;
      if (nodeFilter !== 'all' && vm.node !== nodeFilter) return false;
      if (search) {
        const q = search.toLowerCase();
        const name = (vm.name || '').toLowerCase();
        const ip = (vm.ip_address || '').toLowerCase();
        const vmid = String(vm.vmid);
        if (!name.includes(q) && !ip.includes(q) && !vmid.includes(q)) return false;
      }
      return true;
    });
  }, [vms, typeTab, statusFilter, serverFilter, nodeFilter, search]);

  // Ghost rows from active deploy/clone tasks (prepended before real VMs).
  // - reinstall / change_password tasks are NOT shown as ghosts (they live as overlays on real rows)
  // - clone/deploy tasks whose vmid is already present in real list are skipped (de-dupe)
  const ghostRows = useMemo<VMInstance[]>(() => {
    const realKey = new Set((vms ?? []).map((v) => `${v.server_id}:${v.vmid}`));
    return deployTasks
      .filter((t) => t.status === 'pending' || t.status === 'running')
      .filter((t) => {
        const k = t.kind ?? 'deploy';
        if (k !== 'deploy' && k !== 'clone') return false;
        if (t.vmid && t.server_id && realKey.has(`${t.server_id}:${t.vmid}`)) return false;
        return true;
      })
      .map((t) => ({
        vmid: t.vmid ?? 0,
        name: t.name,
        status: 'creating' as const,
        type: 'qemu' as const,
        node: t.node ?? '...',
        server_id: undefined,
        _deployTaskId: t.id,
      }));
  }, [deployTasks, vms]);

  // Map (server_id:vmid) -> active reinstall/change_password task (for inline overlay)
  const overlayByVm = useMemo(() => {
    const m = new Map<string, typeof deployTasks[number]>();
    for (const t of deployTasks) {
      if (t.status !== 'pending' && t.status !== 'running') continue;
      const k = t.kind ?? 'deploy';
      if (k !== 'reinstall' && k !== 'change_password') continue;
      if (!t.vmid || !t.server_id) continue;
      m.set(`${t.server_id}:${t.vmid}`, t);
    }
    return m;
  }, [deployTasks]);

  // Map (server_id:vmid) -> in-flight bulk action for rows being processed by an
  // active bulk task (start/stop/restart/...). An item drops out of the overlay
  // once it shows up in the task's results (then the row reflects the live status).
  const bulkOverlayByVm = useMemo(() => {
    const m = new Map<string, { action: string }>();
    for (const task of bulkTasks) {
      if (task.status !== 'pending' && task.status !== 'running') continue;
      const done = new Set(task.results.map((r) => `${r.server_id}:${r.vmid}`));
      for (const it of task.items) {
        const k = `${it.server_id}:${it.vmid}`;
        if (!done.has(k)) m.set(k, { action: task.action });
      }
    }
    return m;
  }, [bulkTasks]);

  const allRows = useMemo<VMInstance[]>(() => [...ghostRows, ...filteredVMs], [ghostRows, filteredVMs]);

  // Merge live WS metrics into rows for real-time rendering
  const liveRows = useMemo<VMInstance[]>(() => {
    if (!metricsMap.size) return allRows;
    return allRows.map((vm) => {
      const live = metricsMap.get(`${vm.server_id}:${vm.vmid}`);
      if (!live) return vm;
      return {
        ...vm,
        status: (live.status as VMInstance['status']) ?? vm.status,
        cpu: live.cpu ?? vm.cpu,
        mem: live.mem ?? vm.mem,
        maxmem: live.maxmem ?? vm.maxmem,
        disk: live.disk ?? vm.disk,
        maxdisk: live.maxdisk ?? vm.maxdisk,
        uptime: live.uptime ?? vm.uptime,
        netin: live.netin ?? vm.netin,
        netout: live.netout ?? vm.netout,
        diskread: live.diskread ?? vm.diskread,
        diskwrite: live.diskwrite ?? vm.diskwrite,
        diskread_rate: live.diskread_rate,
        diskwrite_rate: live.diskwrite_rate,
        netin_rate: live.netin_rate,
        netout_rate: live.netout_rate,
      };
    });
  }, [allRows, metricsMap]);

  // Unique nodes for filter — scoped to selected server
  const uniqueNodes = useMemo(() => {
    if (!vms) return [];
    const source = serverFilter !== 'all'
      ? vms.filter((v) => v.server_id === Number(serverFilter))
      : vms;
    return [...new Set(source.map((v) => v.node))].sort();
  }, [vms, serverFilter]);

  // The "node" column shows the panel-side server name (the name entered in the
  // add-server form), not the Proxmox node hostname. server_id → server.name is
  // the exact mapping; nodeName → server.name is a best-effort fallback used by
  // the filter UI (assumes one node per server, which holds for option-2 setups).
  const serverNameById = useMemo(() => {
    const m = new Map<number, string>();
    for (const s of servers) m.set(s.id, s.name);
    return m;
  }, [servers]);

  const nodeLabel = useMemo(() => {
    const m = new Map<string, string>();
    for (const v of vms ?? []) {
      if (v.node && v.server_id != null) {
        const sn = serverNameById.get(v.server_id);
        if (sn) m.set(v.node, sn);
      }
    }
    return m;
  }, [vms, serverNameById]);

  // Drop a stale persisted node filter when that node is gone from the active
  // workspace / selected server (same failure mode as the server filter above).
  useEffect(() => {
    if (nodeFilter !== 'all' && vms && vms.length > 0 && !uniqueNodes.includes(nodeFilter)) {
      setNodeFilter('all');
      localStorage.setItem('instances-node-filter', 'all');
    }
  }, [uniqueNodes, nodeFilter, vms]);

  // Counts (ghosts not counted in tabs)
  const counts = useMemo(() => {
    if (!vms) return { all: 0, vm: 0, lxc: 0 };
    return {
      all: vms.length,
      vm: vms.filter((v) => v.type === 'qemu').length,
      lxc: vms.filter((v) => v.type === 'lxc').length,
    };
  }, [vms]);

  // Selected real VMs (skip ghost rows by checking _deployTaskId)
  const selectedVMs = useMemo(() => {
    const byId = new Map(allRows.map((vm) => [vmRowId(vm), vm]));
    return Object.keys(rowSelection)
      .filter((k) => rowSelection[k])
      .map((id) => byId.get(id))
      .filter((vm): vm is VMInstance => !!vm && !vm._deployTaskId);
  }, [rowSelection, allRows]);

  const handleBulk = (action: string) => {
    if (!selectedVMs.length) return;
    const items = selectedVMs.map((vm) => ({
      server_id: vm.server_id!,
      vmid: vm.vmid,
      vm_type: vm.type,
      name: vm.name || String(vm.vmid),
      node: vm.node,
    }));
    bulkOp.mutate(
      { action, items },
      {
        onSuccess: (data) => {
          // Register the task so the list can track its progress via WS
          // (useBulkTasksSync), the same way instance creation is tracked.
          if (data?.task_id) {
            addBulkTask({
              id: data.task_id,
              action,
              items: items.map((i) => ({ server_id: i.server_id, vmid: i.vmid, name: i.name })),
              status: 'pending',
              total: items.length,
              completed: 0,
              failed: 0,
              results: [],
            });
          }
          toast.success(`${bulkActionLabel(action)}: ${items.length}`);
          setRowSelection({});
        },
        onError: (err) => toast.error(err.message),
      }
    );
  };

  // Миграция возможна только внутри одного сервера/кластера.
  const bulkServerId = useMemo(() => {
    const ids = new Set(selectedVMs.map((vm) => vm.server_id));
    return ids.size === 1 ? selectedVMs[0]?.server_id ?? null : null;
  }, [selectedVMs]);
  const bulkSourceNodes = useMemo(
    () => Array.from(new Set(selectedVMs.map((vm) => vm.node))),
    [selectedVMs],
  );

  const handleBulkMigrate = (targetNode: string, online: boolean) => {
    // Пропускаем инстансы, уже находящиеся на целевой ноде.
    const toMigrate = selectedVMs.filter((vm) => vm.node !== targetNode);
    if (!toMigrate.length) {
      toast.info(t('instances.nothing_to_migrate', 'Все выбранные инстансы уже на целевой ноде'));
      setMigrateOpen(false);
      return;
    }
    const items = toMigrate.map((vm) => ({
      server_id: vm.server_id!,
      vmid: vm.vmid,
      vm_type: vm.type,
      name: vm.name || String(vm.vmid),
      node: vm.node,
      target_node: targetNode,
      online: online && vm.status === 'running',
    }));
    bulkOp.mutate(
      { action: 'migrate', items },
      {
        onSuccess: (data) => {
          if (data?.task_id) {
            addBulkTask({
              id: data.task_id,
              action: 'migrate',
              items: items.map((i) => ({ server_id: i.server_id, vmid: i.vmid, name: i.name })),
              status: 'pending',
              total: items.length,
              completed: 0,
              failed: 0,
              results: [],
            });
          }
          toast.success(`${bulkActionLabel('migrate')}: ${items.length}`);
          setMigrateOpen(false);
          setRowSelection({});
        },
        onError: (err) => toast.error(err.message),
      }
    );
  };

  // ==================== Table Columns ====================
  // Volatile lookups are read inside cells through a ref so the `columns`
  // identity stays stable. flexRender treats inline `cell` functions as React
  // component types, so a new columns array remounts EVERY cell — which closes
  // an open row dropdown menu the moment vms/servers/tasks data refreshes.
  // Cells still see fresh values: any store/query update re-renders the table.
  const cellCtx = { deployTasks, overlayByVm, bulkOverlayByVm, serverNameById, nodeLabel, expandedRows, toggleExpand };
  const cellCtxRef = useRef(cellCtx);
  cellCtxRef.current = cellCtx;

  const columns = useMemo<ColumnDef<VMInstance>[]>(
    () => [
      {
        id: 'select',
        header: ({ table }) => (
          <Checkbox
            checked={table.getIsAllPageRowsSelected()}
            onChange={table.getToggleAllPageRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => {
          const isGhost = !!row.original._deployTaskId;
          if (isGhost) return null;
          return (
            <Checkbox
              checked={row.getIsSelected()}
              onChange={row.getToggleSelectedHandler()}
            />
          );
        },
        size: 32,
        enableSorting: false,
      },
      {
        accessorKey: 'name',
        filterFn: 'includesString',
        meta: { filter: 'text' },
        header: ({ column }) => (
          <Button
            variant="ghost"
            size="sm"
            className="-ml-3 h-8"
            onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')}
          >
            {t('common.name', 'Name')}
            <ArrowUpDown className="ml-1 h-3 w-3" />
          </Button>
        ),
        cell: ({ row }) => {
          const vm = row.original;
          const isGhost = !!vm._deployTaskId;
          if (isGhost) {
            const task = cellCtxRef.current.deployTasks.find((t) => t.id === vm._deployTaskId);
            return (
              <div className="flex items-center gap-2">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary shrink-0" />
                <div>
                  <span className="font-medium text-muted-foreground">{vm.name || 'New VM'}</span>
                  <span className="ml-2 text-xs text-muted-foreground">
                    {task?.node ? `· ${task.node}` : ''}
                  </span>
                </div>
              </div>
            );
          }
          const rowId = vmRowId(vm);
          const isExpanded = cellCtxRef.current.expandedRows.has(rowId);
          return (
            <button
              type="button"
              onClick={() => cellCtxRef.current.toggleExpand(rowId)}
              className="group flex items-center gap-2 text-left"
              aria-expanded={isExpanded}
            >
              {isExpanded ? (
                <ChevronUp className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              )}
              <StatusDot status={vm.status} pulse />
              <div>
                <span className="font-medium text-foreground group-hover:text-primary transition-colors">
                  {vm.name || `VM ${vm.vmid}`}
                </span>
                <span className="ml-2 text-xs text-muted-foreground">#{vm.vmid}</span>
              </div>
            </button>
          );
        },
      },
      {
        accessorKey: 'status',
        filterFn: multiSelectFilter,
        meta: { filter: 'select' },
        header: t('common.status', 'Status'),
        cell: ({ row }) => {
          const vm = row.original;
          const isGhost = !!vm._deployTaskId;
          if (isGhost) {
            const task = cellCtxRef.current.deployTasks.find((t) => t.id === vm._deployTaskId);
            const progress = task?.progress ?? 0;
            return (
              <div className="flex items-center gap-2">
                <CircularProgress value={progress} />
                <Badge variant="secondary" className="bg-primary/10 text-primary text-2xs">
                  {t('instances.status_creating', 'Creating')}
                  {progress > 0 ? ` ${progress}%` : ''}
                </Badge>
              </div>
            );
          }
          const status = vm.status;
          const bulkOv = vm.server_id != null ? cellCtxRef.current.bulkOverlayByVm.get(`${vm.server_id}:${vm.vmid}`) : undefined;
          if (bulkOv) {
            return (
              <div className="flex items-center gap-2">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary shrink-0" />
                <Badge variant="secondary" className="bg-primary/10 text-primary text-2xs">
                  {bulkActionLabel(bulkOv.action)}
                </Badge>
              </div>
            );
          }
          const overlay = vm.server_id != null ? cellCtxRef.current.overlayByVm.get(`${vm.server_id}:${vm.vmid}`) : undefined;
          if (overlay) {
            const label = overlay.kind === 'reinstall'
              ? t('instances.status_reinstalling', 'Reinstalling')
              : t('instances.status_changing_password', 'Changing password');
            return (
              <div className="flex items-center gap-2">
                <CircularProgress value={overlay.progress ?? 0} />
                <Badge variant="secondary" className="bg-warning/10 text-warning text-2xs">
                  {label}{(overlay.progress ?? 0) > 0 ? ` ${overlay.progress}%` : ''}
                </Badge>
              </div>
            );
          }
          return (
            <div className="flex items-center gap-1.5">
              <Badge
                variant="secondary"
                className={status === 'running' ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}
              >
                {status}
              </Badge>
              {vm.lock && (
                <Badge
                  variant="secondary"
                  className="bg-warning/10 text-warning text-2xs gap-1"
                  title={t('instances.locked_hint', 'Заблокировано Proxmox — операция выполняется')}
                >
                  <Lock className="h-3 w-3" />
                  {vm.lock}
                </Badge>
              )}
            </div>
          );
        },
        size: 160,
      },
      {
        accessorKey: 'node',
        filterFn: multiSelectFilter,
        meta: { filter: 'select', formatOption: (v: string) => cellCtxRef.current.nodeLabel.get(v) ?? v },
        header: t('common.node', 'Node'),
        cell: ({ row }) => {
          const vm = row.original;
          const label =
            (vm.server_id != null ? cellCtxRef.current.serverNameById.get(vm.server_id) : undefined) ?? vm.node;
          return <span className="text-muted-foreground">{label}</span>;
        },
        size: 100,
      },
      {
        accessorKey: 'type',
        filterFn: multiSelectFilter,
        meta: { filter: 'select', formatOption: (v: string) => vmTypeLabel(v) },
        header: t('common.type', 'Type'),
        cell: ({ getValue }) => {
          const type = getValue<string>();
          const Icon = type === 'qemu' ? Monitor : Container;
          return (
            <Tooltip>
              <TooltipTrigger render={<span />}>
                <Badge variant="outline" className="gap-1 font-normal">
                  <Icon className="h-3 w-3" />
                  {vmTypeLabel(type)}
                </Badge>
              </TooltipTrigger>
              <TooltipContent>{type === 'qemu' ? 'QEMU Virtual Machine' : 'LXC Container'}</TooltipContent>
            </Tooltip>
          );
        },
        size: 80,
      },
      {
        id: 'ip',
        // Поиск идёт по всем адресам гостя, а показывается основной
        accessorFn: (vm) => [vm.ip_address, ...(vm.ips ?? []).map((a) => a.ip)]
          .filter(Boolean)
          .join(' '),
        filterFn: 'includesString',
        meta: { filter: 'text' },
        header: 'IP',
        cell: ({ row }) => {
          const vm = row.original;
          const primary = vm.ip_address;
          const extra = (vm.ips ?? []).filter((a) => a.ip !== primary);
          if (!primary) {
            return <span className="text-xs text-muted-foreground">—</span>;
          }
          return (
            <span className="flex items-center gap-1.5">
              <span className="font-mono text-xs">{primary}</span>
              {extra.length > 0 && (
                <Badge
                  variant="secondary"
                  className="text-2xs"
                  title={extra.map((a) => a.ip).join(', ')}
                >
                  {t('ipam.extra_addresses', { defaultValue: 'ещё {{count}}', count: extra.length })}
                </Badge>
              )}
            </span>
          );
        },
        size: 130,
      },
      {
        id: 'owner',
        accessorFn: (vm) =>
          vm.owner_user?.email ||
          vm.owner_user?.full_name ||
          vm.owner_user?.username ||
          vm.owner ||
          '',
        filterFn: 'includesString',
        meta: { filter: 'text' },
        header: t('instances.owner', 'Owner'),
        cell: ({ getValue }) => {
          const owner = getValue<string>();
          return owner ? (
            <span className="text-xs">{owner}</span>
          ) : (
            <span className="text-xs text-muted-foreground">—</span>
          );
        },
        size: 180,
      },
      {
        id: 'os',
        accessorFn: (vm) => vm.os || vm.os_template || '',
        filterFn: multiSelectFilter,
        meta: { filter: 'select' },
        header: t('instances.os_config', 'OS / Config'),
        cell: ({ row }) => {
          const vm = row.original;
          if (vm._deployTaskId) return <span className="text-xs text-muted-foreground">—</span>;
          const os = vm.os || vm.os_template;
          const config = formatVmConfig(vm);
          return (
            <div className="leading-tight">
              <div className="text-sm">{os || '—'}</div>
              {config && <div className="text-xs text-muted-foreground">{config}</div>}
            </div>
          );
        },
        size: 200,
      },
      {
        id: 'actions',
        cell: ({ row }) => {
          if (row.original._deployTaskId) return null;
          return <RowActionMenu vm={row.original} />;
        },
        size: 48,
        enableSorting: false,
      },
    ],
    // Volatile data is read via cellCtxRef (see above) — only l10n identities here.
    [t, bulkActionLabel]
  );

  // ==================== TanStack Table ====================
  const table = useReactTable({
    data: liveRows,
    columns,
    state: { sorting, rowSelection, columnFilters },
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    onColumnFiltersChange: setColumnFilters,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getFacetedRowModel: getFacetedRowModel(),
    getFacetedUniqueValues: getFacetedUniqueValues(),
    initialState: { pagination: { pageSize: 25 } },
    // Stable row identity across live data updates: keeps selection and open
    // row menus attached to the same VM when rows are re-sorted or refreshed.
    getRowId: vmRowId,
    // Live WS metrics replace `data` every second; without this the table
    // would snap back to page 1 on every tick.
    autoResetPageIndex: false,
  });

  // Filters can shrink the row set below the current page — go back to page 1
  // only when the user actually changes filtering.
  useEffect(() => {
    table.setPageIndex(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [columnFilters, search, typeTab, statusFilter, serverFilter, nodeFilter]);

  // ==================== Render ====================
  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-[22px] font-semibold">{t('nav.instances', 'Instances')}</h1>
        <div className="flex items-center gap-2">
          {profile?.is_admin && (
            <Button render={<Link to="/instances/snapshot-archives" />} size="sm" variant="outline">
              <Camera className="mr-1.5 h-4 w-4" />
              {t('snap_archive.title')}
            </Button>
          )}
          {quotaBlockers.length > 0 ? (
            // Кнопка-ссылка с disabled всё равно навигирует (<a> не знает про
            // disabled), поэтому при исчерпанной квоте рендерим обычную кнопку
            // без Link, а причину показываем в подсказке.
            <Tooltip>
              <TooltipTrigger render={<span />}>
                <Button size="sm" disabled>
                  <Plus className="mr-1.5 h-4 w-4" />
                  {t('instances.create', 'Create Instance')}
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                <p>{t('wizard.quota_blocked_title')}</p>
                <ul className="mt-1">
                  {quotaBlockers.map(m => (
                    <li key={m.key}>
                      {t(m.labelKey)}: {t('wizard.quota_used_of', { used: m.used, limit: m.limit })}
                    </li>
                  ))}
                </ul>
              </TooltipContent>
            </Tooltip>
          ) : (
            <Button render={<Link to="/instances/create" />} size="sm">
              <Plus className="mr-1.5 h-4 w-4" />
              {t('instances.create', 'Create Instance')}
            </Button>
          )}
        </div>
      </div>

      {/* Type Tabs */}
      <Tabs value={typeTab} onValueChange={setTypeTab}>
        <TabsList>
          <TabsTrigger value="all">
            All <Badge variant="secondary" className="ml-1.5 text-2xs">{counts.all}</Badge>
          </TabsTrigger>
          <TabsTrigger value="vm">
            <Monitor className="mr-1 h-3.5 w-3.5" /> VM
            <Badge variant="secondary" className="ml-1.5 text-2xs">{counts.vm}</Badge>
          </TabsTrigger>
          <TabsTrigger value="lxc">
            <Container className="mr-1 h-3.5 w-3.5" /> LXC
            <Badge variant="secondary" className="ml-1.5 text-2xs">{counts.lxc}</Badge>
          </TabsTrigger>
        </TabsList>
      </Tabs>

      {/* Filters bar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder={t('instances.search_placeholder', 'Search by name, IP, or VMID...')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>

        <Select value={statusFilter} onValueChange={v => { if (v !== null) setStatusFilter(v); }}>
          <SelectTrigger className="w-[130px]">
            <SelectValue placeholder={t('common.status', 'Status')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('instances.all_statuses', 'All statuses')}</SelectItem>
            <SelectItem value="running">{t('common.running', 'Running')}</SelectItem>
            <SelectItem value="stopped">{t('common.stopped', 'Stopped')}</SelectItem>
          </SelectContent>
        </Select>

        {servers.length > 1 && (
          <Select value={serverFilter} onValueChange={v => { if (v !== null) { setServerFilter(v); localStorage.setItem('instances-server-filter', v); setNodeFilter('all'); localStorage.setItem('instances-node-filter', 'all'); } }}>
            <SelectTrigger className="w-[150px]">
              <SelectValue placeholder={t('backups.select_server', 'Server')} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('instances.all_servers', 'All servers')}</SelectItem>
              {servers.map((s) => (
                <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        <Select value={nodeFilter} onValueChange={v => { if (v !== null) { setNodeFilter(v); localStorage.setItem('instances-node-filter', v); } }}>
          <SelectTrigger className="w-[130px]">
            <SelectValue placeholder={t('common.node', 'Node')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('instances.all_nodes', 'All nodes')}</SelectItem>
            {uniqueNodes.map((n) => (
              <SelectItem key={n} value={n}>{nodeLabel.get(n) ?? n}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Bulk actions toolbar */}
      {selectedVMs.length > 0 && (
        <div className="flex items-center gap-2 rounded-lg border bg-muted/50 p-2">
          <span className="text-sm font-medium">{t('instances.selected_count', '{{count}} выбрано', { count: selectedVMs.length })}</span>
          <Button variant="outline" size="sm" onClick={() => handleBulk('start')} disabled={bulkOp.isPending}>
            <Play className="mr-1 h-3 w-3" /> {t('common.start', 'Запустить')}
          </Button>
          <Button variant="outline" size="sm" onClick={() => handleBulk('restart')} disabled={bulkOp.isPending}>
            <RotateCcw className="mr-1 h-3 w-3" /> {t('common.restart', 'Перезагрузить')}
          </Button>
          <Button variant="outline" size="sm" onClick={() => handleBulk('stop')} disabled={bulkOp.isPending}>
            <Square className="mr-1 h-3 w-3" /> {t('common.stop', 'Остановить')}
          </Button>
          <Button
            variant="outline" size="sm"
            onClick={() => setMigrateOpen(true)}
            disabled={bulkOp.isPending || !bulkServerId}
            title={!bulkServerId ? t('instances.migrate_same_server', 'Выберите инстансы одного сервера') : undefined}
          >
            <ArrowRightLeft className="mr-1 h-3 w-3" /> {t('instances.migrate', 'Мигрировать')}
          </Button>
          <Button variant="destructive" size="sm" onClick={() => handleBulk('delete')} disabled={bulkOp.isPending}>
            <Trash2 className="mr-1 h-3 w-3" /> {t('common.delete', 'Удалить')}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setRowSelection({})}>
            {t('common.clear', 'Очистить')}
          </Button>
        </div>
      )}

      {bulkServerId && (
        <BulkMigrateDialog
          open={migrateOpen}
          onOpenChange={setMigrateOpen}
          serverId={bulkServerId}
          sourceNodes={bulkSourceNodes}
          count={selectedVMs.length}
          onConfirm={handleBulkMigrate}
          pending={bulkOp.isPending}
        />
      )}

      {/* Bulk operation progress (tracked like instance creation) */}
      {bulkTasks.map((task) => {
        const processed = task.completed + task.failed;
        const isDone = task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled';
        return (
          <div key={task.id} className="flex items-center gap-3 rounded-lg border bg-muted/50 p-2">
            {isDone ? (
              <Power className={`h-4 w-4 ${task.failed > 0 ? 'text-warning' : 'text-success'}`} />
            ) : (
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
            )}
            <span className="text-sm font-medium">{bulkActionLabel(task.action)}</span>
            <InlineProgress value={processed} max={task.total} />
            <span className="text-xs tabular-nums text-muted-foreground">
              {processed}/{task.total}
            </span>
            {task.failed > 0 && (
              <span className="text-xs text-danger">
                {task.failed} {t('instances.bulk_failed', 'с ошибкой')}
              </span>
            )}
          </div>
        );
      })}

      {/* Table */}
      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="space-y-3 p-4" aria-busy="true" aria-label={t('common.loading', 'Loading')}>
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="flex items-center gap-4">
                  <Skeleton className="h-4 w-4 rounded" />
                  <Skeleton className="h-4 flex-1 max-w-[220px]" />
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="hidden h-4 w-24 sm:block" />
                  <Skeleton className="hidden h-4 w-20 md:block" />
                  <Skeleton className="ml-auto h-4 w-8" />
                </div>
              ))}
            </div>
          ) : allRows.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Monitor className="mb-3 h-10 w-10 opacity-40" />
              <p className="text-sm font-medium">{t('instances.no_instances', 'No instances found')}</p>
              <p className="text-xs">{search ? t('instances.try_other_search', 'Try a different search query') : t('instances.create_first', 'Create your first instance')}</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                {table.getHeaderGroups().map((headerGroup) => (
                  <TableRow key={headerGroup.id}>
                    {headerGroup.headers.map((header) => {
                      const filterVariant = header.column.columnDef.meta?.filter;
                      // Expose sort state to assistive tech (WCAG aria-sort).
                      const sorted = header.column.getIsSorted();
                      const ariaSort = !header.column.getCanSort()
                        ? undefined
                        : sorted === 'asc'
                          ? 'ascending'
                          : sorted === 'desc'
                            ? 'descending'
                            : 'none';
                      return (
                        <TableHead
                          key={header.id}
                          style={{ width: header.getSize() }}
                          aria-sort={ariaSort}
                        >
                          {header.isPlaceholder ? null : (
                            <div className="flex items-center gap-0.5">
                              {flexRender(header.column.columnDef.header, header.getContext())}
                              {filterVariant && (
                                <ColumnFilter
                                  column={header.column}
                                  variant={filterVariant}
                                  formatOption={header.column.columnDef.meta?.formatOption}
                                />
                              )}
                            </div>
                          )}
                        </TableHead>
                      );
                    })}
                  </TableRow>
                ))}
              </TableHeader>
              <TableBody>
                {table.getRowModel().rows.map((row) => {
                  const isGhost = !!row.original._deployTaskId;
                  const isExpanded = !isGhost && expandedRows.has(row.id);
                  return (
                    <Fragment key={row.id}>
                      <TableRow
                        data-state={row.getIsSelected() && 'selected'}
                        className={isGhost ? 'opacity-70 bg-primary/5 border-l-2 border-l-primary' : undefined}
                      >
                        {row.getVisibleCells().map((cell) => (
                          <TableCell key={cell.id}>
                            {flexRender(cell.column.columnDef.cell, cell.getContext())}
                          </TableCell>
                        ))}
                      </TableRow>
                      {isExpanded && (
                        <TableRow className="hover:bg-transparent">
                          <TableCell colSpan={row.getVisibleCells().length} className="p-0">
                            <InstanceMetricsPanel
                              vm={row.original}
                              nodeLabelName={
                                (row.original.server_id != null ? serverNameById.get(row.original.server_id) : undefined) ??
                                row.original.node
                              }
                            />
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Pagination */}
      {allRows.length > 0 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            {t('instances.showing', 'Showing')} {table.getState().pagination.pageIndex * table.getState().pagination.pageSize + 1}–
            {Math.min(
              (table.getState().pagination.pageIndex + 1) * table.getState().pagination.pageSize,
              allRows.length
            )}{' '}
            {t('instances.of', 'of')} {allRows.length}
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="px-2 tabular-nums">
              {table.getState().pagination.pageIndex + 1} / {table.getPageCount()}
            </span>
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

    </div>
  );
}
