import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Pencil, Trash2, Loader2 } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  useImageMirrors,
  useCreateMirror,
  useUpdateMirror,
  useDeleteMirror,
} from '@/hooks/use-image-catalog';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { toast } from 'sonner';
import type { ImageMirrorItem, ImageMirrorInput } from '@/types';

const EMPTY: ImageMirrorInput = {
  name: '', kind: 'qcow2', os: '', version: '', arch: 'amd64',
  url: '', template: '', checksum: '', checksum_algorithm: '', description: '', enabled: true,
};

function MirrorFormDialog({
  open, onClose, editing,
}: {
  open: boolean;
  onClose: () => void;
  editing: ImageMirrorItem | null;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<ImageMirrorInput>(EMPTY);
  const create = useCreateMirror();
  const update = useUpdateMirror();
  const pending = create.isPending || update.isPending;

  // Refill the form each time the dialog opens (or is pointed at another mirror).
  const [filledFor, setFilledFor] = useState({ open: false, editing: null as ImageMirrorItem | null });
  if (filledFor.open !== open || filledFor.editing !== editing) {
    setFilledFor({ open, editing });
    if (open && editing) {
      setForm({
        name: editing.name, kind: editing.kind, os: editing.os || '', version: editing.version || '',
        arch: editing.arch, url: editing.url || '', template: editing.template || '',
        checksum: editing.checksum || '', checksum_algorithm: editing.checksum_algorithm || '',
        description: editing.description || '', enabled: editing.enabled,
      });
    } else if (open) {
      setForm(EMPTY);
    }
  }

  const set = (k: keyof ImageMirrorInput, v: string | boolean) => setForm((f) => ({ ...f, [k]: v }));

  const submit = () => {
    if (!form.name.trim()) return;
    if ((form.kind === 'qcow2' || form.kind === 'iso') && !form.url?.trim()) {
      toast.error(t('mirrors.url_required', { kind: form.kind }));
      return;
    }
    if (!form.url?.trim() && !form.template?.trim()) {
      toast.error(t('mirrors.url_or_template_required'));
      return;
    }
    const payload: ImageMirrorInput = {
      ...form,
      os: form.os?.trim() || undefined,
      version: form.version?.trim() || undefined,
      url: form.url?.trim() || undefined,
      template: form.template?.trim() || undefined,
      checksum: form.checksum?.trim() || undefined,
      checksum_algorithm: form.checksum_algorithm?.trim() || undefined,
      description: form.description?.trim() || undefined,
    };
    const opts = {
      onSuccess: () => { toast.success(t('mirrors.saved')); onClose(); },
      onError: (e: Error) => toast.error(e.message),
    };
    if (editing) update.mutate({ id: editing.mirror_id, data: payload }, opts);
    else create.mutate(payload, opts);
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{editing ? t('mirrors.edit_title') : t('mirrors.add_title')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 max-h-[60vh] overflow-y-auto pr-1">
          <div>
            <Label htmlFor="m-name">{t('mirrors.name')}</Label>
            <Input id="m-name" value={form.name} onChange={(e) => set('name', e.target.value)} />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <Label>{t('mirrors.kind')}</Label>
              <Select value={form.kind} onValueChange={(v) => { if (v !== null) set('kind', v); }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="qcow2">{t('mirrors.kind_qcow2')}</SelectItem>
                  <SelectItem value="vztmpl">{t('mirrors.kind_vztmpl')}</SelectItem>
                  <SelectItem value="iso">{t('mirrors.kind_iso')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>{t('mirrors.arch')}</Label>
              <Select value={form.arch} onValueChange={(v) => { if (v !== null) set('arch', v); }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="amd64">amd64</SelectItem>
                  <SelectItem value="arm64">arm64</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label htmlFor="m-os">{t('mirrors.os')}</Label>
              <Input id="m-os" placeholder="ubuntu" value={form.os} onChange={(e) => set('os', e.target.value)} />
            </div>
          </div>
          <div>
            <Label htmlFor="m-ver">{t('mirrors.version')}</Label>
            <Input id="m-ver" placeholder="24.04" value={form.version} onChange={(e) => set('version', e.target.value)} />
          </div>
          <div>
            <Label htmlFor="m-url">
              URL {form.kind === 'vztmpl' ? t('mirrors.url_or_template_hint') : t('mirrors.url_required_hint')}
            </Label>
            <Input
              id="m-url"
              placeholder={form.kind === 'iso' ? 'https://.../installer-amd64.iso' : 'https://.../image.qcow2'}
              value={form.url}
              onChange={(e) => set('url', e.target.value)}
            />
          </div>
          {form.kind === 'vztmpl' && (
            <div>
              <Label htmlFor="m-tpl">{t('mirrors.template_name')}</Label>
              <Input id="m-tpl" placeholder="debian-12-standard_..._amd64.tar.zst" value={form.template} onChange={(e) => set('template', e.target.value)} />
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="m-sum">{t('mirrors.checksum')}</Label>
              <Input id="m-sum" value={form.checksum} onChange={(e) => set('checksum', e.target.value)} />
            </div>
            <div>
              <Label>{t('mirrors.algo')}</Label>
              <Select value={form.checksum_algorithm || '__none__'} onValueChange={(v) => { if (v !== null) set('checksum_algorithm', v === '__none__' ? '' : v); }}>
                <SelectTrigger><SelectValue placeholder="—" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">—</SelectItem>
                  <SelectItem value="sha256">sha256</SelectItem>
                  <SelectItem value="sha512">sha512</SelectItem>
                  <SelectItem value="md5">md5</SelectItem>
                  <SelectItem value="sha1">sha1</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div>
            <Label htmlFor="m-desc">{t('mirrors.description')}</Label>
            <Input id="m-desc" value={form.description} onChange={(e) => set('description', e.target.value)} />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={form.enabled} onChange={(e) => set('enabled', e.target.checked)} />
            <span>{t('mirrors.enabled_label')}</span>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!form.name.trim() || pending}>
            {pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function MirrorsManager() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data: mirrors = [], isLoading } = useImageMirrors();
  const del = useDeleteMirror();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ImageMirrorItem | null>(null);

  const openAdd = () => { setEditing(null); setDialogOpen(true); };
  const openEdit = (m: ImageMirrorItem) => { setEditing(m); setDialogOpen(true); };

  const handleDelete = async (m: ImageMirrorItem) => {
    const ok = await confirm(m.name, {
      title: t('mirrors.delete_title'),
      confirmLabel: t('common.delete'),
      variant: 'destructive',
    });
    if (!ok) return;
    del.mutate(m.mirror_id, {
      onSuccess: () => toast.success(t('mirrors.deleted')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">{t('mirrors.title')}</h2>
        <Button size="sm" onClick={openAdd}>
          <Plus className="mr-2 h-4 w-4" /> {t('mirrors.add')}
        </Button>
      </div>

      {isLoading ? (
        <div className="text-sm text-muted-foreground">{t('common.loading')}</div>
      ) : mirrors.length === 0 ? (
        <div className="text-sm text-muted-foreground">{t('mirrors.empty')}</div>
      ) : (
        <div className="space-y-2">
          {mirrors.map((m) => (
            <Card key={m.id}>
              <CardContent className="flex items-center gap-3 p-3">
                <div className="min-w-0 flex-1">
                  <div className="font-medium truncate">{m.name}</div>
                  <div className="text-xs text-muted-foreground truncate">{m.url || m.template}</div>
                </div>
                <Badge variant="secondary">{m.kind}</Badge>
                <Badge variant="outline">{m.arch}</Badge>
                {!m.enabled && <Badge variant="destructive">{t('mirrors.disabled_badge')}</Badge>}
                <Button variant="ghost" size="icon" onClick={() => openEdit(m)}>
                  <Pencil className="h-4 w-4" />
                </Button>
                <Button variant="ghost" size="icon" onClick={() => handleDelete(m)}>
                  <Trash2 className="h-4 w-4 text-destructive" />
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <MirrorFormDialog open={dialogOpen} onClose={() => setDialogOpen(false)} editing={editing} />
    </section>
  );
}
