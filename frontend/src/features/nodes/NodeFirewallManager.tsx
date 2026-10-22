import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, RefreshCw, ShieldCheck, Power } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  useFirewallGroups,
  useNodeFirewallOptions, useUpdateNodeFirewallOptions,
  useNodeFirewallRules, useCreateNodeFirewallRule, useUpdateNodeFirewallRule, useDeleteNodeFirewallRule,
  useNodeFirewallLog,
  type FirewallRule,
} from '@/hooks/use-firewall';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { RuleDialog, RuleRow } from './FirewallManager';

const LOG_LEVELS = ['nolog', 'emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug'];

export default function NodeFirewallManager({ serverId, nodeNames }: { serverId: number; nodeNames: string[] }) {
  const { t } = useTranslation();
  const confirm = useConfirm();

  const [nodePick, setNode] = useState('');
  // The picked node, or the first one while none (or a vanished one) is picked.
  const node = nodeNames.includes(nodePick) ? nodePick : (nodeNames[0] ?? '');

  const { data: groupsData } = useFirewallGroups(serverId);
  const { data: optionsData, isLoading, refetch, isFetching } = useNodeFirewallOptions(serverId, node);
  const { data: rulesData } = useNodeFirewallRules(serverId, node);
  const { data: logData } = useNodeFirewallLog(serverId, node, false); // грузим лениво по вкладке

  const updateOptions = useUpdateNodeFirewallOptions(serverId, node);
  const createRule = useCreateNodeFirewallRule(serverId, node);
  const updateRule = useUpdateNodeFirewallRule(serverId, node);
  const deleteRule = useDeleteNodeFirewallRule(serverId, node);

  const groups = groupsData?.groups ?? [];
  const options = optionsData?.options ?? {};
  const rules = rulesData?.rules ?? [];
  const enabled = Number(options.enable) === 1;

  const [ruleDialog, setRuleDialog] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const logQuery = useNodeFirewallLog(serverId, node, logOpen);
  const log = (logOpen ? logQuery.data : logData)?.log ?? [];

  const saveOption = (patch: Record<string, unknown>) => {
    updateOptions.mutate(patch, {
      onSuccess: () => toast.success(t('fw.options_saved', 'Опции сохранены')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const addRule = (payload: Record<string, unknown>) => {
    createRule.mutate(payload, {
      onSuccess: () => { toast.success(t('fw.rule_added', 'Правило добавлено')); setRuleDialog(false); },
      onError: (e: Error) => toast.error(e.message),
    });
  };
  const toggleRule = (r: FirewallRule) => {
    if (r.pos == null) return;
    updateRule.mutate({ pos: r.pos, data: { enable: r.enable === 0 ? 1 : 0 } }, {
      onError: (e: Error) => toast.error(e.message),
    });
  };
  const removeRule = async (pos?: number) => {
    if (pos == null || !await confirm(t('fw.delete_rule_confirm', 'Удалить правило?'))) return;
    deleteRule.mutate(pos, { onSuccess: () => toast.success(t('fw.rule_deleted', 'Правило удалено')), onError: (e: Error) => toast.error(e.message) });
  };

  const boolOption = (key: string, label: string) => (
    <label className="flex items-center gap-2 text-sm">
      <Checkbox
        checked={Number(options[key]) === 1}
        onChange={(e) => saveOption({ [key]: e.target.checked ? 1 : 0 })}
      />
      {label}
    </label>
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-lg">
          <ShieldCheck className="h-5 w-5" />
          {t('fw.node_title', 'Firewall (нода)')}
          <Badge variant={enabled ? 'default' : 'secondary'}>{enabled ? t('fw.on', 'вкл') : t('fw.off', 'выкл')}</Badge>
        </CardTitle>
        <div className="flex items-center gap-2">
          {nodeNames.length > 1 && (
            <Select value={node} onValueChange={(v) => { if (v) setNode(v); }}>
              <SelectTrigger className="w-[140px] h-8"><SelectValue /></SelectTrigger>
              <SelectContent>
                {nodeNames.map(n => <SelectItem key={n} value={n}>{n}</SelectItem>)}
              </SelectContent>
            </Select>
          )}
          <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </Button>
          <Button size="sm" variant={enabled ? 'outline' : 'default'} onClick={() => saveOption({ enable: enabled ? 0 : 1 })} disabled={updateOptions.isPending || !node}>
            <Power className="mr-1 h-4 w-4" />{enabled ? t('fw.disable', 'Выключить') : t('fw.enable', 'Включить')}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {!node ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
        ) : isLoading ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
        ) : (
          <Tabs defaultValue="rules" onValueChange={(v) => setLogOpen(v === 'log')}>
            <TabsList>
              <TabsTrigger value="rules">{t('fw.rules', 'Правила')} ({rules.length})</TabsTrigger>
              <TabsTrigger value="options">{t('fw.options', 'Опции')}</TabsTrigger>
              <TabsTrigger value="log">{t('fw.log_tab', 'Лог')}</TabsTrigger>
            </TabsList>

            {/* Rules */}
            <TabsContent value="rules" className="mt-4 space-y-3">
              <div className="flex justify-end">
                <Button size="sm" onClick={() => setRuleDialog(true)}>
                  <Plus className="mr-1 h-4 w-4" />{t('fw.add_rule', 'Добавить правило')}
                </Button>
              </div>
              {rules.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>{t('fw.direction', 'Напр.')}</TableHead>
                      <TableHead>{t('fw.action', 'Действие')}</TableHead>
                      <TableHead>{t('fw.proto', 'Прото')}</TableHead>
                      <TableHead>{t('fw.match', 'Источник → Назначение')}</TableHead>
                      <TableHead>{t('common.comment', 'Комментарий')}</TableHead>
                      <TableHead className="text-right">{t('common.actions', 'Действия')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rules.map(r => (
                      <RuleRow key={r.pos} rule={r} onToggle={() => toggleRule(r)} onDelete={() => removeRule(r.pos)} />
                    ))}
                  </TableBody>
                </Table>
              )}
            </TabsContent>

            {/* Options */}
            <TabsContent value="options" className="mt-4 space-y-4 max-w-md">
              <label className="flex items-center gap-2.5 cursor-pointer">
                <Checkbox checked={enabled} onChange={(e) => saveOption({ enable: e.target.checked ? 1 : 0 })} />
                <span className="text-sm">{t('fw.enable_node', 'Включить фаервол ноды')}</span>
              </label>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('fw.log_level_in', 'Лог входящих')}</Label>
                  <Select value={String(options.log_level_in || 'nolog')} onValueChange={(v) => { if (v) saveOption({ log_level_in: v }); }}>
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>{LOG_LEVELS.map(l => <SelectItem key={l} value={l}>{l}</SelectItem>)}</SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('fw.log_level_out', 'Лог исходящих')}</Label>
                  <Select value={String(options.log_level_out || 'nolog')} onValueChange={(v) => { if (v) saveOption({ log_level_out: v }); }}>
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>{LOG_LEVELS.map(l => <SelectItem key={l} value={l}>{l}</SelectItem>)}</SelectContent>
                  </Select>
                </div>
              </div>
              <div className="space-y-2">
                {boolOption('nosmurfs', t('fw.nosmurfs', 'Блокировать SMURF-атаки'))}
                {boolOption('tcpflags', t('fw.tcpflags', 'Фильтровать недопустимые TCP-флаги'))}
                {boolOption('protection_synflood', t('fw.synflood', 'Защита от SYN-flood'))}
              </div>
              <p className="text-xs text-muted-foreground">{t('fw.node_options_hint', 'Опции применяются к трафику самой ноды хоста Proxmox.')}</p>
            </TabsContent>

            {/* Log */}
            <TabsContent value="log" className="mt-4">
              {logQuery.isLoading ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
              ) : log.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
              ) : (
                <pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 text-xs font-mono whitespace-pre-wrap">
                  {log.map(l => l.t).filter(Boolean).join('\n')}
                </pre>
              )}
            </TabsContent>
          </Tabs>
        )}
      </CardContent>

      <RuleDialog open={ruleDialog} onOpenChange={setRuleDialog} onSubmit={addRule} pending={createRule.isPending} groups={groups} allowGroupType />
    </Card>
  );
}
