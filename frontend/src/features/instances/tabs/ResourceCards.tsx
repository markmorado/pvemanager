import { useState, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Settings, Loader2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
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
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { useVMConfig, useUpdateConfig, useResizeDisk, useMoveDisk, useVMStatus, usePowerAction } from '@/hooks/use-instances';
import { useLXCStorages } from '@/hooks/use-lxc-templates';
import { Checkbox } from '@/components/ui/checkbox';
import { apiClient } from '@/lib/api-client';
import { toast } from 'sonner';

interface Props {
  serverId: number;
  vmid: number;
  type: string;
  node: string;
}

/** Достать текущий размер диска в ГБ из строки конфига Proxmox
 *  (напр. "local-nvme:vm-106-disk-0.qcow2,...,size=51712M,ssd=1"). */
function parseDiskSizeGb(spec: unknown): number | null {
  if (typeof spec !== 'string') return null;
  const m = spec.match(/(?:^|,)size=(\d+(?:\.\d+)?)([KMGT])?/i);
  if (!m) return null;
  const value = parseFloat(m[1]);
  const factor: Record<string, number> = { K: 1 / 1048576, M: 1 / 1024, G: 1, T: 1024 };
  return value * (factor[(m[2] || 'G').toUpperCase()] ?? 1);
}

// ==================== Compute resources (cores / memory) ====================

export function ComputeResourcesCard({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, type, node);

  const [cores, setCores] = useState('');
  const [memory, setMemory] = useState('');

  const currentCores = config?.cores || 0;
  const currentMemory = config?.memory || 0;

  const handleConfigUpdate = () => {
    const updates: Record<string, unknown> = {};
    if (cores && Number(cores) !== currentCores) updates.cores = Number(cores);
    if (memory && Number(memory) !== currentMemory) updates.memory = Number(memory);

    if (Object.keys(updates).length === 0) {
      toast.info('No changes to apply');
      return;
    }

    updateConfig.mutate(updates, {
      onSuccess: () => {
        toast.success('Configuration updated');
        setCores('');
        setMemory('');
      },
      onError: (err) => toast.error(err.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Settings className="h-4 w-4" />
          {t('instances.compute_resources')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="cores">{t('instances.vcpu_cores')}</Label>
            <Input
              id="cores"
              type="number"
              min={1}
              max={128}
              placeholder={String(currentCores)}
              value={cores}
              onChange={(e) => setCores(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">{t('instances.current_cores', { count: currentCores })}</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="memory">{t('instances.memory_mb')}</Label>
            <Input
              id="memory"
              type="number"
              min={128}
              step={128}
              placeholder={String(currentMemory)}
              value={memory}
              onChange={(e) => setMemory(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">{t('instances.current_memory', { count: currentMemory })}</p>
          </div>
        </div>
        <Button onClick={handleConfigUpdate} disabled={updateConfig.isPending} size="sm">
          {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('instances.apply_changes')}
        </Button>
        <p className="text-xs text-muted-foreground">
          {t('instances.apply_changes_note')}
        </p>
      </CardContent>
    </Card>
  );
}

// ==================== Disk resize ====================

export function DiskResizeCard({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const resizeDisk = useResizeDisk(serverId, vmid, type, node);

  const [diskSize, setDiskSize] = useState('');
  const [diskDevicePick, setDiskDevice] = useState('');
  const [growResult, setGrowResult] = useState<{ grown: boolean; output: string } | null>(null);

  // Реальные дисковые устройства из конфига Proxmox (исключая cd-rom).
  // Захардкоженный scsi0 ломал ресайз на VM с другим диском (напр. sata0).
  const diskDevices = useMemo(() => {
    if (!config) return [] as string[];
    const re = /^(rootfs|mp\d+|scsi\d+|sata\d+|virtio\d+|ide\d+)$/;
    return Object.entries(config)
      .filter(([k, v]) => re.test(k) && typeof v === 'string' && !v.includes('media=cdrom'))
      .map(([k]) => k);
  }, [config]);

  // Загрузочный диск из boot=order=... — выбираем по умолчанию.
  const bootOrder = typeof config?.boot === 'string' ? config.boot : '';
  const bootDisk = useMemo(() => {
    const first = bootOrder.match(/order=([^;]+)/)?.[1]?.split(';')[0]?.trim();
    return first && diskDevices.includes(first) ? first : undefined;
  }, [bootOrder, diskDevices]);

  // The picked disk, or the boot disk (else the first one) until one is picked.
  const diskDevice = diskDevices.includes(diskDevicePick)
    ? diskDevicePick
    : (bootDisk ?? diskDevices[0] ?? '');

  // Текущий размер выбранного диска (ГБ). Proxmox не умеет уменьшать диск,
  // поэтому показываем его и не даём ввести значение меньше текущего.
  const currentDiskGb = useMemo(
    () => (diskDevice ? parseDiskSizeGb(config?.[diskDevice]) : null),
    [config, diskDevice]
  );
  const minDiskGb = currentDiskGb != null ? Math.ceil(currentDiskGb) : 1;

  // При смене диска подставляем текущий размер как стартовое значение,
  // чтобы пользователь увеличивал его, а не угадывал.
  const [sizeFilledFor, setSizeFilledFor] = useState<number | null>(null);
  if (currentDiskGb != null && currentDiskGb !== sizeFilledFor) {
    setSizeFilledFor(currentDiskGb);
    setDiskSize(String(Math.ceil(currentDiskGb)));
  }

  const handleDiskResize = () => {
    if (!diskSize || !diskDevice) return;
    if (currentDiskGb != null && Number(diskSize) <= currentDiskGb) {
      toast.error(
        t('instances.disk_resize_too_small', { size: currentDiskGb.toFixed(1) })
      );
      return;
    }
    resizeDisk.mutate(
      { disk: diskDevice, size: `${diskSize}G` },
      {
        onSuccess: (data) => {
          const res = data as { message?: string; filesystem_grown?: boolean; grow_output?: string };
          const grown = !!res?.filesystem_grown;
          setGrowResult({ grown, output: res?.grow_output ?? '' });
          if (grown) toast.success(res?.message ?? `Disk resized to ${diskSize}G`);
          else toast.warning(res?.message ?? t('instances.disk_no_fs', { size: diskSize }));
        },
        onError: (err) => toast.error(err.message),
      }
    );
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold">{t('instances.disk_resize')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="disk-device">{t('instances.disk_device')}</Label>
            {diskDevices.length > 0 ? (
              <Select value={diskDevice} onValueChange={(v) => setDiskDevice(v ?? "")}>
                <SelectTrigger id="disk-device">
                  <SelectValue placeholder={t('common.placeholder_disk')} />
                </SelectTrigger>
                <SelectContent>
                  {diskDevices.map((dev) => (
                    <SelectItem key={dev} value={dev}>
                      {dev}
                      {dev === bootDisk ? ' (boot)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id="disk-device"
                value={diskDevice}
                onChange={(e) => setDiskDevice(e.target.value)}
                placeholder={t('common.placeholder_disk')}
              />
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="disk-size">{t('instances.new_size_gb')}</Label>
            <Input
              id="disk-size"
              type="number"
              min={minDiskGb}
              value={diskSize}
              onChange={(e) => setDiskSize(e.target.value)}
              placeholder={t('common.placeholder_disk_size')}
            />
            {currentDiskGb != null && (
              <p className="text-xs text-muted-foreground">
                {t('instances.current_disk_size', { size: currentDiskGb.toFixed(1) })}
              </p>
            )}
          </div>
        </div>
        <Button
          onClick={handleDiskResize}
          disabled={!diskSize || !diskDevice || resizeDisk.isPending}
          size="sm"
          variant="outline"
        >
          {resizeDisk.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('instances.resize_disk')}
        </Button>
        <p className="text-xs text-muted-foreground">
          {t('instances.resize_disk_hint')}
        </p>
        {growResult && (
          <div className="space-y-1.5">
            <p className={`text-xs font-medium ${growResult.grown ? 'text-success' : 'text-warning'}`}>
              {growResult.grown
                ? t('instances.fs_grown')
                : t('instances.fs_not_grown')}
            </p>
            {growResult.output && (
              <pre className="max-h-48 overflow-auto rounded bg-muted p-2 text-xs font-mono whitespace-pre-wrap">
                {growResult.output}
              </pre>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ==================== Move disk to another storage ====================

type MoveStage = 'confirm' | 'stopping' | 'moving' | 'starting' | 'error';

export function DiskMoveCard({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  // Только хранилища, поддерживающие нужный тип контента (rootdir для LXC, images для VM) —
  // иначе Proxmox отклонит задачу переноса уже после запуска.
  const { data: storages = [] } = useLXCStorages(serverId, node, type === 'lxc' ? 'rootdir' : 'images');
  const moveDisk = useMoveDisk(serverId, vmid, type, node);
  const power = usePowerAction(serverId, vmid, type, node);
  // move_volume для LXC (в отличие от QEMU move_disk) не работает на запущенном контейнере —
  // Proxmox отклоняет задачу с "cannot move volumes of a running container".
  const { data: status } = useVMStatus(serverId, vmid, type, node);
  const isRunning = status?.status === 'running';
  const blockedByRunning = type === 'lxc' && isRunning;

  const diskDevices = useMemo(() => {
    if (!config) return [] as string[];
    const re = type === 'lxc' ? /^(rootfs|mp\d+)$/ : /^(scsi\d+|sata\d+|virtio\d+|ide\d+)$/;
    return Object.entries(config)
      .filter(([k, v]) => re.test(k) && typeof v === 'string' && !v.includes('media=cdrom'))
      .map(([k]) => k);
  }, [config, type]);

  const [moveDiskDev, setMoveDiskDev] = useState('');
  const [moveStorage, setMoveStorage] = useState('');
  const [moveDelete, setMoveDelete] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [stage, setStage] = useState<MoveStage>('confirm');
  const [errorMsg, setErrorMsg] = useState('');

  useEffect(() => {
    if (diskDevices.length && !diskDevices.includes(moveDiskDev)) setMoveDiskDev(diskDevices[0]);
  }, [diskDevices, moveDiskDev]);

  const prefix = type === 'lxc' ? 'container' : 'vm';
  const fetchStatus = () =>
    apiClient
      .get<{ status: string }>(`/proxmox/api/${serverId}/${prefix}/${vmid}/status?node=${node}`)
      .then((r) => r.status);

  const waitForStatus = async (target: string, timeoutMs = 60000, intervalMs = 1500) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if ((await fetchStatus()) === target) return true;
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    return false;
  };

  const runMove = () =>
    moveDisk.mutateAsync({ disk: moveDiskDev, target_storage: moveStorage, delete: moveDelete });

  const openConfirm = () => {
    if (!moveDiskDev || !moveStorage) return;
    setErrorMsg('');
    setStage('confirm');
    setDialogOpen(true);
  };

  // Прямой перенос — контейнер/VM уже в подходящем состоянии.
  const confirmDirectMove = async () => {
    setStage('moving');
    try {
      await runMove();
      setDialogOpen(false);
      toast.success(t('instances.disk_moved'));
      setMoveStorage('');
    } catch (e) {
      setStage('error');
      setErrorMsg((e as Error).message);
    }
  };

  // Согласованный сценарий для запущенного LXC: стоп -> перенос -> запуск обратно.
  const confirmStopAndMove = async () => {
    setStage('stopping');
    try {
      await power.mutateAsync({ action: 'stop' });
      const stopped = await waitForStatus('stopped');
      if (!stopped) throw new Error(t('instances.disk_move_stop_timeout'));

      setStage('moving');
      await runMove();

      setStage('starting');
      await power.mutateAsync({ action: 'start' });

      setDialogOpen(false);
      toast.success(t('instances.disk_moved'));
      setMoveStorage('');
    } catch (e) {
      // Даже если перенос упал, возвращаем контейнер в исходное состояние.
      try { await power.mutateAsync({ action: 'start' }); } catch { /* уже могло стартовать */ }
      setStage('error');
      setErrorMsg((e as Error).message);
    }
  };

  const busy = stage === 'stopping' || stage === 'moving' || stage === 'starting';

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold">{t('instances.disk_move')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('instances.disk_device')}</Label>
            <Select value={moveDiskDev} onValueChange={(v) => v && setMoveDiskDev(v)}>
              <SelectTrigger><SelectValue placeholder={t('instances.disk_device')} /></SelectTrigger>
              <SelectContent>{diskDevices.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('instances.migrate_target_storage')}</Label>
            <Select value={moveStorage} onValueChange={(v) => v && setMoveStorage(v)}>
              <SelectTrigger><SelectValue placeholder={t('instances.migrate_target_storage')} /></SelectTrigger>
              <SelectContent>{storages.map((s) => <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>)}</SelectContent>
            </Select>
          </div>
        </div>
        <label className="flex items-center gap-2 text-xs">
          <Checkbox checked={moveDelete} onChange={(e) => setMoveDelete(e.target.checked)} />
          <span>{t('instances.disk_move_delete')}</span>
        </label>
        <Button size="sm" variant="outline" onClick={openConfirm} disabled={!moveDiskDev || !moveStorage}>
          {t('instances.disk_move')}
        </Button>
        {blockedByRunning && (
          <p className="text-xs text-muted-foreground">{t('instances.disk_move_stop_hint')}</p>
        )}
      </CardContent>

      <Dialog open={dialogOpen} onOpenChange={(open) => { if (!open && !busy) setDialogOpen(false); }}>
        <DialogContent showCloseButton={!busy}>
          <DialogHeader>
            <DialogTitle>{t('instances.disk_move_confirm_title')}</DialogTitle>
            <DialogDescription>
              {blockedByRunning
                ? t('instances.disk_move_confirm_running', { dev: moveDiskDev, storage: moveStorage })
                : t('instances.disk_move_confirm_plain', { dev: moveDiskDev, storage: moveStorage })}
            </DialogDescription>
          </DialogHeader>

          {stage === 'stopping' && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />{t('instances.disk_move_stage_stopping')}
            </p>
          )}
          {stage === 'moving' && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />{t('instances.disk_move_stage_moving')}
            </p>
          )}
          {stage === 'starting' && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />{t('instances.disk_move_stage_starting')}
            </p>
          )}
          {stage === 'error' && (
            <p className="text-sm text-destructive">{errorMsg}</p>
          )}

          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => setDialogOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              disabled={busy}
              onClick={blockedByRunning ? confirmStopAndMove : confirmDirectMove}
            >
              {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {blockedByRunning ? t('instances.disk_move_stop_and_move') : t('instances.disk_move')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
