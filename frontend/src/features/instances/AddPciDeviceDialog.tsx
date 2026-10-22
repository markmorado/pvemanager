import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Cpu } from 'lucide-react';
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
import { useNodePciDevices, useUpdateConfig } from '@/hooks/use-instances';
import { toast } from 'sonner';

interface Props {
  open: boolean;
  onClose: () => void;
  serverId: number;
  vmid: number;
  node: string;
  config?: Record<string, unknown>;
}

/** Следующий свободный слот hostpciN из текущего конфига. */
function nextHostpciKey(config?: Record<string, unknown>): string {
  const used = new Set(
    Object.keys(config ?? {})
      .filter((k) => /^hostpci\d+$/.test(k))
      .map((k) => Number(k.slice(7))),
  );
  let n = 0;
  while (used.has(n)) n++;
  return `hostpci${n}`;
}

export default function AddPciDeviceDialog({ open, onClose, serverId, vmid, node, config }: Props) {
  const { t } = useTranslation();
  const { data: devices, isLoading } = useNodePciDevices(serverId, node, open);
  const updateConfig = useUpdateConfig(serverId, vmid, 'qemu', node);

  const [deviceId, setDeviceId] = useState('');
  const [pcie, setPcie] = useState(true);
  const [allFunctions, setAllFunctions] = useState(false);
  const [primaryGpu, setPrimaryGpu] = useState(false);
  const [romBar, setRomBar] = useState(true);

  const hostpciKey = useMemo(() => nextHostpciKey(config), [config]);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setDeviceId('');
      setPcie(true);
      setAllFunctions(false);
      setPrimaryGpu(false);
      setRomBar(true);
    }
  }

  const buildValue = (): string => {
    // "All functions" пробрасывает всё устройство: 0000:01:00 (без .функции)
    const id = allFunctions ? deviceId.replace(/\.\d+$/, '') : deviceId;
    const parts = [id];
    if (pcie) parts.push('pcie=1');
    if (primaryGpu) parts.push('x-vga=1');
    if (!romBar) parts.push('rombar=0');
    return parts.join(',');
  };

  const submit = () => {
    if (!deviceId) {
      toast.error(t('hw.err_pci_required', 'Выберите PCI-устройство'));
      return;
    }
    updateConfig.mutate(
      { [hostpciKey]: buildValue() },
      {
        onSuccess: () => {
          toast.success(t('hw.pci_added', 'Устройство {{key}} добавлено', { key: hostpciKey }));
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
            <Cpu className="h-4 w-4" />
            {t('hw.add_pci_title', 'Проброс PCI-устройства')}
          </DialogTitle>
          <DialogDescription>
            {t('hw.add_pci_description', 'Будет добавлено как {{key}}. VM должна быть выключена.', { key: hostpciKey })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>{t('hw.pci_device', 'Устройство')}</Label>
            <Select value={deviceId} onValueChange={(v) => setDeviceId(v ?? "")}>
              <SelectTrigger className="w-full">
                <SelectValue
                  placeholder={
                    isLoading
                      ? t('common.loading', 'Загрузка…')
                      : t('hw.select_pci', 'Выберите устройство')
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {(devices ?? []).map((d) => {
                  const label = [d.device_name, d.vendor_name].filter(Boolean).join(' — ');
                  return (
                    <SelectItem key={d.id} value={d.id}>
                      <span className="flex items-center gap-2">
                        <span className="font-mono text-xs">{d.id}</span>
                        {label && <span className="text-xs text-muted-foreground">{label}</span>}
                      </span>
                    </SelectItem>
                  );
                })}
                {!isLoading && (devices ?? []).length === 0 && (
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    {t('hw.no_pci', 'Устройства не найдены')}
                  </div>
                )}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={pcie} onChange={(e) => setPcie(e.target.checked)} />
              {t('hw.pci_pcie', 'PCI-Express (только для q35)')}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={allFunctions} onChange={(e) => setAllFunctions(e.target.checked)} />
              {t('hw.pci_all_functions', 'Все функции устройства')}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={primaryGpu} onChange={(e) => setPrimaryGpu(e.target.checked)} />
              {t('hw.pci_primary_gpu', 'Основная видеокарта (x-vga)')}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={romBar} onChange={(e) => setRomBar(e.target.checked)} />
              {t('hw.pci_rombar', 'ROM-Bar')}
            </label>
          </div>

          <p className="text-xs text-muted-foreground">
            {t('hw.pci_hint', 'Проброс PCI требует включённого IOMMU (VT-d / AMD-Vi) на хосте.')}
          </p>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={submit} disabled={updateConfig.isPending || !deviceId}>
            {t('hw.add_submit', 'Добавить')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
