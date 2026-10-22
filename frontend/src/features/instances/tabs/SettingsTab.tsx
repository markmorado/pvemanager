import { useState, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Settings, Loader2, Power, SlidersHorizontal, LockOpen, ListOrdered, ArrowUp, ArrowDown, Cloud, Eye, EyeOff } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useVMConfig, useUpdateConfig, useVMOwner, useSetVMOwner, useExecuteScript, useSavedConfig, useUnlockInstance, useUpdateCloudInit } from '@/hooks/use-instances';
import { useProfile } from '@/hooks/use-settings';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { toast } from 'sonner';
import { ComputeResourcesCard, DiskResizeCard, DiskMoveCard } from './ResourceCards';

interface Props {
  serverId: number;
  vmid: number;
  type: string;
  node: string;
}

const OWNER_NONE = '__none__';

function OwnerCard({ serverId, vmid }: { serverId: number; vmid: number }) {
  const { t } = useTranslation();
  const { data: profile } = useProfile();
  const isAdmin = !!profile?.is_admin;
  const { data: owner } = useVMOwner(serverId, vmid, isAdmin);
  const setOwner = useSetVMOwner(serverId, vmid);
  const [selected, setSelected] = useState<string | null>(null);

  if (!isAdmin) return null;

  const current = owner?.owner_id != null ? String(owner.owner_id) : OWNER_NONE;
  const value = selected ?? current;

  const handleSave = () => {
    const userId = value === OWNER_NONE ? null : Number(value);
    setOwner.mutate(userId, {
      onSuccess: () => { toast.success(t('instances.owner_saved')); setSelected(null); },
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Settings className="h-4 w-4" />{t('instances.owner')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1.5">
            <Label>{t('instances.owner')}</Label>
            <Select value={value} onValueChange={v => { if (v) setSelected(v); }}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={OWNER_NONE}>{t('instances.no_owner')}</SelectItem>
                {(owner?.users ?? []).map(u => (
                  <SelectItem key={u.id} value={String(u.id)}>{u.username}{u.full_name ? ` (${u.full_name})` : ''}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button size="sm" onClick={handleSave} disabled={setOwner.isPending || value === current}>{t('common.save')}</Button>
        </div>
        <p className="text-xs text-muted-foreground">{t('instances.owner_hint')}</p>
      </CardContent>
    </Card>
  );
}

function ExecuteScriptCard({ serverId, vmid, node }: { serverId: number; vmid: number; node: string }) {
  const { t } = useTranslation();
  const { data: profile } = useProfile();
  const exec = useExecuteScript(serverId, vmid);
  const [script, setScript] = useState('');
  const [interpreter, setInterpreter] = useState('/bin/bash');
  const [timeout, setTimeoutVal] = useState('60');

  if (!profile?.is_admin) return null;

  const run = () => {
    if (!script.trim()) { toast.error(t('instances.script_required')); return; }
    exec.mutate(
      { script, interpreter, node, timeout: Number(timeout) || 60 },
      { onError: (e: Error) => toast.error(e.message) },
    );
  };

  const r = exec.data;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Settings className="h-4 w-4" />{t('instances.execute_script')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">{t('instances.execute_script_hint')}</p>
        <textarea
          value={script}
          onChange={e => setScript(e.target.value)}
          rows={6}
          spellCheck={false}
          placeholder={'uptime\ndf -h'}
          className="w-full rounded-md border bg-background p-2 font-mono text-xs"
        />
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5"><Label>{t('instances.interpreter')}</Label><Input value={interpreter} onChange={e => setInterpreter(e.target.value)} className="font-mono" /></div>
          <div className="space-y-1.5"><Label>{t('instances.timeout_sec')}</Label><Input type="number" min={1} max={600} value={timeout} onChange={e => setTimeoutVal(e.target.value)} /></div>
        </div>
        <Button size="sm" onClick={run} disabled={exec.isPending}>
          {exec.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('instances.run_script')}
        </Button>

        {r && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-xs">
              <span className="text-muted-foreground">{t('instances.exit_code')}:</span>
              <code className={r.exit_code === 0 ? 'text-success' : 'text-destructive'}>{r.exit_code ?? '—'}</code>
            </div>
            {r.error && <p className="text-xs text-destructive">{r.error}</p>}
            {r.stdout != null && r.stdout !== '' && (
              <div><p className="text-xs font-medium text-muted-foreground">stdout</p>
                <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap">{r.stdout}</pre></div>
            )}
            {r.stderr != null && r.stderr !== '' && (
              <div><p className="text-xs font-medium text-muted-foreground">stderr</p>
                <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap text-destructive">{r.stderr}</pre></div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Разобрать строку startup Proxmox ("order=1,up=30,down=60") на поля. */
function parseStartup(spec: unknown): { order: string; up: string; down: string } {
  const out = { order: '', up: '', down: '' };
  if (typeof spec !== 'string') return out;
  for (const part of spec.split(',')) {
    const [k, v] = part.split('=');
    if (k === 'order') out.order = v ?? '';
    else if (k === 'up') out.up = v ?? '';
    else if (k === 'down') out.down = v ?? '';
  }
  return out;
}

function AutostartCard({ serverId, vmid, type, node }: { serverId: number; vmid: number; type: string; node: string }) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, type, node);

  const [onboot, setOnboot] = useState(false);
  const [protection, setProtection] = useState(false);
  const [order, setOrder] = useState('');
  const [up, setUp] = useState('');
  const [down, setDown] = useState('');

  // Инициализация из каждого загруженного конфига Proxmox (в т.ч. уже закэшированного)
  const [syncedConfig, setSyncedConfig] = useState<typeof config>(undefined);
  if (config && config !== syncedConfig) {
    setSyncedConfig(config);
    setOnboot(Number(config.onboot) === 1);
    setProtection(Number(config.protection) === 1);
    const s = parseStartup(config.startup);
    setOrder(s.order);
    setUp(s.up);
    setDown(s.down);
  }

  const save = () => {
    const updates: Record<string, unknown> = {
      onboot: onboot ? 1 : 0,
      protection: protection ? 1 : 0,
    };
    // Собираем startup=order=..,up=..,down.. — либо удаляем, если всё пусто
    const parts: string[] = [];
    if (order.trim() !== '') parts.push(`order=${Number(order)}`);
    if (up.trim() !== '') parts.push(`up=${Number(up)}`);
    if (down.trim() !== '') parts.push(`down=${Number(down)}`);
    if (parts.length > 0) updates.startup = parts.join(',');
    else updates.delete = 'startup';

    updateConfig.mutate(updates, {
      onSuccess: () => toast.success(t('instances.autostart_saved')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Power className="h-4 w-4" />{t('instances.autostart')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <label className="flex items-center gap-2.5 cursor-pointer">
          <Checkbox
            checked={onboot}
            onChange={e => setOnboot(e.target.checked)}
          />
          <span className="text-sm">{t('instances.start_on_boot')}</span>
        </label>
        <p className="text-xs text-muted-foreground -mt-2">{t('instances.start_on_boot_hint')}</p>

        <div className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label htmlFor="startup-order">{t('instances.startup_order')}</Label>
            <Input id="startup-order" type="number" min={0} value={order}
              onChange={e => setOrder(e.target.value)} placeholder="any" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="startup-up">{t('instances.startup_up_delay')}</Label>
            <Input id="startup-up" type="number" min={0} value={up}
              onChange={e => setUp(e.target.value)} placeholder="0" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="startup-down">{t('instances.startup_down_timeout')}</Label>
            <Input id="startup-down" type="number" min={0} value={down}
              onChange={e => setDown(e.target.value)} placeholder="0" />
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{t('instances.startup_order_hint')}</p>

        <label className="flex items-center gap-2.5 cursor-pointer">
          <Checkbox
            checked={protection}
            onChange={e => setProtection(e.target.checked)}
          />
          <span className="text-sm">{t('instances.protection')}</span>
        </label>
        <p className="text-xs text-muted-foreground -mt-2">{t('instances.protection_hint')}</p>

        <Button size="sm" onClick={save} disabled={updateConfig.isPending}>
          {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('common.save')}
        </Button>
      </CardContent>
    </Card>
  );
}

/** Разобрать строку тегов Proxmox ("web;prod") в массив. */
function parseTags(spec: unknown): string[] {
  if (typeof spec !== 'string') return [];
  return spec.split(/[;,\s]+/).map(s => s.trim()).filter(Boolean);
}

function TagsCard({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, type, node);

  const [value, setValue] = useState<string | null>(null);
  const current = useMemo(() => parseTags(config?.tags).join(', '), [config]);
  const text = value ?? current;

  const save = () => {
    // Proxmox хранит теги через ';'; пустая строка удаляет все теги.
    const tags = parseTags(text.replace(/,/g, ';')).join(';');
    updateConfig.mutate(
      tags ? { tags } : { delete: 'tags' },
      {
        onSuccess: () => { toast.success(t('instances.tags_saved', 'Теги сохранены')); setValue(null); },
        onError: (e: Error) => toast.error(e.message),
      },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Settings className="h-4 w-4" />{t('instances.tags', 'Теги')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-1.5 min-h-[1.5rem]">
          {parseTags(text).length === 0
            ? <span className="text-xs text-muted-foreground">{t('instances.no_tags', 'Нет тегов')}</span>
            : parseTags(text).map(tag => (
              <span key={tag} className="rounded-full bg-secondary px-2 py-0.5 text-xs">{tag}</span>
            ))}
        </div>
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1.5">
            <Label htmlFor="tags-input">{t('instances.tags_hint', 'Теги через запятую')}</Label>
            <Input id="tags-input" value={text} onChange={e => setValue(e.target.value)} placeholder="web, prod, db" />
          </div>
          <Button size="sm" onClick={save} disabled={updateConfig.isPending || text === current}>
            {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('common.save', 'Сохранить')}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function SavedConfigCard({ serverId, vmid }: { serverId: number; vmid: number }) {
  const { t } = useTranslation();
  const { data } = useSavedConfig(serverId, vmid);
  if (!data?.found || !data.config) return null;
  const c = data.config;

  const rows: [string, string | number | undefined][] = [
    [t('instances.saved_name'), c.name],
    ['vCPU', c.cores],
    [`${t('nodes.memory')} (MB)`, c.memory],
    [`${t('nodes.disk')} (GB)`, c.disk_size],
    ['IP', c.ip_address ? `${c.ip_address}${c.ip_prefix ? `/${c.ip_prefix}` : ''}` : undefined],
    [t('netif.gateway'), c.gateway],
    ['DNS', c.nameserver],
    [t('instances.cloud_user'), c.cloud_init_user],
  ];

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Settings className="h-4 w-4" />{t('instances.saved_config')}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">{t('instances.saved_config_hint')}</p>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          {rows.filter(([, v]) => v != null && v !== '').map(([label, v]) => (
            <div key={label} className="flex justify-between gap-2 border-b py-1">
              <span className="text-muted-foreground">{label}</span>
              <span className="font-mono">{String(v)}</span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

// ==================== Cloud-Init ====================

function CloudInitCard({ serverId, vmid, type, node }: { serverId: number; vmid: number; type: string; node: string }) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const update = useUpdateCloudInit(serverId, vmid, node);

  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [showPw, setShowPw] = useState(false);
  const [ipMode, setIpMode] = useState<'dhcp' | 'static' | 'none'>('dhcp');
  const [ipCidr, setIpCidr] = useState('');
  const [gateway, setGateway] = useState('');
  const [nameserver, setNameserver] = useState('');
  const [searchdomain, setSearchdomain] = useState('');
  const [sshkeys, setSshkeys] = useState('');

  // Наличие cloud-init диска (ide2/scsiN со значением *-cloudinit)
  const hasCloudInit = useMemo(() => {
    if (!config) return false;
    return Object.values(config).some(v => typeof v === 'string' && v.includes('cloudinit'));
  }, [config]);

  // Refill from each newly loaded config (also the one already cached on mount).
  const [syncedConfig, setSyncedConfig] = useState<typeof config>(undefined);
  if (config && config !== syncedConfig) {
    setSyncedConfig(config);
    setUser(typeof config.ciuser === 'string' ? config.ciuser : '');
    setNameserver(typeof config.nameserver === 'string' ? config.nameserver : '');
    setSearchdomain(typeof config.searchdomain === 'string' ? config.searchdomain : '');
    // sshkeys приходит URL-кодированным
    let keys = typeof config.sshkeys === 'string' ? config.sshkeys : '';
    try { keys = decodeURIComponent(keys); } catch { /* оставляем как есть */ }
    setSshkeys(keys);
    // ipconfig0
    const ip = typeof config.ipconfig0 === 'string' ? config.ipconfig0 : '';
    if (!ip) { setIpMode('none'); setIpCidr(''); setGateway(''); }
    else if (ip.includes('ip=dhcp')) { setIpMode('dhcp'); setIpCidr(''); setGateway(''); }
    else {
      setIpMode('static');
      setIpCidr(ip.match(/ip=([^,]+)/)?.[1] ?? '');
      setGateway(ip.match(/gw=([^,]+)/)?.[1] ?? '');
    }
  }

  const save = () => {
    let ipconfig0: string | undefined;
    if (ipMode === 'dhcp') ipconfig0 = 'ip=dhcp';
    else if (ipMode === 'none') ipconfig0 = '';
    else {
      if (!ipCidr.trim()) { toast.error(t('instances.ci_ip_required')); return; }
      ipconfig0 = `ip=${ipCidr.trim()}${gateway.trim() ? `,gw=${gateway.trim()}` : ''}`;
    }
    const payload: Record<string, string> = {
      ciuser: user,
      ipconfig0,
      nameserver,
      searchdomain,
      sshkeys,
    };
    if (password) payload.cipassword = password; // пусто = не менять

    update.mutate(payload, {
      onSuccess: () => { toast.success(t('instances.ci_saved')); setPassword(''); },
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Cloud className="h-4 w-4" />{t('instances.cloud_init')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!hasCloudInit && (
          <p className="rounded-md bg-warning/10 px-3 py-2 text-xs text-warning">
            {t('instances.ci_no_drive')}
          </p>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('instances.cloud_user')}</Label>
            <Input value={user} onChange={e => setUser(e.target.value)} placeholder={t('common.placeholder_ubuntu')} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('wizard.ci_password')}</Label>
            <div className="relative">
              <Input type={showPw ? 'text' : 'password'} value={password} onChange={e => setPassword(e.target.value)} placeholder={t('instances.ci_password_keep')} />
              <Button type="button" variant="ghost" size="icon" className="absolute right-1 top-1 h-7 w-7" onClick={() => setShowPw(s => !s)}>
                {showPw ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
              </Button>
            </div>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label>{t('instances.ci_ip_mode')}</Label>
            <Select value={ipMode} onValueChange={(v) => v && setIpMode(v as 'dhcp' | 'static' | 'none')}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="dhcp">DHCP</SelectItem>
                <SelectItem value="static">{t('instances.ci_static')}</SelectItem>
                <SelectItem value="none">{t('instances.ci_ip_none')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {ipMode === 'static' && (
            <>
              <div className="space-y-1.5">
                <Label>{t('instances.ci_ip_cidr')}</Label>
                <Input value={ipCidr} onChange={e => setIpCidr(e.target.value)} placeholder="10.0.0.5/24" />
              </div>
              <div className="space-y-1.5">
                <Label>{t('netif.gateway')}</Label>
                <Input value={gateway} onChange={e => setGateway(e.target.value)} placeholder="10.0.0.1" />
              </div>
            </>
          )}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>DNS ({t('instances.ci_nameserver')})</Label>
            <Input value={nameserver} onChange={e => setNameserver(e.target.value)} placeholder="1.1.1.1" />
          </div>
          <div className="space-y-1.5">
            <Label>{t('instances.ci_searchdomain')}</Label>
            <Input value={searchdomain} onChange={e => setSearchdomain(e.target.value)} placeholder="example.com" />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label>{t('wizard.ssh_keys')}</Label>
          <textarea
            value={sshkeys}
            onChange={e => setSshkeys(e.target.value)}
            rows={3}
            spellCheck={false}
            placeholder={t('common.placeholder_ssh_key')}
            className="w-full rounded-md border bg-background p-2 font-mono text-xs"
          />
        </div>

        <Button size="sm" onClick={save} disabled={update.isPending}>
          {update.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('common.save')}
        </Button>
        <p className="text-xs text-muted-foreground">{t('instances.ci_reboot_hint')}</p>
      </CardContent>
    </Card>
  );
}

// ==================== Boot order ====================

/** Человекочитаемая подпись загрузочного устройства по его конфигу. */
function bootDeviceLabel(dev: string, spec: unknown, t: (k: string) => string): string {
  if (dev.startsWith('net')) return `${dev} · ${t('instances.boot_network')}`;
  if (typeof spec === 'string' && spec.includes('media=cdrom')) return `${dev} · CD-ROM`;
  return `${dev} · ${t('nodes.disk')}`;
}

function BootOrderCard({ serverId, vmid, type, node }: { serverId: number; vmid: number; type: string; node: string }) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, type, node);

  // Все загрузочные устройства из конфига: диски (вкл. cdrom) + сетевые.
  const allDevices = useMemo(() => {
    if (!config) return [] as string[];
    const re = /^(scsi\d+|sata\d+|virtio\d+|ide\d+|net\d+)$/;
    return Object.entries(config)
      .filter(([k, v]) => re.test(k) && typeof v === 'string' && !v.includes('cloudinit'))
      .map(([k]) => k);
  }, [config]);

  // Текущий порядок из boot=order=dev1;dev2
  const currentOrder = useMemo(() => {
    const raw = typeof config?.boot === 'string' ? config.boot : '';
    const list = raw.match(/order=([^,]+)/)?.[1]?.split(';').map(s => s.trim()).filter(Boolean) ?? [];
    return list.filter(d => allDevices.includes(d));
  }, [config?.boot, allDevices]);

  const [order, setOrder] = useState<string[]>([]);
  useEffect(() => { setOrder(currentOrder); }, [currentOrder]);

  const disabled = allDevices.filter(d => !order.includes(d));

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= order.length) return;
    const next = [...order];
    [next[i], next[j]] = [next[j], next[i]];
    setOrder(next);
  };
  const enable = (dev: string) => setOrder([...order, dev]);
  const disable = (dev: string) => setOrder(order.filter(d => d !== dev));

  const dirty = order.join(';') !== currentOrder.join(';');

  const save = () => {
    updateConfig.mutate(
      { boot: `order=${order.join(';')}` },
      {
        onSuccess: () => toast.success(t('instances.boot_order_saved')),
        onError: (e: Error) => toast.error(e.message),
      }
    );
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <ListOrdered className="h-4 w-4" />{t('instances.boot_order')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">{t('instances.boot_order_hint')}</p>

        {/* Активный порядок */}
        <div className="space-y-1.5">
          {order.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t('instances.boot_none_enabled')}</p>
          ) : order.map((dev, i) => (
            <div key={dev} className="flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm">
              <span className="w-5 text-center text-xs text-muted-foreground">{i + 1}</span>
              <span className="flex-1 font-mono text-xs">{bootDeviceLabel(dev, config?.[dev], t)}</span>
              <Button variant="ghost" size="icon" className="h-6 w-6" disabled={i === 0} onClick={() => move(i, -1)}>
                <ArrowUp className="h-3.5 w-3.5" />
              </Button>
              <Button variant="ghost" size="icon" className="h-6 w-6" disabled={i === order.length - 1} onClick={() => move(i, 1)}>
                <ArrowDown className="h-3.5 w-3.5" />
              </Button>
              <Button variant="ghost" size="sm" className="h-6 px-2 text-xs text-muted-foreground" onClick={() => disable(dev)}>
                {t('instances.boot_disable')}
              </Button>
            </div>
          ))}
        </div>

        {/* Отключённые устройства */}
        {disabled.length > 0 && (
          <div className="space-y-1.5 border-t pt-3">
            <Label className="text-xs text-muted-foreground">{t('instances.boot_disabled')}</Label>
            {disabled.map((dev) => (
              <div key={dev} className="flex items-center gap-2 rounded-md border border-dashed px-3 py-1.5 text-sm">
                <span className="flex-1 font-mono text-xs text-muted-foreground">{bootDeviceLabel(dev, config?.[dev], t)}</span>
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => enable(dev)}>
                  {t('instances.boot_enable')}
                </Button>
              </div>
            ))}
          </div>
        )}

        <Button size="sm" onClick={save} disabled={!dirty || updateConfig.isPending}>
          {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {t('common.save')}
        </Button>
      </CardContent>
    </Card>
  );
}

// ==================== VM options & unlock ====================

const HOTPLUG_DEFAULT = 'network,disk,usb';

/** Простой тумблер-строка. */
function OptionToggle({ label, hint, checked, onChange }: {
  label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-start justify-between gap-3 cursor-pointer py-1">
      <div>
        <span className="text-sm">{label}</span>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
      <Checkbox checked={checked} onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5" />
    </label>
  );
}

function VmOptionsCard({ serverId, vmid, type, node }: { serverId: number; vmid: number; type: string; node: string }) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);
  const updateConfig = useUpdateConfig(serverId, vmid, type, node);
  const unlock = useUnlockInstance(serverId, vmid, type, node);

  const [agent, setAgent] = useState(false);
  const [tablet, setTablet] = useState(true);
  const [kvm, setKvm] = useState(true);
  const [hotplug, setHotplug] = useState(true);

  // Refill from each newly loaded config (also the one already cached on mount).
  const [syncedConfig, setSyncedConfig] = useState<typeof config>(undefined);
  if (config && config !== syncedConfig) {
    setSyncedConfig(config);
    const agentRaw = typeof config.agent === 'string' ? config.agent : String(config.agent ?? '');
    setAgent(agentRaw.startsWith('1'));
    setTablet(config.tablet == null ? true : Number(config.tablet) === 1);
    setKvm(config.kvm == null ? true : Number(config.kvm) === 1);
    const hp = typeof config.hotplug === 'string' ? config.hotplug : '';
    setHotplug(config.hotplug == null ? true : hp !== '0' && hp !== '');
  }

  const lock = typeof config?.lock === 'string' ? config.lock : '';

  const save = () => {
    const updates: Record<string, unknown> = {
      agent: agent ? 1 : 0,
      tablet: tablet ? 1 : 0,
      kvm: kvm ? 1 : 0,
      hotplug: hotplug ? HOTPLUG_DEFAULT : '0',
    };
    updateConfig.mutate(updates, {
      onSuccess: () => toast.success(t('instances.vm_options_saved')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const doUnlock = () => {
    unlock.mutate(undefined, {
      onSuccess: () => toast.success(t('instances.unlocked')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <SlidersHorizontal className="h-4 w-4" />{t('instances.vm_options')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        <OptionToggle label={t('instances.opt_agent')} hint={t('instances.opt_agent_hint')} checked={agent} onChange={setAgent} />
        <OptionToggle label={t('instances.opt_tablet')} hint={t('instances.opt_tablet_hint')} checked={tablet} onChange={setTablet} />
        <OptionToggle label={t('instances.opt_kvm')} hint={t('instances.opt_kvm_hint')} checked={kvm} onChange={setKvm} />
        <OptionToggle label={t('instances.opt_hotplug')} hint={t('instances.opt_hotplug_hint')} checked={hotplug} onChange={setHotplug} />
        <div className="flex items-center gap-2 pt-2">
          <Button size="sm" onClick={save} disabled={updateConfig.isPending}>
            {updateConfig.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('common.save')}
          </Button>
        </div>

        {/* Unlock */}
        <div className="mt-3 flex items-center justify-between gap-3 border-t pt-3">
          <div>
            <span className="text-sm">{t('instances.unlock')}</span>
            <p className="text-xs text-muted-foreground">
              {lock ? t('instances.lock_current', { lock }) : t('instances.no_lock')}
            </p>
          </div>
          <Button size="sm" variant="outline" onClick={doUnlock} disabled={unlock.isPending}>
            {unlock.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <LockOpen className="mr-2 h-4 w-4" />}
            {t('instances.unlock')}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default function SettingsTab({ serverId, vmid, type, node }: Props) {
  const { t } = useTranslation();
  const { data: config } = useVMConfig(serverId, vmid, type, node);

  return (
    <div className="space-y-6 max-w-2xl">
      <OwnerCard serverId={serverId} vmid={vmid} />

      <AutostartCard serverId={serverId} vmid={vmid} type={type} node={node} />

      <TagsCard serverId={serverId} vmid={vmid} type={type} node={node} />

      {type !== 'lxc' && <ExecuteScriptCard serverId={serverId} vmid={vmid} node={node} />}

      {/* Для QEMU вычислительные ресурсы, опции CPU, управление дисками и ресайз
          диска живут на вкладке «Оборудование» (которой у LXC нет — поэтому
          здесь эти карточки остаются только для контейнеров). */}
      {type === 'lxc' && <ComputeResourcesCard serverId={serverId} vmid={vmid} type={type} node={node} />}

      {type !== 'lxc' && <CloudInitCard serverId={serverId} vmid={vmid} type={type} node={node} />}

      {type !== 'lxc' && <BootOrderCard serverId={serverId} vmid={vmid} type={type} node={node} />}

      {type !== 'lxc' && <VmOptionsCard serverId={serverId} vmid={vmid} type={type} node={node} />}

      {type === 'lxc' && <DiskResizeCard serverId={serverId} vmid={vmid} type={type} node={node} />}

      {type === 'lxc' && <DiskMoveCard serverId={serverId} vmid={vmid} type={type} node={node} />}

      <SavedConfigCard serverId={serverId} vmid={vmid} />

      {/* Raw Config */}
      {config && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-semibold">{t('instances.raw_configuration')}</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="overflow-auto rounded-md bg-muted p-3 text-xs font-mono max-h-64">
              {JSON.stringify(config, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
