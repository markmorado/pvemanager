import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Loader2, Play, CheckCircle2, XCircle, AlertTriangle } from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select';
import { useServers, useNodes } from '@/hooks/use-nodes';
import { useVirtualMachines } from '@/hooks/use-instances';
import { useExecuteScript, useScriptExecution } from '@/hooks/use-scripts';
import type { ScriptCatalogItem } from '@/types/scripts';
import type { ScriptExecution } from '@/types/scripts';

interface Props {
  script: ScriptCatalogItem | null;
  onOpenChange: (open: boolean) => void;
}

export default function ScriptExecuteDialog({ script, onOpenChange }: Props) {
  const { t } = useTranslation();
  const open = !!script;

  const { data: servers = [] } = useServers();
  const { data: vms = [] } = useVirtualMachines();

  const [serverId, setServerId] = useState('');
  const [node, setNode] = useState('');
  const [guestKey, setGuestKey] = useState(''); // `${server_id}:${vmid}:${type}`
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [executionId, setExecutionId] = useState<number | null>(null);

  // Выполнение фоновое: запуск возвращает execution со статусом `running`,
  // дальше поллим его до завершения (см. useScriptExecution).
  const { data: liveExec } = useScriptExecution(executionId ?? 0);
  const result: ScriptExecution | null = liveExec ?? null;

  const serverIdNum = serverId ? Number(serverId) : 0;
  const { data: nodesData } = useNodes(serverIdNum || 0);
  const nodes = nodesData?.nodes ?? [];

  const guestOptions = useMemo(
    () => vms.filter((v) => !v.template && (!serverIdNum || v.server_id === serverIdNum)),
    [vms, serverIdNum],
  );

  // Fresh form (with the script's parameter defaults) for every script opened.
  const [filledFor, setFilledFor] = useState<typeof script>(null);
  if (script !== filledFor) {
    setFilledFor(script);
    if (script) {
      setServerId(''); setNode(''); setGuestKey(''); setExecutionId(null);
      const defaults: Record<string, string> = {};
      for (const p of script.params_schema || []) {
        defaults[p.name] = p.default != null ? String(p.default) : '';
      }
      setParamValues(defaults);
    }
  }

  const execute = useExecuteScript(script?.id || 0);

  const selectedGuest = useMemo(() => {
    if (!guestKey) return null;
    const [sid, vmid] = guestKey.split(':');
    return guestOptions.find((v) => String(v.server_id) === sid && String(v.vmid) === vmid) || null;
  }, [guestKey, guestOptions]);

  const canRun = useMemo(() => {
    if (!script || !serverIdNum) return false;
    if (script.target_type === 'node') return !!node;
    return !!selectedGuest;
  }, [script, serverIdNum, node, selectedGuest]);

  const handleRun = () => {
    if (!script) return;
    for (const p of script.params_schema) {
      if (p.required && !paramValues[p.name]?.trim()) {
        toast.error(t('scripts.param_required', 'Параметр "{{label}}" обязателен', { label: p.label }));
        return;
      }
    }
    execute.mutate(
      {
        server_id: serverIdNum,
        node: script.target_type === 'node' ? node : selectedGuest?.node,
        vmid: script.target_type === 'guest' ? selectedGuest?.vmid : undefined,
        vm_type: script.target_type === 'guest' ? selectedGuest?.type : undefined,
        params: paramValues,
      },
      {
        onSuccess: (r) => setExecutionId(r.id),
        onError: (e) => toast.error((e as Error).message),
      },
    );
  };

  if (!script) return null;

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onOpenChange(false)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('scripts.run_title', 'Запустить: {{name}}', { name: script.name })}</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>{t('scripts.server', 'Сервер Proxmox')}</Label>
            <Select value={serverId} onValueChange={(v) => { setServerId(v ?? ''); setNode(''); setGuestKey(''); }}>
              <SelectTrigger><SelectValue placeholder={t('scripts.pick_server', 'Выберите сервер')} /></SelectTrigger>
              <SelectContent>
                {servers.map((s) => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          {script.target_type === 'node' ? (
            <div className="space-y-1.5">
              <Label>{t('scripts.node', 'Нода')}</Label>
              <Select value={node} onValueChange={(v) => setNode(v ?? '')} disabled={!serverIdNum}>
                <SelectTrigger><SelectValue placeholder={t('scripts.pick_node', 'Выберите ноду')} /></SelectTrigger>
                <SelectContent>
                  {nodes.map((n) => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label>{t('scripts.guest', 'VM / LXC')}</Label>
              <Select value={guestKey} onValueChange={(v) => setGuestKey(v ?? '')} disabled={!serverIdNum}>
                <SelectTrigger><SelectValue placeholder={t('scripts.pick_guest', 'Выберите ВМ/контейнер')} /></SelectTrigger>
                <SelectContent>
                  {guestOptions.map((v) => (
                    <SelectItem key={`${v.server_id}:${v.vmid}`} value={`${v.server_id}:${v.vmid}:${v.type}`}>
                      {v.vmid} — {v.name} ({v.type})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {script.source === 'community-scripts' && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                {t(
                  'scripts.community_scripts_warning',
                  'Скрипт создаст новый LXC-контейнер на хосте Proxmox и во время выполнения сам обратится к GitHub за дополнительным кодом (misc/build.func) — это сторонний код, не проверяемый и не фиксируемый pvemanager.',
                )}
              </p>
            </div>
          )}

          {script.params_schema.length > 0 && (
            <div className="space-y-3 rounded-md border p-3">
              {script.params_schema.map((p) => (
                <div key={p.name} className="space-y-1.5">
                  <Label>{p.label}{p.required && ' *'}</Label>
                  <Input
                    value={paramValues[p.name] ?? ''}
                    onChange={(e) => setParamValues({ ...paramValues, [p.name]: e.target.value })}
                    type={p.type === 'number' ? 'number' : 'text'}
                  />
                </div>
              ))}
            </div>
          )}

          {result && (
            <div className="space-y-1.5 rounded-md border bg-muted/40 p-3">
              <div className="flex items-center gap-2 text-sm">
                {result.status === 'success'
                  ? <CheckCircle2 className="h-4 w-4 text-green-600" />
                  : <XCircle className="h-4 w-4 text-destructive" />}
                <span>{t('scripts.exit_code', 'Exit code')}: {result.exit_code ?? '—'}</span>
              </div>
              {result.error_text && <p className="text-xs text-destructive">{result.error_text}</p>}
              {result.output && (
                <pre className="max-h-64 overflow-auto rounded bg-background p-2 text-xs whitespace-pre-wrap">{result.output}</pre>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.close', 'Закрыть')}</Button>
          <Button onClick={handleRun} disabled={!canRun || execute.isPending}>
            {execute.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
            {t('scripts.run', 'Запустить')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
