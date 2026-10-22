import { useParams, useSearchParams, Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Cpu, MemoryStick, HardDrive, Clock, MoreVertical, TerminalSquare, RotateCw, Power } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useServer, useNodes, useClusterInfo } from '@/hooks/use-nodes';
import { useVirtualMachines } from '@/hooks/use-instances';
import { useNodePower } from '@/hooks/use-node-admin';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { ServerStatusBadge } from '@/components/shared/ServerStatusBadge';
import { useHasPermission } from '@/lib/permissions';
import { StatusDot } from '@/components/shared/status-dot';
import { formatBytes, formatUptime, formatPercent } from '@/lib/format';

/** Меню действий питания/консоли для одной ноды (Shell / Reboot / Shutdown). */
function NodeActionsMenu({ serverId, node }: { serverId: number; node: string }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const power = useNodePower(serverId, node);
  // hasPermission, not a raw map lookup: an admin bound to a role gets only
  // that role's map from /me, yet the backend lets admins through.
  const canPower = useHasPermission('node:power');

  const doPower = async (command: 'reboot' | 'shutdown') => {
    const ok = await confirm(
      command === 'reboot'
        ? t('nodes.confirm_reboot', 'Перезагрузить ноду «{{node}}»? Все ВМ и контейнеры на ней будут остановлены.', { node })
        : t('nodes.confirm_shutdown', 'Выключить ноду «{{node}}»? Доступ к ней будет потерян до ручного включения.', { node }),
      { title: command === 'reboot' ? t('nodes.reboot', 'Перезагрузка') : t('nodes.shutdown', 'Выключение'), variant: 'destructive' },
    );
    if (!ok) return;
    power.mutate(command, {
      onSuccess: () => toast.success(
        command === 'reboot'
          ? t('nodes.reboot_started', 'Перезагрузка ноды запущена')
          : t('nodes.shutdown_started', 'Выключение ноды запущено')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="icon" className="h-7 w-7" />}>
        <MoreVertical className="h-4 w-4" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        <DropdownMenuItem
          render={<Link to={`/console/node/${serverId}/${node}?cmd=login`} target="_blank" rel="noopener noreferrer" />}>
          <TerminalSquare className="mr-2 h-4 w-4" />{t('nodes.shell', 'Консоль (Shell)')}
        </DropdownMenuItem>
        {canPower && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => doPower('reboot')}>
              <RotateCw className="mr-2 h-4 w-4" />{t('nodes.reboot', 'Перезагрузка')}
            </DropdownMenuItem>
            <DropdownMenuItem className="text-destructive" onClick={() => doPower('shutdown')}>
              <Power className="mr-2 h-4 w-4" />{t('nodes.shutdown', 'Выключение')}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
import NodeGraphs from './NodeGraphs';
import NodeNetworks from './NodeNetworks';
import HAResources from './HAResources';
import SDNManager from './SDNManager';
import FirewallManager from './FirewallManager';
import NodeFirewallManager from './NodeFirewallManager';
import PoolManager from './PoolManager';
import DiskManager from './DiskManager';
import AccessManager from './AccessManager';
import NodeAdminManager from './NodeAdminManager';

export default function NodeDetailPage() {
  const { t } = useTranslation();
  const { serverId } = useParams<{ serverId: string }>();
  const sid = Number(serverId);
  const { data: server } = useServer(sid);
  const { data: nodesData } = useNodes(sid);
  const { data: clusterInfo } = useClusterInfo(sid);
  const { data: allVMs = [], isLoading: vmsLoading } = useVirtualMachines();
  const [searchParams, setSearchParams] = useSearchParams();

  const nodes = nodesData?.nodes || [];
  const serverVMs = allVMs.filter(v => v.server_id === sid);

  const activeTab = searchParams.get('tab') || 'overview';
  const setTab = (tab: string) => {
    const params = new URLSearchParams(searchParams);
    params.set('tab', tab);
    setSearchParams(params, { replace: true });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Button render={<Link to="/nodes" />} variant="ghost" size="sm">
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold">{server?.name || `Server #${sid}`}</h1>
          <p className="text-sm text-muted-foreground font-mono">{server?.ip_address}:{server?.port}</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {clusterInfo && (
            <Badge variant={clusterInfo.is_cluster ? 'default' : 'secondary'}>
              {clusterInfo.is_cluster
                ? `${t('nodes.cluster_mode')} · ${clusterInfo.node_count ?? clusterInfo.nodes?.length ?? 0}`
                : t('nodes.standalone')}
            </Badge>
          )}
          <ServerStatusBadge
            isOnline={server?.is_online}
            errorKind={server?.last_error_kind}
            lastError={server?.last_error}
          />
        </div>
      </div>

      <Tabs value={activeTab} onValueChange={setTab}>
        <TabsList className="flex-wrap h-auto">
          <TabsTrigger value="overview">{t('nodes.tab_overview', 'Обзор')}</TabsTrigger>
          {nodes.length > 0 && (
            <>
              <TabsTrigger value="graphs">{t('nodes.tab_graphs', 'Графики')}</TabsTrigger>
              <TabsTrigger value="network">{t('nodes.tab_network', 'Сеть')}</TabsTrigger>
              <TabsTrigger value="ha">{t('nodes.tab_ha', 'HA')}</TabsTrigger>
              <TabsTrigger value="firewall">{t('nodes.tab_firewall', 'Firewall')}</TabsTrigger>
              <TabsTrigger value="pools">{t('nodes.tab_pools', 'Пулы')}</TabsTrigger>
              <TabsTrigger value="disks">{t('nodes.tab_disks', 'Диски')}</TabsTrigger>
              <TabsTrigger value="admin">{t('nodes.tab_admin', 'Администрирование')}</TabsTrigger>
              <TabsTrigger value="access">{t('nodes.tab_access', 'Доступ')}</TabsTrigger>
            </>
          )}
        </TabsList>

        {/* Overview: cluster nodes + instances on server */}
        <TabsContent value="overview" className="mt-4 space-y-6">
          {nodes.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-lg font-semibold">{t('nodes.cluster_nodes')}</h2>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {nodes.map(node => (
                  <Card key={node.node}>
                    <CardContent className="p-4 space-y-3">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{node.node}</span>
                          <StatusDot status={node.status} />
                        </div>
                        <NodeActionsMenu serverId={sid} node={node.node} />
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground">
                        <div className="flex items-center gap-1"><Cpu className="h-3 w-3" />{formatPercent(node.cpu || 0)} CPU</div>
                        <div className="flex items-center gap-1"><MemoryStick className="h-3 w-3" />{formatBytes(node.mem || 0)} / {formatBytes(node.maxmem || 0)}</div>
                        <div className="flex items-center gap-1"><HardDrive className="h-3 w-3" />{formatBytes(node.disk || 0)} / {formatBytes(node.maxdisk || 0)}</div>
                        <div className="flex items-center gap-1"><Clock className="h-3 w-3" />{formatUptime(node.uptime || 0)}</div>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </div>
          )}

          <div className="space-y-3">
            <h2 className="text-lg font-semibold">
              {t('nodes.instances_on_server', 'Инстансы на сервере')}{vmsLoading ? '' : ` (${serverVMs.length})`}
            </h2>
            <div className="rounded-lg border divide-y">
              {vmsLoading && (
                <div className="space-y-3 p-4" aria-busy="true" aria-label={t('common.loading', 'Загрузка…')}>
                  {Array.from({ length: 4 }).map((_, i) => (
                    <div key={i} className="flex items-center gap-3">
                      <Skeleton className="h-2 w-2 rounded-full" />
                      <Skeleton className="h-4 flex-1 max-w-[220px]" />
                      <Skeleton className="h-4 w-10" />
                      <Skeleton className="h-4 w-12" />
                      <Skeleton className="h-4 w-16" />
                    </div>
                  ))}
                </div>
              )}
              {!vmsLoading && serverVMs.map(vm => (
                <Link
                  key={`${vm.server_id}-${vm.vmid}`}
                  to={`/instances/${vm.server_id}/${vm.vmid}?node=${vm.node}&type=${vm.type}`}
                  className="flex items-center gap-3 px-4 py-3 hover:bg-muted/50 transition-colors"
                >
                  <StatusDot status={vm.status} />
                  <span className="font-medium min-w-0 truncate flex-1">{vm.name || `VM ${vm.vmid}`}</span>
                  <Badge variant="outline" className="text-xs">{vm.type === 'qemu' ? 'VM' : 'LXC'}</Badge>
                  <span className="text-xs text-muted-foreground">#{vm.vmid}</span>
                  <span className="text-xs text-muted-foreground">{vm.node}</span>
                </Link>
              ))}
              {!vmsLoading && serverVMs.length === 0 && (
                <p className="px-4 py-6 text-center text-sm text-muted-foreground">{t('common.no_data')}</p>
              )}
            </div>
          </div>
        </TabsContent>

        {nodes.length > 0 && (
          <>
            {/* Graphs: node RRD metrics */}
            <TabsContent value="graphs" className="mt-4">
              <NodeGraphs serverId={sid} nodeNames={nodes.map(n => n.node)} />
            </TabsContent>

            {/* Network: interfaces + SDN */}
            <TabsContent value="network" className="mt-4 space-y-6">
              <NodeNetworks serverId={sid} nodeNames={nodes.map(n => n.node)} />
              <SDNManager serverId={sid} />
            </TabsContent>

            {/* High Availability */}
            <TabsContent value="ha" className="mt-4">
              <HAResources serverId={sid} vms={serverVMs} />
            </TabsContent>

            {/* Firewall: datacenter + node */}
            <TabsContent value="firewall" className="mt-4 space-y-6">
              <FirewallManager serverId={sid} />
              <NodeFirewallManager serverId={sid} nodeNames={nodes.map(n => n.node)} />
            </TabsContent>

            {/* Resource Pools */}
            <TabsContent value="pools" className="mt-4">
              <PoolManager
                serverId={sid}
                vms={serverVMs.map(v => ({ vmid: v.vmid, name: v.name, type: v.type, node: v.node }))}
              />
            </TabsContent>

            {/* Disks & ZFS */}
            <TabsContent value="disks" className="mt-4">
              <DiskManager serverId={sid} nodeNames={nodes.map(n => n.node)} />
            </TabsContent>

            {/* Node administration: services & APT */}
            <TabsContent value="admin" className="mt-4">
              <NodeAdminManager serverId={sid} nodeNames={nodes.map(n => n.node)} />
            </TabsContent>

            {/* PVE Access: realms & API tokens */}
            <TabsContent value="access" className="mt-4">
              <AccessManager serverId={sid} />
            </TabsContent>
          </>
        )}
      </Tabs>
    </div>
  );
}
