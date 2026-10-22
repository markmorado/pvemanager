import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Trash2, RefreshCw, FolderTree, ChevronRight } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  usePools, usePool, useCreatePool, useUpdatePool, useDeletePool,
} from '@/hooks/use-pools';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';

interface PoolVm {
  vmid: number;
  name?: string;
  type: string;
  node?: string;
}

// ==================== Pool members (expandable) ====================

function PoolMembers({ serverId, poolid, vms }: { serverId: number; poolid: string; vms: PoolVm[] }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data } = usePool(serverId, poolid);
  const updatePool = useUpdatePool(serverId);
  const [addVmid, setAddVmid] = useState('');

  const members = useMemo(() => data?.pool?.members ?? [], [data]);
  const available = useMemo(() => {
    const memberVmids = new Set(members.filter(m => m.vmid != null).map(m => m.vmid));
    return vms.filter(v => !memberVmids.has(v.vmid));
  }, [vms, members]);

  const add = () => {
    if (!addVmid) return;
    updatePool.mutate({ poolid, data: { vms: addVmid } }, {
      onSuccess: () => { toast.success(t('pools.member_added', 'Добавлено в пул')); setAddVmid(''); },
      onError: (e: Error) => toast.error(e.message),
    });
  };
  const remove = async (vmid: number) => {
    if (!await confirm(t('pools.remove_member_confirm', 'Убрать из пула?'))) return;
    updatePool.mutate({ poolid, data: { vms: String(vmid), delete: 1 } }, {
      onSuccess: () => toast.success(t('pools.member_removed', 'Убрано из пула')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <div className="space-y-2 pt-2">
      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <Label className="text-xs">{t('pools.add_member', 'Добавить VM/LXC')}</Label>
          <Select value={addVmid} onValueChange={(v) => { if (v) setAddVmid(v); }}>
            <SelectTrigger className="w-full h-8"><SelectValue placeholder={t('pools.select_vm', 'Выберите инстанс')} /></SelectTrigger>
            <SelectContent>
              {available.length === 0
                ? <div className="px-2 py-1.5 text-xs text-muted-foreground">{t('pools.no_available', 'Нет доступных')}</div>
                : available.map(v => (
                  <SelectItem key={v.vmid} value={String(v.vmid)}>
                    <span className="flex items-center gap-2">
                      <Badge variant="outline" className="text-2xs">{v.type === 'qemu' ? 'VM' : 'LXC'}</Badge>
                      {v.name || `#${v.vmid}`} <span className="text-xs text-muted-foreground">#{v.vmid}</span>
                    </span>
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
        <Button size="sm" onClick={add} disabled={!addVmid || updatePool.isPending}><Plus className="h-3.5 w-3.5" /></Button>
      </div>
      {members.length === 0 ? (
        <p className="py-2 text-center text-xs text-muted-foreground">{t('pools.empty', 'Пул пуст')}</p>
      ) : (
        <div className="space-y-1">
          {members.map(m => (
            <div key={m.id} className="flex items-center gap-2 rounded border px-2 py-1 text-xs">
              <Badge variant="outline" className="text-2xs">
                {m.type === 'storage' ? 'STORAGE' : m.type === 'qemu' ? 'VM' : 'LXC'}
              </Badge>
              <span className="font-medium">{m.name || m.storage || m.id}</span>
              {m.vmid != null && <span className="font-mono text-muted-foreground">#{m.vmid}</span>}
              {m.node && <span className="text-muted-foreground">{m.node}</span>}
              {m.vmid != null && (
                <Button variant="ghost" size="icon" className="ml-auto h-6 w-6 text-destructive" onClick={() => remove(m.vmid!)}>
                  <Trash2 className="h-3 w-3" />
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ==================== Main ====================

export default function PoolManager({ serverId, vms }: { serverId: number; vms: PoolVm[] }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data, isLoading, refetch, isFetching } = usePools(serverId);
  const createPool = useCreatePool(serverId);
  const deletePool = useDeletePool(serverId);

  const pools = data?.pools ?? [];
  const [expanded, setExpanded] = useState<string | null>(null);
  const [dialog, setDialog] = useState(false);
  const [poolid, setPoolid] = useState('');
  const [comment, setComment] = useState('');

  const create = () => {
    if (!poolid.trim()) { toast.error(t('pools.err_id', 'Укажите имя пула')); return; }
    createPool.mutate({ poolid: poolid.trim(), comment: comment.trim() || undefined }, {
      onSuccess: () => { toast.success(t('pools.created', 'Пул создан')); setDialog(false); setPoolid(''); setComment(''); },
      onError: (e: Error) => toast.error(e.message),
    });
  };
  const remove = async (p: string) => {
    if (!await confirm(`${t('pools.delete', 'Удалить пул')} "${p}"? ${t('pools.delete_hint', '(пул должен быть пустым)')}`)) return;
    deletePool.mutate(p, { onSuccess: () => toast.success(t('pools.deleted', 'Пул удалён')), onError: (e: Error) => toast.error(e.message) });
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-lg">
          <FolderTree className="h-5 w-5" />
          {t('pools.title', 'Пулы ресурсов')} ({pools.length})
        </CardTitle>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </Button>
          <Button size="sm" onClick={() => setDialog(true)}>
            <Plus className="mr-1 h-4 w-4" />{t('pools.add', 'Создать пул')}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
        ) : pools.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
        ) : (
          <div className="space-y-2">
            {pools.map(p => (
              <div key={p.poolid} className="rounded-md border">
                <div className="flex items-center gap-2 px-3 py-2">
                  <button className="flex flex-1 items-center gap-2 text-left" onClick={() => setExpanded(prev => prev === p.poolid ? null : p.poolid)}>
                    <ChevronRight className={`h-4 w-4 transition-transform ${expanded === p.poolid ? 'rotate-90' : ''}`} />
                    <span className="font-mono font-medium">{p.poolid}</span>
                    {p.comment && <span className="text-xs text-muted-foreground">— {p.comment}</span>}
                  </button>
                  <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => remove(p.poolid)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {expanded === p.poolid && (
                  <div className="px-3 pb-3"><PoolMembers serverId={serverId} poolid={p.poolid} vms={vms} /></div>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>

      <Dialog open={dialog} onOpenChange={(v) => { setDialog(v); if (!v) { setPoolid(''); setComment(''); } }}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('pools.add', 'Создать пул')}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label>{t('pools.pool_id', 'Имя пула')}</Label>
              <Input value={poolid} onChange={(e) => setPoolid(e.target.value)} className="font-mono" placeholder="production" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('common.comment', 'Комментарий')}</Label>
              <Input value={comment} onChange={(e) => setComment(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(false)}>{t('common.cancel', 'Отмена')}</Button>
            <Button onClick={create} disabled={createPool.isPending}>{t('common.add', 'Создать')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
