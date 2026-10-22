import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Cpu, Usb, HardDrive, Plus, Trash2, MemoryStick, Loader2, AlertTriangle } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
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
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { useVMConfig, useUpdateConfig, useVMStatus, useMoveDisk, useAddDisk, useDetachDisk } from '@/hooks/use-instances';
import { useImageStorages } from '@/hooks/use-image-catalog';
import { useLXCStorages } from '@/hooks/use-lxc-templates';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { toast } from 'sonner';
import AddPciDeviceDialog from '../AddPciDeviceDialog';
import AddUsbDeviceDialog from '../AddUsbDeviceDialog';
import AddPhysicalDiskDialog from '../AddPhysicalDiskDialog';
import { ComputeResourcesCard, DiskResizeCard } from './ResourceCards';

interface Props {
  serverId: number;
  vmid: number;
  type: string;
  node: string;
}

const BIOS_OPTIONS = [
  { value: 'seabios', label: 'SeaBIOS (default)' },
  { value: 'ovmf', label: 'OVMF (UEFI)' },
];
const MACHINE_OPTIONS = [
  { value: 'pc', label: 'Default (i440fx)' },
  { value: 'q35', label: 'q35' },
];
const VGA_OPTIONS = ['std', 'qxl', 'virtio', 'vmware', 'serial0', 'none'];
const SCSIHW_OPTIONS = [
  'virtio-scsi-single',
  'virtio-scsi-pci',
  'lsi',
  'lsi53c810',
  'megasas',
  'pvscsi',
];

/** Нормализуем machine из конфига (напр. "pc-q35-8.1") к значению селекта. */
function machineToOption(raw: unknown): string {
  const s = typeof raw === 'string' ? raw.toLowerCase() : '';
  if (s.includes('q35')) return 'q35';
  return 'pc';
}

