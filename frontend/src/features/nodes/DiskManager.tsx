import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { HardDrive, RefreshCw, Activity, Trash2, Plus, Database, Eraser } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  useNodeDisks, useDiskSmart, useZfsPools, useCreateZfsPool, useDestroyZfsPool, useWipeDisk,
  type NodeDisk,
} from '@/hooks/use-disks';
import { formatBytes } from '@/lib/format';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';

const RAID_LEVELS = ['single', 'mirror', 'raid10', 'raidz', 'raidz2', 'raidz3'];
const COMPRESSIONS = ['on', 'off', 'lz4', 'zstd', 'gzip'];

function healthVariant(h?: string): 'default' | 'secondary' | 'destructive' {
  if (!h) return 'secondary';
  const up = h.toUpperCase();
  if (up === 'PASSED' || up === 'OK' || up === 'ONLINE') return 'default';
  if (up.includes('FAIL') || up === 'DEGRADED') return 'destructive';
  return 'secondary';
}

// ==================== SMART dialog ====================

function SmartDialog({ serverId, node, disk, onClose }: { serverId: number; node: string; disk: string | null; onClose: () => void }) {
  const { t } = useTranslation();
  const { data, isLoading } = useDiskSmart(serverId, node, disk ?? '', !!disk);
  const smart = data?.smart;
  const attrs = smart?.attributes ?? [];

  return (
    <Dialog open={!!disk} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Activity className="h-4 w-4" />SMART — <span className="font-mono">{disk}</span>
          </DialogTitle>
          {smart?.health && (
            <DialogDescription>
              {t('disks.health', 'Состояние')}: <Badge variant={healthVariant(smart.health)}>{smart.health}</Badge>
            </DialogDescription>
          )}
        </DialogHeader>
        {isLoading ? (
          <p className="py-6 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
        ) : smart?.error ? (
          <p className="py-6 text-center text-sm text-destructive">{smart.error}</p>
        ) : attrs.length > 0 ? (
          <div className="max-h-96 overflow-auto">
            <Table>
              <TableHeader><TableRow>
                <TableHead>ID</TableHead><TableHead>{t('disks.attribute', 'Атрибут')}</TableHead>
                <TableHead>{t('disks.value', 'Знач.')}</TableHead><TableHead>{t('disks.worst', 'Худш.')}</TableHead>
                <TableHead>{t('disks.threshold', 'Порог')}</TableHead><TableHead>RAW</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {attrs.map((a, i) => (
                  <TableRow key={a.id ?? i}>
                    <TableCell className="text-xs text-muted-foreground">{a.id ?? '—'}</TableCell>
                    <TableCell className="text-xs">{a.name ?? '—'}</TableCell>
                    <TableCell className="text-xs tabular-nums">{a.value ?? '—'}</TableCell>
                    <TableCell className="text-xs tabular-nums">{a.worst ?? '—'}</TableCell>
                    <TableCell className="text-xs tabular-nums">{a.threshold ?? '—'}</TableCell>
                    <TableCell className="font-mono text-xs">{a.raw ?? '—'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : smart?.text ? (
          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs font-mono whitespace-pre-wrap">{smart.text}</pre>
        ) : (
          <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ==================== Create ZFS pool dialog ====================

function CreateZfsDialog({ serverId, node, disks, open, onClose }: {
  serverId: number; node: string; disks: NodeDisk[]; open: boolean; onClose: () => void;
}) {
  const { t } = useTranslation();
  const createPool = useCreateZfsPool(serverId, node);
  const [name, setName] = useState('');
  const [raidlevel, setRaid] = useState('single');
  const [ashift, setAshift] = useState('12');
  const [compression, setCompression] = useState('on');
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Свободные диски (без файловой системы / роли) — кандидаты под пул.
  const available = useMemo(() => disks.filter(d => !d.used), [disks]);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) { setName(''); setRaid('single'); setAshift('12'); setCompression('on'); setSelected(new Set()); }
  }

  const toggle = (dev: string) => setSelected(prev => {
    const next = new Set(prev);
    if (next.has(dev)) next.delete(dev); else next.add(dev);
    return next;
  });

  const submit = () => {
    if (!name.trim()) { toast.error(t('disks.err_name', 'Укажите имя пула')); return; }
    if (selected.size === 0) { toast.error(t('disks.err_devices', 'Выберите диски')); return; }
    createPool.mutate(
      { name: name.trim(), devices: Array.from(selected).join(','), raidlevel, ashift: Number(ashift), compression, add_storage: true },
      {
        onSuccess: () => { toast.success(t('disks.pool_created', 'ZFS-пул создаётся')); onClose(); },
        onError: (e: Error) => toast.error(e.message),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('disks.create_zfs', 'Создать ZFS-пул')}</DialogTitle>
          <DialogDescription>{t('disks.create_zfs_hint', 'Диски будут очищены и объединены в пул.')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('disks.pool_name', 'Имя пула')}</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} className="font-mono" placeholder="tank" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('disks.raid_level', 'RAID-уровень')}</Label>
              <Select value={raidlevel} onValueChange={(v) => { if (v) setRaid(v); }}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{RAID_LEVELS.map(r => <SelectItem key={r} value={r}>{r}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>ashift</Label>
              <Input type="number" min={9} max={16} value={ashift} onChange={(e) => setAshift(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>{t('disks.compression', 'Сжатие')}</Label>
              <Select value={compression} onValueChange={(v) => { if (v) setCompression(v); }}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{COMPRESSIONS.map(c => <SelectItem key={c} value={c}>{c}</SelectItem>)}</SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>{t('disks.devices', 'Диски')} ({selected.size})</Label>
            <div className="max-h-48 space-y-1 overflow-auto rounded-md border p-2">
              {available.length === 0
                ? <p className="py-2 text-center text-xs text-muted-foreground">{t('disks.no_free_disks', 'Нет свободных дисков')}</p>
                : available.map(d => (
                  <label key={d.devpath} className="flex items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-muted/50">
                    <Checkbox checked={selected.has(d.devpath)} onChange={() => toggle(d.devpath)} />
                    <span className="font-mono text-xs">{d.devpath}</span>
                    <span className="text-xs text-muted-foreground">{formatBytes(d.size || 0)}</span>
                    {d.model && <span className="text-xs text-muted-foreground truncate">{d.model}</span>}
                  </label>
                ))}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={submit} disabled={createPool.isPending}>{t('common.create', 'Создать')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Main ====================

export default function DiskManager({ serverId, nodeNames }: { serverId: number; nodeNames: string[] }) {
  const { t } = useTranslation();
  const confirm = useConfirm();

  const [nodePick, setNode] = useState('');
  // The picked node, or the first one while none (or a vanished one) is picked.
  const node = nodeNames.includes(nodePick) ? nodePick : (nodeNames[0] ?? '');

  const { data: disksData, isLoading, refetch, isFetching } = useNodeDisks(serverId, node);
  const { data: zfsData } = useZfsPools(serverId, node);
  const destroyPool = useDestroyZfsPool(serverId, node);
  const wipeDisk = useWipeDisk(serverId, node);

  const disks = disksData?.disks ?? [];
  const pools = zfsData?.pools ?? [];

  const [smartDisk, setSmartDisk] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const wipe = async (dev: string) => {
    if (!await confirm(`${t('disks.wipe_confirm', 'Очистить диск (все данные будут уничтожены)')} ${dev}?`)) return;
    wipeDisk.mutate(dev, { onSuccess: () => toast.success(t('disks.wiped', 'Диск очищается')), onError: (e: Error) => toast.error(e.message) });
  };
  const destroy = async (name: string) => {
    if (!await confirm(`${t('disks.destroy_pool', 'Уничтожить ZFS-пул')} "${name}"?`)) return;
    destroyPool.mutate({ name }, { onSuccess: () => toast.success(t('disks.pool_destroyed', 'Пул уничтожается')), onError: (e: Error) => toast.error(e.message) });
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-lg">
          <HardDrive className="h-5 w-5" />{t('disks.title', 'Диски и ZFS')}
        </CardTitle>
        <div className="flex items-center gap-2">
          {nodeNames.length > 1 && (
            <Select value={node} onValueChange={(v) => { if (v) setNode(v); }}>
              <SelectTrigger className="w-[140px] h-8"><SelectValue /></SelectTrigger>
              <SelectContent>{nodeNames.map(n => <SelectItem key={n} value={n}>{n}</SelectItem>)}</SelectContent>
            </Select>
          )}
          <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {!node ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
        ) : (
          <Tabs defaultValue="disks">
            <TabsList>
              <TabsTrigger value="disks">{t('disks.physical', 'Диски')} ({disks.length})</TabsTrigger>
              <TabsTrigger value="zfs">ZFS ({pools.length})</TabsTrigger>
            </TabsList>

            {/* Physical disks */}
            <TabsContent value="disks" className="mt-4">
              {isLoading ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
              ) : disks.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
              ) : (
                <Table>
                  <TableHeader><TableRow>
                    <TableHead>{t('disks.device', 'Устройство')}</TableHead>
                    <TableHead>{t('disks.type', 'Тип')}</TableHead>
                    <TableHead>{t('disks.model', 'Модель')}</TableHead>
                    <TableHead>{t('disks.size', 'Объём')}</TableHead>
                    <TableHead>{t('disks.usage', 'Использование')}</TableHead>
                    <TableHead>{t('disks.health', 'Состояние')}</TableHead>
                    <TableHead className="text-right">{t('common.actions', 'Действия')}</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {disks.map(d => (
                      <TableRow key={d.devpath}>
                        <TableCell className="font-mono text-xs font-medium">{d.devpath}</TableCell>
                        <TableCell><Badge variant="outline" className="uppercase text-2xs">{d.type || '—'}</Badge></TableCell>
                        <TableCell className="text-xs">{d.model || '—'}{d.serial ? <span className="block text-2xs text-muted-foreground font-mono">{d.serial}</span> : null}</TableCell>
                        <TableCell className="text-xs tabular-nums">{formatBytes(d.size || 0)}</TableCell>
                        <TableCell className="text-xs">{d.used || <span className="text-muted-foreground">{t('disks.free', 'свободен')}</span>}</TableCell>
                        <TableCell>
                          {d.health ? <Badge variant={healthVariant(d.health)}>{d.health}</Badge> : <span className="text-xs text-muted-foreground">—</span>}
                          {d.wearout != null && d.wearout !== '' && <span className="ml-1 text-2xs text-muted-foreground">wear {d.wearout}%</span>}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-1">
                            <Button variant="ghost" size="icon" className="h-7 w-7" title="SMART" onClick={() => setSmartDisk(d.devpath)}>
                              <Activity className="h-3.5 w-3.5" />
                            </Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" title={t('disks.wipe', 'Очистить')}
                              disabled={!!d.used} onClick={() => wipe(d.devpath)}>
                              <Eraser className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </TabsContent>

            {/* ZFS pools */}
            <TabsContent value="zfs" className="mt-4 space-y-3">
              <div className="flex justify-end">
                <Button size="sm" onClick={() => setCreateOpen(true)}>
                  <Plus className="mr-1 h-4 w-4" />{t('disks.create_zfs', 'Создать ZFS-пул')}
                </Button>
              </div>
              {pools.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
              ) : (
                <Table>
                  <TableHeader><TableRow>
                    <TableHead>{t('disks.pool_name', 'Имя')}</TableHead>
                    <TableHead>{t('disks.size', 'Объём')}</TableHead>
                    <TableHead>{t('disks.alloc', 'Занято')}</TableHead>
                    <TableHead>{t('disks.free', 'Свободно')}</TableHead>
                    <TableHead>{t('disks.health', 'Состояние')}</TableHead>
                    <TableHead className="text-right">{t('common.actions', 'Действия')}</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {pools.map(p => (
                      <TableRow key={p.name}>
                        <TableCell className="font-mono font-medium flex items-center gap-2"><Database className="h-3.5 w-3.5 text-muted-foreground" />{p.name}</TableCell>
                        <TableCell className="text-xs tabular-nums">{p.size ? formatBytes(p.size) : '—'}</TableCell>
                        <TableCell className="text-xs tabular-nums">{p.alloc != null ? formatBytes(p.alloc) : '—'}</TableCell>
                        <TableCell className="text-xs tabular-nums">{p.free != null ? formatBytes(p.free) : '—'}</TableCell>
                        <TableCell>{p.health ? <Badge variant={healthVariant(p.health)}>{p.health}</Badge> : '—'}</TableCell>
                        <TableCell className="text-right">
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => destroy(p.name)}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </TabsContent>
          </Tabs>
        )}
      </CardContent>

      <SmartDialog serverId={serverId} node={node} disk={smartDisk} onClose={() => setSmartDisk(null)} />
      <CreateZfsDialog serverId={serverId} node={node} disks={disks} open={createOpen} onClose={() => setCreateOpen(false)} />
    </Card>
  );
}
