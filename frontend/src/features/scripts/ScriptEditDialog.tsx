import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Loader2, Plus, Trash2 } from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select';
import { useCreateScript, useUpdateScript } from '@/hooks/use-scripts';
import type { ScriptCatalogItem, ScriptParamField } from '@/types/scripts';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  script: ScriptCatalogItem | null;
}

const emptyParam = (): ScriptParamField => ({ name: '', label: '', type: 'string', required: false, default: '' });

export default function ScriptEditDialog({ open, onOpenChange, script }: Props) {
  const { t } = useTranslation();
  const isEdit = !!script;

  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [description, setDescription] = useState('');
  const [category, setCategory] = useState('');
  const [targetType, setTargetType] = useState<'node' | 'guest'>('guest');
  const [interpreter, setInterpreter] = useState('/bin/bash');
  const [content, setContent] = useState('#!/bin/bash\n');
  const [params, setParams] = useState<ScriptParamField[]>([]);

  // Refill the form each time the dialog opens (or is pointed at another script).
  const [filledFor, setFilledFor] = useState<{ open: boolean; script: typeof script }>({ open: false, script: null });
  if (filledFor.open !== open || filledFor.script !== script) {
    setFilledFor({ open, script });
    if (open && script) {
      setName(script.name);
      setSlug(script.slug);
      setDescription(script.description || '');
      setCategory(script.category || '');
      setTargetType(script.target_type);
      setInterpreter(script.interpreter);
      setContent(script.content || '');
      setParams(script.params_schema || []);
    } else if (open) {
      setName(''); setSlug(''); setDescription(''); setCategory('');
      setTargetType('guest'); setInterpreter('/bin/bash'); setContent('#!/bin/bash\n'); setParams([]);
    }
  }

  const create = useCreateScript();
  const update = useUpdateScript(script?.id || 0);
  const pending = create.isPending || update.isPending;

  const handleNameChange = (v: string) => {
    setName(v);
    if (!isEdit) {
      setSlug(v.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, ''));
    }
  };

  const handleSave = () => {
    if (!name.trim() || !slug.trim() || !content.trim()) {
      toast.error(t('scripts.validation_required', 'Заполните имя, slug и содержимое скрипта'));
      return;
    }
    const payload = {
      name, slug, description: description || undefined, category: category || undefined,
      target_type: targetType, interpreter, content,
      params_schema: params.filter((p) => p.name.trim()),
    };
    const onDone = {
      onSuccess: () => { toast.success(t('scripts.saved', 'Скрипт сохранён')); onOpenChange(false); },
      onError: (e: unknown) => toast.error((e as Error).message),
    };
    if (isEdit) update.mutate(payload, onDone);
    else create.mutate(payload, onDone);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? t('scripts.edit_title', 'Редактировать скрипт') : t('scripts.new_title', 'Новый скрипт')}</DialogTitle>
        </DialogHeader>

        <div className="max-h-[65vh] space-y-4 overflow-y-auto pr-1">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('scripts.name', 'Имя')}</Label>
              <Input value={name} onChange={(e) => handleNameChange(e.target.value)} placeholder={t('scripts.name_placeholder')} />
            </div>
            <div className="space-y-1.5">
              <Label>{t('scripts.slug', 'Slug')}</Label>
              <Input value={slug} onChange={(e) => setSlug(e.target.value)} disabled={isEdit} placeholder="clean-logs" />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>{t('scripts.description', 'Описание')}</Label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1.5">
              <Label>{t('scripts.category', 'Категория')}</Label>
              <Input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="maintenance" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('scripts.target_type', 'Цель выполнения')}</Label>
              <Select value={targetType} onValueChange={(v) => setTargetType((v as 'node' | 'guest') ?? 'guest')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="guest">{t('scripts.target_guest', 'VM/LXC')}</SelectItem>
                  <SelectItem value="node">{t('scripts.target_node', 'Нода Proxmox')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>{t('scripts.interpreter', 'Интерпретатор')}</Label>
              <Input value={interpreter} onChange={(e) => setInterpreter(e.target.value)} placeholder="/bin/bash" />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>{t('scripts.content', 'Содержимое скрипта')}</Label>
            <textarea
              className="min-h-[220px] w-full rounded-lg border border-input bg-transparent p-2.5 font-mono text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              spellCheck={false}
            />
            <p className="text-2xs text-muted-foreground">
              {t('scripts.params_hint', 'Параметры доступны как переменные окружения ($NAME).')}
            </p>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>{t('scripts.params', 'Параметры')}</Label>
              <Button size="sm" variant="outline" onClick={() => setParams([...params, emptyParam()])}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />{t('scripts.add_param', 'Добавить параметр')}
              </Button>
            </div>
            {params.map((p, i) => (
              <div key={i} className="grid grid-cols-[1fr_1fr_100px_80px_auto] items-center gap-2">
                <Input
                  placeholder="NAME" value={p.name}
                  onChange={(e) => setParams(params.map((x, j) => j === i ? { ...x, name: e.target.value.toUpperCase() } : x))}
                />
                <Input
                  placeholder={t('scripts.param_label', 'Подпись')} value={p.label}
                  onChange={(e) => setParams(params.map((x, j) => j === i ? { ...x, label: e.target.value } : x))}
                />
                <Select value={p.type} onValueChange={(v) => setParams(params.map((x, j) => j === i ? { ...x, type: (v as ScriptParamField['type']) ?? 'string' } : x))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="string">string</SelectItem>
                    <SelectItem value="number">number</SelectItem>
                    <SelectItem value="bool">bool</SelectItem>
                  </SelectContent>
                </Select>
                <label className="flex items-center justify-center gap-1 text-xs">
                  <Checkbox
                    checked={p.required}
                    onChange={(e) => setParams(params.map((x, j) => j === i ? { ...x, required: e.target.checked } : x))}
                  />
                  {t('scripts.required', 'обяз.')}
                </label>
                <Button size="sm" variant="outline" onClick={() => setParams(params.filter((_, j) => j !== i))}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={handleSave} disabled={pending}>
            {pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('common.save', 'Сохранить')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
