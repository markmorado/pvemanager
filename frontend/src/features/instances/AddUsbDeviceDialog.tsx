import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Usb } from 'lucide-react';
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
import { useNodeUsbDevices, useUpdateConfig } from '@/hooks/use-instances';
import { toast } from 'sonner';

interface Props {
  open: boolean;
  onClose: () => void;
  serverId: number;
  vmid: number;
  node: string;
  config?: Record<string, unknown>;
}

/** Следующий свободный слот usbN из текущего конфига (макс. usb0..usb4). */
function nextUsbKey(config?: Record<string, unknown>): string {
  const used = new Set(
    Object.keys(config ?? {})
      .filter((k) => /^usb\d+$/.test(k))
      .map((k) => Number(k.slice(3))),
  );
  let n = 0;
  while (used.has(n)) n++;
  return `usb${n}`;
}

export default function AddUsbDeviceDialog({ open, onClose, serverId, vmid, node, config }: Props) {
  const { t } = useTranslation();
  const { data: devices, isLoading } = useNodeUsbDevices(serverId, node, open);
  const updateConfig = useUpdateConfig(serverId, vmid, 'qemu', node);

  // value = "vendid:prodid" — привязка по конкретному устройству (host=id:id)
  const [selected, setSelected] = useState('');
  const [usb3, setUsb3] = useState(false);

  const usbKey = useMemo(() => nextUsbKey(config), [config]);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setSelected('');
      setUsb3(false);
    }
  }

  const submit = () => {
    if (!selected) {
      toast.error(t('hw.err_usb_required', 'Выберите USB-устройство'));
      return;
    }
    const parts = [`host=${selected}`];
    if (usb3) parts.push('usb3=1');
    updateConfig.mutate(
      { [usbKey]: parts.join(',') },
      {
        onSuccess: () => {
          toast.success(t('hw.usb_added', 'Устройство {{key}} добавлено', { key: usbKey }));
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
            <Usb className="h-4 w-4" />
            {t('hw.add_usb_title', 'Проброс USB-устройства')}
          </DialogTitle>
          <DialogDescription>
            {t('hw.add_usb_description', 'Будет добавлено как {{key}}', { key: usbKey })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>{t('hw.usb_device', 'Устройство')}</Label>
            <Select value={selected} onValueChange={(v) => setSelected(v ?? "")}>
              <SelectTrigger className="w-full">
                <SelectValue
                  placeholder={
                    isLoading
                      ? t('common.loading', 'Загрузка…')
                      : t('hw.select_usb', 'Выберите устройство')
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {(devices ?? []).map((d, i) => {
                  const id = `${d.vendid}:${d.prodid}`;
                  const label = [d.manufacturer, d.prodname].filter(Boolean).join(' ');
                  return (
                    <SelectItem key={`${id}-${d.usbpath ?? d.port ?? i}`} value={id}>
                      <span className="flex items-center gap-2">
                        <span className="font-mono text-xs">{id}</span>
                        {label && <span className="text-xs text-muted-foreground">{label}</span>}
                      </span>
                    </SelectItem>
                  );
                })}
                {!isLoading && (devices ?? []).length === 0 && (
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    {t('hw.no_usb', 'Устройства не найдены')}
                  </div>
                )}
              </SelectContent>
            </Select>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={usb3} onChange={(e) => setUsb3(e.target.checked)} />
            {t('hw.usb3', 'USB 3.0')}
          </label>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={submit} disabled={updateConfig.isPending || !selected}>
            {t('hw.add_submit', 'Добавить')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
