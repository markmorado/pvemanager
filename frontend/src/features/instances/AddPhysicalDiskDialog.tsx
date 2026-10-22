import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { HardDrive } from 'lucide-react';
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
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useNodeDisks } from '@/hooks/use-disks';
import { useAttachPhysicalDisk } from '@/hooks/use-instances';
import { formatBytes } from '@/lib/format';
import { toast } from 'sonner';

interface Props {
  open: boolean;
  onClose: () => void;
  serverId: number;
  vmid: number;
  node: string;
  config?: Record<string, unknown>;
}

const BUSES = ['scsi', 'virtio', 'sata'] as const;

/** Следующее свободное имя устройства для выбранной шины из текущего конфига. */
function nextDiskKey(bus: string, config?: Record<string, unknown>): string {
  const used = new Set(Object.keys(config ?? {}).filter((k) => new RegExp(`^${bus}\\d+$`).test(k)));
  for (let i = 0; i < 31; i++) {
    if (!used.has(`${bus}${i}`)) return `${bus}${i}`;
  }
  return `${bus}0`;
}

export default function AddPhysicalDiskDialog({ open, onClose, serverId, vmid, node, config }: Props) {
  const { t } = useTranslation();
  const { data, isLoading } = useNodeDisks(serverId, node, open);
  const attach = useAttachPhysicalDisk(serverId, vmid, node);

  const [devpath, setDevpath] = useState('');
  const [bus, setBus] = useState<string>('scsi');
  const [ssd, setSsd] = useState(false);
  const [discard, setDiscard] = useState(false);

  const disks = useMemo(() => data?.disks ?? [], [data]);
  const diskKey = useMemo(() => nextDiskKey(bus, config), [bus, config]);
  const selected = useMemo(() => disks.find((d) => (d.passthrough_path || d.devpath) === devpath), [disks, devpath]);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setDevpath('');
      setBus('scsi');
      setSsd(false);
      setDiscard(false);
    }
  }

  const submit = () => {
    if (!devpath) {
      toast.error(t('hw.err_disk_required', 'Выберите диск'));
      return;
    }
    attach.mutate(
      { disk: diskKey, devpath, ssd, discard },
      {
        onSuccess: () => {
          toast.success(t('hw.disk_added', 'Диск {{key}} проброшен', { key: diskKey }));
          onClose();
        },
        onError: (e) => toast.error((e as Error).message),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <HardDrive className="h-4 w-4" />
            {t('hw.add_disk_title', 'Проброс физического диска')}
          </DialogTitle>
          <DialogDescription>
            {t('hw.add_disk_description', 'Будет добавлено как {{key}}', { key: diskKey })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>{t('hw.disk_device', 'Диск')}</Label>
            <Select value={devpath} onValueChange={(v) => setDevpath(v ?? '')}>
              <SelectTrigger className="w-full">
                <SelectValue
                  placeholder={
                    isLoading
                      ? t('common.loading', 'Загрузка…')
                      : t('hw.select_disk', 'Выберите диск')
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {disks.map((d) => {
                  const value = d.passthrough_path || d.devpath;
                  const label = [d.model, d.serial].filter(Boolean).join(' · ');
                  return (
                    <SelectItem key={d.devpath} value={value}>
                      <span className="flex items-center gap-2">
                        <span className="font-mono text-xs">{d.devpath}</span>
                        {typeof d.size === 'number' && (
                          <span className="text-xs text-muted-foreground">{formatBytes(d.size)}</span>
                        )}
                        {label && <span className="text-xs text-muted-foreground">{label}</span>}
                        {d.used && (
                          <span className="text-xs text-warning">({d.used})</span>
                        )}
                      </span>
                    </SelectItem>
                  );
                })}
                {!isLoading && disks.length === 0 && (
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    {t('hw.no_disks', 'Диски не найдены')}
                  </div>
                )}
              </SelectContent>
            </Select>
            {selected?.used && (
              <p className="text-xs text-warning">
                {t('hw.disk_used_warn', 'Диск занят ({{used}}) — проброс может привести к потере данных.', { used: selected.used })}
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label>{t('hw.disk_bus', 'Шина')}</Label>
            <Select value={bus} onValueChange={(v) => v && setBus(v)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {BUSES.map((b) => (
                  <SelectItem key={b} value={b}>{b}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={ssd} onChange={(e) => setSsd(e.target.checked)} />
              {t('hw.disk_ssd', 'Эмулировать SSD')}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={discard} onChange={(e) => setDiscard(e.target.checked)} />
              {t('hw.disk_discard', 'Discard / TRIM')}
            </label>
          </div>

          <p className="text-xs text-muted-foreground">
            {t('hw.disk_passthrough_hint', 'Диск отдаётся VM монопольно по стабильному пути /dev/disk/by-id/. Привязан к этой ноде — миграция на другую ноду недоступна.')}
          </p>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={submit} disabled={attach.isPending || !devpath}>
            {t('hw.add_submit', 'Добавить')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