function PlatformCard({
  serverId, vmid, node, config,
}: Props & { config?: Record<string, unknown> }) {
  const { t } = useTranslation();
  const updateConfig = useUpdateConfig(serverId, vmid, 'qemu', node);

  const [bios, setBios] = useState('seabios');
  const [machine, setMachine] = useState('pc');
  const [vga, setVga] = useState('std');
  const [scsihw, setScsihw] = useState('virtio-scsi-single');
  const [sockets, setSockets] = useState('');
  const [efiStorage, setEfiStorage] = useState('');

  const hasEfiDisk = !!config?.efidisk0;
  // EFI-диск нужно создать, только если переключаемся на OVMF и его ещё нет.
  const needsEfiDisk = bios === 'ovmf' && !hasEfiDisk;

  // Хранилища ноды, поддерживающие образы дисков (content=images) — для efidisk0.
  const { data: storages, isLoading: storagesLoading } = useImageStorages(
    serverId, node, needsEfiDisk ? 'images' : undefined,
  );

  // Refill from each newly loaded config (also the one already cached on mount).
  const [syncedConfig, setSyncedConfig] = useState<typeof config>(undefined);
  if (config && config !== syncedConfig) {
    setSyncedConfig(config);
    setBios(typeof config.bios === 'string' ? config.bios : 'seabios');
    setMachine(machineToOption(config.machine));
    setVga(typeof config.vga === 'string' ? String(config.vga).split(',')[0] : 'std');
    setScsihw(typeof config.scsihw === 'string' ? config.scsihw : 'virtio-scsi-single');
    setSockets(config.sockets != null ? String(config.sockets) : '');
  }

  const save = () => {
    if (needsEfiDisk && !efiStorage) {
      toast.error(t('hw.err_efi_storage', 'Выберите хранилище для EFI-диска'));
      return;
    }
    const updates: Record<string, unknown> = { bios, machine, vga, scsihw };
    if (sockets.trim() !== '') updates.sockets = Number(sockets);
    // При переходе на OVMF без существующего EFI-диска создаём efidisk0.
    // Формат как в web-интерфейсе PVE: <storage>:1,efitype=4m,pre-enrolled-keys=1
    if (needsEfiDisk) {
      updates.efidisk0 = `${efiStorage}:1,efitype=4m,pre-enrolled-keys=1`;
    }
    updateConfig.mutate(updates, {
      onSuccess: () => {
        toast.success(t('hw.platform_saved', 'Оборудование обновлено'));
        setEfiStorage('');
      },
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <MemoryStick className="h-4 w-4" />
          {t('hw.platform', 'Платформа')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('hw.bios', 'BIOS')}</Label>
            <Select value={bios} onValueChange={(v) => setBios(v ?? "")}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {BIOS_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('hw.machine', 'Тип платформы')}</Label>
            <Select value={machine} onValueChange={(v) => setMachine(v ?? "")}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {MACHINE_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('hw.vga', 'Дисплей (VGA)')}</Label>
            <Select value={vga} onValueChange={(v) => setVga(v ?? "")}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {VGA_OPTIONS.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('hw.scsihw', 'SCSI-контроллер')}</Label>
            <Select value={scsihw} onValueChange={(v) => setScsihw(v ?? "")}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {SCSIHW_OPTIONS.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="hw-sockets">{t('hw.sockets', 'CPU-сокеты')}</Label>
            <Input id="hw-sockets" type="number" min={1} max={4} value={sockets}
              onChange={(e) => setSockets(e.target.value)} placeholder="1" />
          </div>
        </div>

        {/* EFI-диск: обязателен для OVMF, показываем выбор хранилища */}
        {needsEfiDisk && (
          <div className="rounded-md border border-warning/40 bg-warning/5 p-3 space-y-2">
            <div className="flex items-center gap-2 text-sm font-medium text-warning">
              <AlertTriangle className="h-4 w-4" />
              {t('hw.efi_required_title', 'Для OVMF (UEFI) нужен EFI-диск')}
            </div>
            <p className="text-xs text-muted-foreground">
              {t('hw.efi_required_hint', 'EFI-диск будет создан автоматически. Выберите хранилище для него.')}
            </p>
            <div className="space-y-1.5">
              <Label>{t('hw.efi_storage', 'Хранилище EFI-диска')}</Label>
              <Select value={efiStorage} onValueChange={(v) => setEfiStorage(v ?? "")}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder={
                    storagesLoading
                      ? t('common.loading', 'Загрузка…')
                      : t('hw.select_storage', 'Выберите хранилище')
                  } />
                </SelectTrigger>
                <SelectContent>
                  {(storages ?? []).map((s) => (
                    <SelectItem key={s.storage} value={s.storage}>{s.storage}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        )}

        <Button size="sm" onClick={save} disabled={updateConfig.isPending}>
          {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('common.save', 'Сохранить')}
        </Button>
        <p className="text-xs text-muted-foreground">
          {t('hw.platform_hint', 'Изменения оборудования применяются после выключения и повторного запуска VM.')}
        </p>
      </CardContent>
    </Card>
  );
}

// ==================== CPU options ====================

const CPU_TYPES = ['default', 'host', 'x86-64-v2', 'x86-64-v2-AES', 'x86-64-v3', 'x86-64-v4', 'kvm64', 'qemu64'];

function CpuOptionsCard({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, type, node);

  const [cpuType, setCpuType] = useState('default');
  const [sockets, setSockets] = useState('');
  const [cpulimit, setCpulimit] = useState('');
  const [cpuunits, setCpuunits] = useState('');
  const [numa, setNuma] = useState(false);

  // Refill from each newly loaded config (also the one already cached on mount).
  const [syncedConfig, setSyncedConfig] = useState<typeof config>(undefined);
  if (config && config !== syncedConfig) {
    setSyncedConfig(config);
    // cpu из конфига может быть "host" или "cputype=host,flags=..."
    const raw = typeof config.cpu === 'string' ? config.cpu : '';
    const parsed = raw.includes('cputype=') ? (raw.match(/cputype=([^,]+)/)?.[1] ?? '') : raw;
    setCpuType(parsed && CPU_TYPES.includes(parsed) ? parsed : (parsed ? parsed : 'default'));
    setSockets(config.sockets != null ? String(config.sockets) : '');
    setCpulimit(config.cpulimit != null ? String(config.cpulimit) : '');
    setCpuunits(config.cpuunits != null ? String(config.cpuunits) : '');
    setNuma(Number(config.numa) === 1);
  }

  const save = () => {
    const updates: Record<string, unknown> = {
      sockets: sockets ? Number(sockets) : 1,
      numa: numa ? 1 : 0,
    };
    if (cpuType && cpuType !== 'default') updates.cpu = cpuType;
    else updates.delete = 'cpu';
    // cpulimit: 0 = без лимита; cpuunits: вес планировщика
    updates.cpulimit = cpulimit ? Number(cpulimit) : 0;
    if (cpuunits) updates.cpuunits = Number(cpuunits);

    updateConfig.mutate(updates, {
      onSuccess: () => toast.success(t('instances.cpu_saved')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Cpu className="h-4 w-4" />{t('instances.cpu_options')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('instances.cpu_type')}</Label>
            <Select value={cpuType} onValueChange={(v) => v && setCpuType(v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {CPU_TYPES.map((c) => (
                  <SelectItem key={c} value={c}>{c === 'default' ? `default (kvm64)` : c}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{t('instances.cpu_type_hint')}</p>
          </div>
          <div className="space-y-1.5">
            <Label>{t('instances.sockets')}</Label>
            <Input type="number" min={1} max={8} value={sockets} onChange={(e) => setSockets(e.target.value)} placeholder="1" />
          </div>
          <div className="space-y-1.5">
            <Label>{t('instances.cpulimit')}</Label>
            <Input type="number" min={0} max={128} step="0.1" value={cpulimit} onChange={(e) => setCpulimit(e.target.value)} placeholder="0" />
            <p className="text-xs text-muted-foreground">{t('instances.cpulimit_hint')}</p>
          </div>
          <div className="space-y-1.5">
            <Label>{t('instances.cpuunits')}</Label>
            <Input type="number" min={1} max={262144} value={cpuunits} onChange={(e) => setCpuunits(e.target.value)} placeholder="100" />
            <p className="text-xs text-muted-foreground">{t('instances.cpuunits_hint')}</p>
          </div>
        </div>
        <label className="flex items-center gap-2.5 cursor-pointer">
          <Checkbox checked={numa} onChange={(e) => setNuma(e.target.checked)} />
          <span className="text-sm">{t('instances.numa')}</span>
        </label>
        <Button size="sm" onClick={save} disabled={updateConfig.isPending}>
          {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('common.save')}
        </Button>
        <p className="text-xs text-muted-foreground">{t('instances.apply_changes_note')}</p>
      </CardContent>
    </Card>
  );
}

// ==================== Disk management (move / add / detach) ====================

/** Достать имя хранилища из спецификации диска ("local-lvm:vm-100-disk-0,size=32G"). */
function parseDiskStorage(spec: unknown): string | null {
  if (typeof spec !== 'string') return null;
  return spec.split(':', 1)[0] || null;
}

/** Следующее свободное имя устройства для заданной шины (scsi/virtio/sata). */
function nextFreeDevice(bus: string, existing: string[]): string {
  for (let i = 0; i < 31; i++) {
    const name = `${bus}${i}`;
    if (!existing.includes(name)) return name;
  }
  return `${bus}0`;
}

function DiskManagementCard({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  // Только хранилища, поддерживающие нужный тип контента (rootdir для LXC, images для VM) —
  // иначе Proxmox отклонит задачу переноса уже после запуска.
  const { data: storages = [] } = useLXCStorages(serverId, node, type === 'lxc' ? 'rootdir' : 'images');
  const moveDisk = useMoveDisk(serverId, vmid, type, node);
  const addDisk = useAddDisk(serverId, vmid, node);
  const detachDisk = useDetachDisk(serverId, vmid, node);

  // Реальные дисковые устройства (без cd-rom и cloudinit)
  const disks = useMemo(() => {
    if (!config) return [] as string[];
    const re = type === 'lxc' ? /^(rootfs|mp\d+)$/ : /^(scsi\d+|sata\d+|virtio\d+|ide\d+)$/;
    return Object.entries(config)
      .filter(([k, v]) => re.test(k) && typeof v === 'string'
        && !v.includes('media=cdrom') && !v.includes('cloudinit')
        && !v.startsWith('/dev/'))  // проброшенные физдиски — управляются в блоке «Проброс устройств»
      .map(([k]) => k);
  }, [config, type]);

  const [moveDiskPick, setMoveDiskDev] = useState('');
  // The picked disk, or the first one while none (or a removed one) is picked.
  const moveDiskDev = disks.includes(moveDiskPick) ? moveDiskPick : (disks[0] ?? '');
  const [moveStorage, setMoveStorage] = useState('');
  const [moveDelete, setMoveDelete] = useState(true);

  const [addBus, setAddBus] = useState('scsi');
  const [addStorage, setAddStorage] = useState('');
  const [addSize, setAddSize] = useState('16');
  const [addSsd, setAddSsd] = useState(false);
  const [addDiscard, setAddDiscard] = useState(false);

  const doMove = () => {
    if (!moveDiskDev || !moveStorage) return;
    moveDisk.mutate(
      { disk: moveDiskDev, target_storage: moveStorage, delete: moveDelete },
      {
        onSuccess: () => { toast.success(t('instances.disk_moved')); setMoveStorage(''); },
        onError: (e: Error) => toast.error(e.message),
      }
    );
  };

  const doAdd = () => {
    if (!addStorage || !addSize) return;
    const dev = nextFreeDevice(addBus, disks);
    addDisk.mutate(
      { disk: dev, storage: addStorage, size_gb: Number(addSize), ssd: addSsd, discard: addDiscard },
      {
        onSuccess: () => { toast.success(t('instances.disk_added', { dev })); },
        onError: (e: Error) => toast.error(e.message),
      }
    );
  };

  const doDetach = async (dev: string) => {
    const destroy = await confirm(t('instances.disk_detach_confirm', { dev }));
    if (!destroy) return;
    detachDisk.mutate(
      { disk: dev, destroy: true },
      {
        onSuccess: () => toast.success(t('instances.disk_detached')),
        onError: (e: Error) => toast.error(e.message),
      }
    );
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <HardDrive className="h-4 w-4" />{t('instances.disk_management')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {/* Список дисков */}
        <div className="space-y-1.5">
          {disks.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t('instances.no_disks')}</p>
          ) : disks.map((d) => (
            <div key={d} className="flex items-center justify-between rounded-md border px-3 py-1.5 text-sm">
              <span className="font-mono">{d}</span>
              <span className="text-xs text-muted-foreground">{parseDiskStorage(config?.[d]) ?? '—'}</span>
              <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" title={t('common.delete')} onClick={() => doDetach(d)}>
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          ))}
        </div>

        {/* Move disk */}
        <div className="space-y-2 border-t pt-3">
          <Label className="text-xs font-medium">{t('instances.disk_move')}</Label>
          <div className="grid gap-3 sm:grid-cols-2">
            <Select value={moveDiskDev} onValueChange={(v) => v && setMoveDiskDev(v)}>
              <SelectTrigger><SelectValue placeholder={t('instances.disk_device')} /></SelectTrigger>
              <SelectContent>{disks.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
            </Select>
            <Select value={moveStorage} onValueChange={(v) => v && setMoveStorage(v)}>
              <SelectTrigger><SelectValue placeholder={t('instances.migrate_target_storage')} /></SelectTrigger>
              <SelectContent>{storages.map((s) => <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>)}</SelectContent>
            </Select>
          </div>
          <label className="flex items-center gap-2 text-xs">
            <Checkbox checked={moveDelete} onChange={(e) => setMoveDelete(e.target.checked)} />
            <span>{t('instances.disk_move_delete')}</span>
          </label>
          <Button size="sm" variant="outline" onClick={doMove} disabled={!moveStorage || moveDisk.isPending}>
            {moveDisk.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.disk_move')}
          </Button>
        </div>

        {/* Add disk */}
        <div className="space-y-2 border-t pt-3">
          <Label className="text-xs font-medium">{t('instances.disk_add')}</Label>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1"><Label className="text-2xs">{t('instances.disk_bus')}</Label>
              <Select value={addBus} onValueChange={(v) => v && setAddBus(v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="scsi">scsi</SelectItem>
                  <SelectItem value="virtio">virtio</SelectItem>
                  <SelectItem value="sata">sata</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1"><Label className="text-2xs">{t('instances.migrate_target_storage')}</Label>
              <Select value={addStorage} onValueChange={(v) => v && setAddStorage(v)}>
                <SelectTrigger><SelectValue placeholder="—" /></SelectTrigger>
                <SelectContent>{storages.map((s) => <SelectItem key={s.storage} value={s.storage}>{s.storage}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-1"><Label className="text-2xs">{t('instances.new_size_gb')}</Label>
              <Input type="number" min={1} value={addSize} onChange={(e) => setAddSize(e.target.value)} />
            </div>
          </div>
          <div className="flex gap-4">
            <label className="flex items-center gap-2 text-xs"><Checkbox checked={addSsd} onChange={(e) => setAddSsd(e.target.checked)} /><span>SSD</span></label>
            <label className="flex items-center gap-2 text-xs"><Checkbox checked={addDiscard} onChange={(e) => setAddDiscard(e.target.checked)} /><span>Discard</span></label>
          </div>
          <Button size="sm" variant="outline" onClick={doAdd} disabled={!addStorage || !addSize || addDisk.isPending}>
            {addDisk.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('instances.disk_add')}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default function HardwareTab({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const { data: status } = useVMStatus(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, 'qemu', node);

  const [showAddPci, setShowAddPci] = useState(false);
  const [showAddUsb, setShowAddUsb] = useState(false);
  const [showAddDisk, setShowAddDisk] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const cfg = config as Record<string, unknown> | undefined;
  // Проброс PCI/USB нельзя менять на работающей VM — устройства не поддерживают hotplug.
  const isRunning = status?.status === 'running';

  // Проброшенные устройства: hostpciN (PCI), usbN (USB) и физдиски (scsiN/virtioN/sataN
  // со значением-путём /dev/...). Обычные диски (storage:size) сюда не попадают.
  const passthrough = useMemo(() => {
    if (!cfg) return [] as Array<{ key: string; value: string; kind: 'pci' | 'usb' | 'disk' }>;
    return Object.entries(cfg)
      .filter(([k, v]) =>
        /^(hostpci|usb)\d+$/.test(k) ||
        (/^(scsi|virtio|sata|ide)\d+$/.test(k) && typeof v === 'string' && v.startsWith('/dev/')))
      .map(([key, value]) => ({
        key,
        value: String(value),
        kind: key.startsWith('hostpci')
          ? ('pci' as const)
          : key.startsWith('usb')
            ? ('usb' as const)
            : ('disk' as const),
      }))
      .sort((a, b) => a.key.localeCompare(b.key));
  }, [cfg]);

  const removeDevice = () => {
    if (!deleteTarget) return;
    updateConfig.mutate(
      { delete: deleteTarget },
      {
        onSuccess: () => {
          toast.success(t('hw.device_removed', 'Устройство {{key}} удалено', { key: deleteTarget }));
          setDeleteTarget(null);
        },
        onError: (e) => toast.error((e as Error).message),
      },
    );
  };

  return (
    <div className="space-y-4 max-w-2xl">
      <ComputeResourcesCard serverId={serverId} vmid={vmid} type={type} node={node} />

      <CpuOptionsCard serverId={serverId} vmid={vmid} type={type} node={node} />

      <PlatformCard serverId={serverId} vmid={vmid} type={type} node={node} config={cfg} />

      <DiskManagementCard serverId={serverId} vmid={vmid} type={type} node={node} />

      <DiskResizeCard serverId={serverId} vmid={vmid} type={type} node={node} />

      {/* Passthrough devices */}
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-semibold flex items-center gap-2">
              <Cpu className="h-4 w-4" />
              {t('hw.passthrough', 'Проброс устройств')}
            </CardTitle>
            <div className="flex items-center gap-1.5">
              <Button size="sm" variant="outline" disabled={isRunning}
                title={isRunning ? t('hw.stop_first', 'Выключите VM для изменения проброса') : undefined}
                onClick={() => setShowAddPci(true)}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />PCI
              </Button>
              <Button size="sm" variant="outline" disabled={isRunning}
                title={isRunning ? t('hw.stop_first', 'Выключите VM для изменения проброса') : undefined}
                onClick={() => setShowAddUsb(true)}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />USB
              </Button>
              <Button size="sm" variant="outline"
                onClick={() => setShowAddDisk(true)}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />{t('hw.disk', 'Диск')}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {isRunning && (
            <div className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/5 p-2.5 text-xs text-warning">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              {t('hw.running_lock', 'VM запущена — проброс устройств нельзя добавить или удалить. Выключите VM.')}
            </div>
          )}
          {passthrough.length === 0 && (
            <p className="py-4 text-center text-sm text-muted-foreground">
              {t('hw.no_passthrough', 'Нет проброшенных устройств')}
            </p>
          )}
          {passthrough.map(({ key, value, kind }) => (
            <div key={key} className="rounded-md border p-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {kind === 'pci'
                    ? <Cpu className="h-3.5 w-3.5 text-muted-foreground" />
                    : kind === 'usb'
                      ? <Usb className="h-3.5 w-3.5 text-muted-foreground" />
                      : <HardDrive className="h-3.5 w-3.5 text-muted-foreground" />}
                  <Badge variant="outline">{key}</Badge>
                  <span className="font-mono text-xs">{value}</span>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-destructive hover:text-destructive"
                  disabled={isRunning && kind !== 'disk'}
                  onClick={() => setDeleteTarget(key)}
                  title={isRunning && kind !== 'disk'
                    ? t('hw.stop_first', 'Выключите VM для изменения проброса')
                    : t('hw.remove_device', 'Удалить устройство')}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          ))}
          <p className="text-xs text-muted-foreground">
            {t('hw.passthrough_hint', 'Проброс PCI/USB требует выключенной VM и включённого IOMMU на хосте.')}
          </p>
        </CardContent>
      </Card>

      <AddPciDeviceDialog
        open={showAddPci}
        onClose={() => setShowAddPci(false)}
        serverId={serverId}
        vmid={vmid}
        node={node}
        config={cfg}
      />
      <AddUsbDeviceDialog
        open={showAddUsb}
        onClose={() => setShowAddUsb(false)}
        serverId={serverId}
        vmid={vmid}
        node={node}
        config={cfg}
      />
      <AddPhysicalDiskDialog
        open={showAddDisk}
        onClose={() => setShowAddDisk(false)}
        serverId={serverId}
        vmid={vmid}
        node={node}
        config={cfg}
      />

      {/* Delete confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('hw.remove_title', 'Удалить устройство?')}</DialogTitle>
            <DialogDescription>
              {t('hw.remove_description', 'Устройство {{key}} будет удалено из конфигурации VM.', { key: deleteTarget ?? '' })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              {t('common.cancel', 'Отмена')}
            </Button>
            <Button variant="destructive" onClick={removeDevice} disabled={updateConfig.isPending}>
              {t('common.delete', 'Удалить')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
