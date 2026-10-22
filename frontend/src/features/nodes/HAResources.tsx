import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Trash2, ShieldCheck, RefreshCw } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useHAStatus, useAddToHA, useRemoveFromHA } from '@/hooks/use-ha';
import type { VMInstance } from '@/types';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';

const HA_STATES = ['started', 'stopped', 'enabled', 'disabled', 'ignored'];

interface AddForm {
  guest: string; // "vm:100"
  state: string;
  group: string;
  max_restart: string;
  max_relocate: string;
  comment: string;
}

const emptyAddForm: AddForm = {
  guest: '',
  state: 'started',
  group: '',
  max_restart: '1',
  max_relocate: '1',
  comment: '',
};

export default function HAResources({ serverId, vms }: { serverId: number; vms: VMInstance[] }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data, isLoading, isFetching, refetch } = useHAStatus(serverId);
  const addHA = useAddToHA(serverId);
  const removeHA = useRemoveFromHA(serverId);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<AddForm>(emptyAddForm);
  const set = <K extends keyof AddForm>(k: K, v: AddForm[K]) => setForm(p => ({ ...p, [k]: v }));

  const resources = useMemo(() => data?.resources ?? [], [data]);
  const groups = data?.groups ?? [];

  // Guests not yet managed by HA, for the add dialog
  const candidates = useMemo(() => {
    const inHA = new Set(resources.map(r => r.sid));
    return vms
      .map(vm => ({ vm, sid: `${vm.type === 'qemu' ? 'vm' : 'ct'}:${vm.vmid}` }))
      .filter(({ sid }) => !inHA.has(sid));
  }, [vms, resources]);

  const handleAdd = () => {
    if (!form.guest) { toast.error(t('ha.select_guest')); return; }
    const [vmType, vmidStr] = form.guest.split(':');
    addHA.mutate(
      {
        vmType: vmType as 'vm' | 'ct',
        vmid: Number(vmidStr),
        state: form.state,
        group: form.group || undefined,
        max_restart: Number(form.max_restart) || 0,
        max_relocate: Number(form.max_relocate) || 0,
        comment: form.comment || undefined,
      },
      {
        onSuccess: () => { toast.success(t('ha.added')); setDialogOpen(false); setForm(emptyAddForm); },
        onError: (err: Error) => toast.error(err.message),
      },
    );
  };

  const handleRemove = async (sid: string) => {
    if (!await confirm(`${t('ha.remove_confirm')} "${sid}"?`)) return;
    const [vmType, vmidStr] = sid.split(':');
    removeHA.mutate(
      { vmType: vmType as 'vm' | 'ct', vmid: Number(vmidStr) },
      {
        onSuccess: () => toast.success(t('ha.removed')),
        onError: (err: Error) => toast.error(err.message),
      },
    );
  };

  const nameForSid = (sid: string) => {
    const [, vmidStr] = sid.split(':');
    const vm = vms.find(v => String(v.vmid) === vmidStr);
    return vm?.name;
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-lg">
          <ShieldCheck className="h-5 w-5" />
          {t('ha.title')}
        </CardTitle>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </Button>
          <Button
            size="sm"
            onClick={() => { setForm(emptyAddForm); setDialogOpen(true); }}
            disabled={!data?.is_cluster || candidates.length === 0}
          >
            <Plus className="mr-1 h-4 w-4" />{t('ha.add')}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.loading')}</p>
        ) : !data?.is_cluster ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('ha.cluster_only')}</p>
        ) : (
          <>
            {resources.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">{t('ha.no_resources')}</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('ha.resource')}</TableHead>
                    <TableHead>{t('ha.state')}</TableHead>
                    <TableHead>{t('ha.group')}</TableHead>
                    <TableHead>{t('ha.max_restart')}</TableHead>
                    <TableHead>{t('ha.max_relocate')}</TableHead>
                    <TableHead>{t('netif.comments')}</TableHead>
                    <TableHead className="text-right">{t('common.actions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {resources.map(r => (
                    <TableRow key={r.sid}>
                      <TableCell className="font-mono font-medium">
                        {r.sid}
                        {nameForSid(r.sid) && (
                          <span className="ml-2 text-xs text-muted-foreground">{nameForSid(r.sid)}</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant={r.state === 'started' ? 'default' : 'secondary'}>{r.state || '—'}</Badge>
                      </TableCell>
                      <TableCell>{r.group || '—'}</TableCell>
                      <TableCell>{r.max_restart ?? '—'}</TableCell>
                      <TableCell>{r.max_relocate ?? '—'}</TableCell>
                      <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">{r.comment || '—'}</TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => handleRemove(r.sid)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}

            {groups.length > 0 && (
              <p className="text-xs text-muted-foreground">
                {t('ha.groups')}: {groups.map(g => g.group).join(', ')}
              </p>
            )}
          </>
        )}
      </CardContent>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('ha.add')}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>{t('ha.resource')}</Label>
              <Select value={form.guest} onValueChange={v => { if (v) set('guest', v); }}>
                <SelectTrigger className="mt-1"><SelectValue placeholder={t('ha.select_guest')} /></SelectTrigger>
                <SelectContent>
                  {candidates.map(({ vm, sid }) => (
                    <SelectItem key={sid} value={sid}>
                      {sid} {vm.name ? `— ${vm.name}` : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>{t('ha.state')}</Label>
                <Select value={form.state} onValueChange={v => { if (v) set('state', v); }}>
                  <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {HA_STATES.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>{t('ha.group')}</Label>
                {groups.length > 0 ? (
                  <Select value={form.group || '__none'} onValueChange={v => set('group', v && v !== '__none' ? v : '')}>
                    <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none">{t('ha.no_group')}</SelectItem>
                      {groups.map(g => <SelectItem key={g.group} value={g.group}>{g.group}</SelectItem>)}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input value={form.group} onChange={e => set('group', e.target.value)} className="mt-1" placeholder={t('ha.no_group')} />
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>{t('ha.max_restart')}</Label>
                <Input type="number" min="0" value={form.max_restart} onChange={e => set('max_restart', e.target.value)} className="mt-1" />
              </div>
              <div>
                <Label>{t('ha.max_relocate')}</Label>
                <Input type="number" min="0" value={form.max_relocate} onChange={e => set('max_relocate', e.target.value)} className="mt-1" />
              </div>
            </div>

            <div>
              <Label>{t('netif.comments')}</Label>
              <Input value={form.comment} onChange={e => set('comment', e.target.value)} className="mt-1" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>{t('common.cancel')}</Button>
            <Button onClick={handleAdd} disabled={addHA.isPending || !form.guest}>{t('ha.add')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
