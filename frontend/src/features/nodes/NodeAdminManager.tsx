import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw, Server, Play, Square, RotateCw, PackageCheck, Download } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  useNodeServices, useServiceAction,
  useAptUpdates, useAptRefresh, useAptRepositories, useSetAptRepository,
} from '@/hooks/use-node-admin';
import { toast } from 'sonner';
import NodeSyslog from './NodeSyslog';
import NodeTasks from './NodeTasks';
import NodeSystemConfig from './NodeSystemConfig';
import NodeCertificates from './NodeCertificates';

function stateVariant(s?: string): 'default' | 'secondary' | 'destructive' {
  if (!s) return 'secondary';
  if (s === 'running' || s === 'active') return 'default';
  if (s === 'dead' || s === 'failed') return 'destructive';
  return 'secondary';
}

export default function NodeAdminManager({ serverId, nodeNames }: { serverId: number; nodeNames: string[] }) {
  const { t } = useTranslation();
  const [nodePick, setNode] = useState('');
  // The picked node, or the first one while none (or a vanished one) is picked.
  const node = nodeNames.includes(nodePick) ? nodePick : (nodeNames[0] ?? '');

  const { data: servicesData, refetch, isFetching } = useNodeServices(serverId, node);
  const serviceAction = useServiceAction(serverId, node);
  const [tab, setTab] = useState('services');
  const [aptOpen, setAptOpen] = useState(false);
  const [reposOpen, setReposOpen] = useState(false);
  const { data: updatesData } = useAptUpdates(serverId, node, aptOpen);
  const aptRefresh = useAptRefresh(serverId, node);
  const { data: reposData } = useAptRepositories(serverId, node, reposOpen);
  const setRepo = useSetAptRepository(serverId, node);

  const services = servicesData?.services ?? [];
  const updates = updatesData?.updates ?? [];
  const files = reposData?.repositories?.files ?? [];

  const doAction = (service: string, action: string) => {
    serviceAction.mutate({ service, action }, {
      onSuccess: () => toast.success(`${service}: ${action}`),
      onError: (e: Error) => toast.error(e.message),
    });
  };
  const refreshApt = () => {
    aptRefresh.mutate(undefined, {
      onSuccess: () => toast.success(t('nodeadm.apt_refreshed', 'Списки пакетов обновляются')),
      onError: (e: Error) => toast.error(e.message),
    });
  };
  const toggleRepo = (path: string, index: number, enabled: boolean) => {
    setRepo.mutate({ path, index, enabled }, {
      onSuccess: () => toast.success(t('nodeadm.repo_updated', 'Репозиторий обновлён')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-lg">
          <Server className="h-5 w-5" />{t('nodeadm.title', 'Администрирование ноды')}
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
          <Tabs value={tab} onValueChange={(v) => { setTab(v); setAptOpen(v === 'updates'); setReposOpen(v === 'repos'); }}>
            <TabsList className="flex-wrap h-auto">
              <TabsTrigger value="services">{t('nodeadm.services', 'Сервисы')} ({services.length})</TabsTrigger>
              <TabsTrigger value="updates">{t('nodeadm.updates', 'Обновления')}</TabsTrigger>
              <TabsTrigger value="repos">{t('nodeadm.repos', 'Репозитории')}</TabsTrigger>
              <TabsTrigger value="certs">{t('nodeadm.certs', 'Сертификаты')}</TabsTrigger>
              <TabsTrigger value="syslog">{t('nodeadm.syslog', 'Журнал')}</TabsTrigger>
              <TabsTrigger value="tasks">{t('nodeadm.tasks', 'Задачи')}</TabsTrigger>
              <TabsTrigger value="system">{t('nodeadm.system', 'Система')}</TabsTrigger>
            </TabsList>

            {/* Services */}
            <TabsContent value="services" className="mt-4">
              {services.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
              ) : (
                <Table>
                  <TableHeader><TableRow>
                    <TableHead>{t('nodeadm.service', 'Сервис')}</TableHead>
                    <TableHead>{t('nodeadm.desc', 'Описание')}</TableHead>
                    <TableHead>{t('common.status', 'Статус')}</TableHead>
                    <TableHead className="text-right">{t('common.actions', 'Действия')}</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {services.map(s => (
                      <TableRow key={s.service}>
                        <TableCell className="font-mono text-xs font-medium">{s.service}</TableCell>
                        <TableCell className="text-xs text-muted-foreground truncate max-w-[280px]">{s.desc || s.name}</TableCell>
                        <TableCell><Badge variant={stateVariant(s.state)}>{s.state || '—'}</Badge></TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-1">
                            <Button variant="ghost" size="icon" className="h-7 w-7" title="start" onClick={() => doAction(s.service, 'start')}><Play className="h-3.5 w-3.5" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7" title="restart" onClick={() => doAction(s.service, 'restart')}><RotateCw className="h-3.5 w-3.5" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" title="stop" onClick={() => doAction(s.service, 'stop')}><Square className="h-3.5 w-3.5" /></Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </TabsContent>

            {/* Updates */}
            <TabsContent value="updates" className="mt-4 space-y-3">
              <div className="flex items-center justify-between">
                <p className="text-sm text-muted-foreground">
                  {updates.length > 0
                    ? t('nodeadm.updates_count', 'Доступно обновлений: {{n}}', { n: updates.length })
                    : t('nodeadm.up_to_date', 'Обновлений нет')}
                </p>
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="outline" onClick={refreshApt} disabled={aptRefresh.isPending}>
                    <PackageCheck className="mr-1 h-4 w-4" />{t('nodeadm.refresh', 'Проверить (apt update)')}
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => window.open(`/console/node/${serverId}/${node}?cmd=upgrade`, '_blank', 'noopener,noreferrer')}
                    disabled={!node}
                  >
                    <Download className="mr-1 h-4 w-4" />{t('nodeadm.do_upgrade', 'Установить обновления')}
                  </Button>
                </div>
              </div>
              {updates.length > 0 && (
                <Table>
                  <TableHeader><TableRow>
                    <TableHead>{t('nodeadm.package', 'Пакет')}</TableHead>
                    <TableHead>{t('nodeadm.current', 'Текущая')}</TableHead>
                    <TableHead>{t('nodeadm.new', 'Новая')}</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {updates.map(u => (
                      <TableRow key={u.Package}>
                        <TableCell className="font-mono text-xs font-medium">{u.Package}</TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">{u.OldVersion || '—'}</TableCell>
                        <TableCell className="font-mono text-xs text-success">{u.Version || '—'}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
              <p className="text-xs text-muted-foreground">{t('nodeadm.upgrade_hint', '«Установить обновления» откроет консоль ноды с apt dist-upgrade (потребуется подтверждение). Не закрывайте вкладку до завершения.')}</p>
            </TabsContent>

            {/* Repositories */}
            <TabsContent value="repos" className="mt-4 space-y-4">
              {files.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
              ) : files.map(f => (
                <div key={f.path} className="space-y-2">
                  <p className="font-mono text-xs text-muted-foreground">{f.path}</p>
                  <div className="space-y-1">
                    {(f.repositories ?? []).map((r, idx) => {
                      const enabled = r.Enabled === 1 || r.Enabled === true;
                      return (
                        <div key={idx} className="flex items-center gap-2 rounded border px-2 py-1.5 text-xs">
                          <Badge variant={enabled ? 'default' : 'secondary'} className="text-2xs">{enabled ? 'on' : 'off'}</Badge>
                          <span className="font-mono truncate">{(r.URIs || []).join(' ')} {(r.Suites || []).join(' ')} {(r.Components || []).join(' ')}</span>
                          <Button size="sm" variant="outline" className="ml-auto h-6 text-2xs"
                            onClick={() => toggleRepo(f.path, idx, !enabled)} disabled={setRepo.isPending}>
                            {enabled ? t('nodeadm.disable', 'Выключить') : t('nodeadm.enable', 'Включить')}
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </TabsContent>

            {/* TLS certificates */}
            <TabsContent value="certs" className="mt-4">
              {tab === 'certs' && <NodeCertificates serverId={serverId} node={node} />}
            </TabsContent>

            {/* Syslog */}
            <TabsContent value="syslog" className="mt-4">
              {tab === 'syslog' && <NodeSyslog serverId={serverId} node={node} />}
            </TabsContent>

            {/* Task history */}
            <TabsContent value="tasks" className="mt-4">
              {tab === 'tasks' && <NodeTasks serverId={serverId} node={node} />}
            </TabsContent>

            {/* System config: DNS / hosts / time */}
            <TabsContent value="system" className="mt-4">
              {tab === 'system' && <NodeSystemConfig serverId={serverId} node={node} />}
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  );
}
