import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Network } from 'lucide-react';
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
import { useNodeNetworks, useUpdateConfig } from '@/hooks/use-instances';
import { toast } from 'sonner';

const VM_MODELS = ['virtio', 'e1000', 'rtl8139', 'vmxnet3'] as const;
const MAC_RE = /^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$/;
const CIDR_RE = /^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/;
const IP_RE = /^\d{1,3}(\.\d{1,3}){3}$/;

interface Props {
  open: boolean;
  onClose: () => void;
  serverId: number;
  vmid: number;
  type: string; // 'qemu' | 'lxc'
  node: string;
  config?: Record<string, unknown>;
}

export default function AddNetworkInterfaceDialog({ open, onClose, serverId, vmid, type, node, config }: Props) {
  const { t } = useTranslation();
  const isLxc = type === 'lxc';

  const [model, setModel] = useState<string>('virtio');
  const [bridge, setBridge] = useState('');
  const [vlanTag, setVlanTag] = useState('');
  const [firewall, setFirewall] = useState(true);
  const [mac, setMac] = useState('');
  const [ifName, setIfName] = useState('');
  const [ipMode, setIpMode] = useState<'dhcp' | 'static'>('dhcp');
  const [ipCidr, setIpCidr] = useState('');
  const [gateway, setGateway] = useState('');

  const { data: networks } = useNodeNetworks(serverId, node, open);
  const bridges = useMemo(
    () =>
      [
        ...(networks?.interfaces ?? []).filter((i) => i.type === 'bridge'),
        ...(networks?.sdn_vnets ?? []),
      ].sort((a, b) => (a.iface || a.name || '').localeCompare(b.iface || b.name || '')),
    [networks],
  );

  const updateConfig = useUpdateConfig(serverId, vmid, type, node);

  // Next free netN slot from current config keys
  const netKey = useMemo(() => {
    const used = new Set(
      Object.keys(config ?? {})
        .filter((k) => /^net\d+$/.test(k))
        .map((k) => Number(k.slice(3))),
    );
    let n = 0;
    while (used.has(n)) n++;
    return `net${n}`;
  }, [config]);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setModel('virtio');
      setBridge('');
      setVlanTag('');
      setFirewall(true);
      setMac('');
      setIfName('');
      setIpMode('dhcp');
      setIpCidr('');
      setGateway('');
    }
  }

  const validationError = (): string | null => {
    if (!bridge) return t('netif.err_bridge_required', 'Выберите сетевой мост');
    if (mac && !MAC_RE.test(mac)) return t('netif.err_bad_mac', 'Некорректный MAC-адрес (формат AA:BB:CC:DD:EE:FF)');
    if (vlanTag && (!/^\d+$/.test(vlanTag) || +vlanTag < 1 || +vlanTag > 4094)) {
      return t('netif.err_bad_vlan', 'VLAN tag должен быть числом 1–4094');
    }
    if (isLxc && ipMode === 'static') {
      if (!CIDR_RE.test(ipCidr)) return t('netif.err_bad_cidr', 'Укажите IP в формате CIDR, например 10.0.0.5/24');
      if (gateway && !IP_RE.test(gateway)) return t('netif.err_bad_gateway', 'Некорректный адрес шлюза');
    }
    return null;
  };

  const buildValue = (): string => {
    const parts: string[] = [];
    if (isLxc) {
      const idx = netKey.slice(3);
      parts.push(`name=${ifName.trim() || `eth${idx}`}`);
      parts.push(`bridge=${bridge}`);
      if (mac) parts.push(`hwaddr=${mac.toUpperCase()}`);
      if (vlanTag) parts.push(`tag=${vlanTag}`);
      if (firewall) parts.push('firewall=1');
      if (ipMode === 'dhcp') {
        parts.push('ip=dhcp');
      } else {
        parts.push(`ip=${ipCidr}`);
        if (gateway) parts.push(`gw=${gateway}`);
      }
    } else {
      parts.push(mac ? `${model}=${mac.toUpperCase()}` : model);
      parts.push(`bridge=${bridge}`);
      if (vlanTag) parts.push(`tag=${vlanTag}`);
      if (firewall) parts.push('firewall=1');
    }
    return parts.join(',');
  };

  const submit = () => {
    const err = validationError();
    if (err) {
      toast.error(err);
      return;
    }
    updateConfig.mutate(
      { [netKey]: buildValue() },
      {
        onSuccess: () => {
          toast.success(t('netif.added', 'Интерфейс {{key}} добавлен', { key: netKey }));
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
            <Network className="h-4 w-4" />
            {t('netif.add_title', 'Добавить сетевой интерфейс')}
          </DialogTitle>
          <DialogDescription>
            {t('netif.add_description', 'Будет добавлен как {{key}}', { key: netKey })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            {isLxc ? (
              <div className="space-y-1.5">
                <Label htmlFor="nic-name">{t('netif.if_name', 'Имя интерфейса')}</Label>
                <Input
                  id="nic-name"
                  value={ifName}
                  onChange={(e) => setIfName(e.target.value)}
                  placeholder={`eth${netKey.slice(3)}`}
                />
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label>{t('netif.model', 'Модель')}</Label>
                <Select value={model} onValueChange={(v) => setModel(v ?? "")}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {VM_MODELS.map((m) => (
                      <SelectItem key={m} value={m}>{m}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            <div className="space-y-1.5">
              <Label>{t('netif.bridge', 'Мост')}</Label>
              <Select value={bridge} onValueChange={(v) => setBridge(v ?? "")}>
                <SelectTrigger>
                  <SelectValue placeholder={t('netif.select_bridge', 'Выберите мост')} />
                </SelectTrigger>
                <SelectContent>
                  {bridges.map((b) => {
                    const name = b.iface || b.name || '';
                    const hint = b.comments?.trim() || b.ipam_cidr || b.cidr || '';
                    return (
                      <SelectItem key={name} value={name}>
                        <span className="flex items-center gap-2">
                          {name}
                          {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
                        </span>
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="nic-vlan">{t('netif.vlan_tag', 'VLAN tag')}</Label>
              <Input
                id="nic-vlan"
                value={vlanTag}
                onChange={(e) => setVlanTag(e.target.value)}
                placeholder={t('netif.optional', 'необязательно')}
                inputMode="numeric"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="nic-mac">{t('netif.mac', 'MAC-адрес')}</Label>
              <Input
                id="nic-mac"
                value={mac}
                onChange={(e) => setMac(e.target.value)}
                placeholder={t('netif.mac_auto', 'автоматически')}
                className="font-mono"
              />
            </div>
          </div>

          {isLxc && (
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>{t('netif.ip_mode', 'Режим IP')}</Label>
                <Select value={ipMode} onValueChange={(v) => setIpMode(v as 'dhcp' | 'static')}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="dhcp">DHCP</SelectItem>
                    <SelectItem value="static">{t('netif.static', 'Статический')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {ipMode === 'static' && (
                <>
                  <div className="space-y-1.5">
                    <Label htmlFor="nic-ip">{t('netif.ip_cidr', 'IP/CIDR')}</Label>
                    <Input
                      id="nic-ip"
                      value={ipCidr}
                      onChange={(e) => setIpCidr(e.target.value)}
                      placeholder="10.0.0.5/24"
                      className="font-mono"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="nic-gw">{t('netif.gateway', 'Шлюз')}</Label>
                    <Input
                      id="nic-gw"
                      value={gateway}
                      onChange={(e) => setGateway(e.target.value)}
                      placeholder="10.0.0.1"
                      className="font-mono"
                    />
                  </div>
                </>
              )}
            </div>
          )}

          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={firewall} onChange={(e) => setFirewall(e.target.checked)} />
            {t('netif.firewall', 'Firewall')}
          </label>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={submit} disabled={updateConfig.isPending || !bridge}>
            {t('netif.add_submit', 'Добавить')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
