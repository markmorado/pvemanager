import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowRightLeft } from 'lucide-react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useNodes } from '@/hooks/use-nodes';

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  serverId: number;
  /** Ноды, на которых сейчас находятся выбранные инстансы (для исключения). */
  sourceNodes: string[];
  count: number;
  onConfirm: (targetNode: string, online: boolean) => void;
  pending?: boolean;
}

/**
 * Диалог массовой миграции: выбор целевой ноды кластера и режима online.
 * Целевая нода не может совпадать с единственной исходной. Инстансы, уже
 * находящиеся на выбранной ноде, родитель отфильтровывает при отправке.
 */
export default function BulkMigrateDialog({
  open, onOpenChange, serverId, sourceNodes, count, onConfirm, pending,
}: Props) {
  const { t } = useTranslation();
  const { data: nodesData } = useNodes(serverId);
  const [targetPick, setTarget] = useState('');
  const [online, setOnline] = useState(true);

  const nodeNames = useMemo(() => (nodesData?.nodes ?? []).map((n) => n.node), [nodesData]);
  const singleSource = sourceNodes.length === 1 ? sourceNodes[0] : null;

  // Выбранная нода, а пока выбора нет (или он стал недопустим) — первая подходящая.
  const candidates = nodeNames.filter((n) => n !== singleSource);
  const target = candidates.includes(targetPick) ? targetPick : (candidates[0] ?? '');

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ArrowRightLeft className="h-5 w-5" />
            {t('instances.bulk_migrate_title', 'Массовая миграция ({{count}})', { count })}
          </DialogTitle>
          <DialogDescription>
            {t('instances.bulk_migrate_desc', 'Выберите целевую ноду кластера. Инстансы, уже находящиеся на ней, будут пропущены.')}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium">{t('instances.target_node', 'Целевая нода')}</label>
            <Select value={target} onValueChange={(v) => { if (v) setTarget(v); }}>
              <SelectTrigger><SelectValue placeholder={t('instances.select_node', 'Выберите ноду')} /></SelectTrigger>
              <SelectContent>
                {nodeNames.filter((n) => n !== singleSource).map((n) => (
                  <SelectItem key={n} value={n}>{n}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={online}
              onChange={(e) => setOnline(e.target.checked)}
            />
            {t('instances.online_migration', 'Онлайн-миграция запущенных (live)')}
          </label>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel', 'Отмена')}
          </Button>
          <Button
            onClick={() => onConfirm(target, online)}
            disabled={!target || pending}
          >
            <ArrowRightLeft className="mr-1 h-4 w-4" />
            {t('instances.migrate', 'Мигрировать')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
