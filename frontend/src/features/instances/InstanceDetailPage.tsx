import { useState } from 'react';
import { useParams, useSearchParams, Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  Play,
  Square,
  RotateCcw,
  Terminal,
  Monitor,
  Container,
  Loader2,
  Power,
  MoreHorizontal,
  Copy,
  Sparkles,
  KeyRound,
  FileText,
  Disc,
  ArrowRightLeft,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { StatusDot } from '@/components/shared/status-dot';
import { useVirtualMachines, usePowerAction, useVMInterfaces } from '@/hooks/use-instances';
import { useServers } from '@/hooks/use-nodes';
import InstanceActionDialogs, { PowerConfirmDialog, type InstanceAction, type PowerAction } from './InstanceActionDialogs';
import { vmTypeLabel } from '@/lib/format';
import { toast } from 'sonner';

import OverviewTab from './tabs/OverviewTab';
import GraphsTab from './tabs/GraphsTab';
import SnapshotsTab from './tabs/SnapshotsTab';
import NetworkingTab from './tabs/NetworkingTab';
import HardwareTab from './tabs/HardwareTab';
import FirewallTab from './tabs/FirewallTab';
import SettingsTab from './tabs/SettingsTab';
import DestroyTab from './tabs/DestroyTab';

export default function InstanceDetailPage() {
  const { serverId, vmid } = useParams<{ serverId: string; vmid: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const { t } = useTranslation();

  const node = searchParams.get('node') || '';
  const type = searchParams.get('type') || 'qemu';
  const activeTab = searchParams.get('tab') || 'overview';

  const sid = Number(serverId);
  const vid = Number(vmid);

  // Find VM from cached list for basic info
  const { data: allVMs, isLoading } = useVirtualMachines();
  const vm = allVMs?.find((v) => v.server_id === sid && v.vmid === vid);

  // Show the panel-side server name (from the add-server form) instead of the
  // Proxmox node hostname, matching the instances list. `node` is still used for
  // all API calls below — only the displayed label changes.
  const { data: servers = [] } = useServers();
  const nodeLabel = servers.find((s) => s.id === sid)?.name || node;

  const power = usePowerAction(sid, vid, type, node);
  const isRunning = vm?.status === 'running';

  const { data: ifaces } = useVMInterfaces(sid, vid, type, node, isRunning);
  const primaryLiveIP = ifaces?.interfaces
    ?.flatMap((i) => i.ips || [])
    .find((ip) => ip.type === 'ipv4' && !ip.address.startsWith('127.'))
    ?.address;
  const displayIP = primaryLiveIP || vm?.ip_address;
  const isQemu = type === 'qemu';
  const TypeIcon = type === 'qemu' ? Monitor : Container;
  const [dialog, setDialog] = useState<InstanceAction>(null);
  const [pendingPower, setPendingPower] = useState<PowerAction | null>(null);

  const handlePower = (action: string) => {
    setPendingPower(action as PowerAction);
  };

  const confirmPower = () => {
    if (!pendingPower) return;
    power.mutate(
      { action: pendingPower },
      {
        onSuccess: () => {
          toast.success(t('console.action_sent'));
          setPendingPower(null);
        },
        onError: (err) => {
          toast.error(err.message);
          setPendingPower(null);
        },
      }
    );
  };

  const setTab = (tab: string) => {
    const params = new URLSearchParams(searchParams);
    params.set('tab', tab);
    setSearchParams(params, { replace: true });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back link + header */}
      <div>
        <Link to="/instances" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors mb-3">
          <ArrowLeft className="h-4 w-4" />
          {t('nav.instances', 'Instances')}
        </Link>

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted">
              <TypeIcon className="h-5 w-5 text-muted-foreground" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-[22px] font-semibold">{vm?.name || `${vmTypeLabel(type)} ${vmid}`}</h1>
                <Badge variant="outline" className="text-xs">#{vmid}</Badge>
                <Badge variant="secondary" className="text-xs">{vmTypeLabel(type)}</Badge>
              </div>
              <div className="flex items-center gap-3 text-sm text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <StatusDot status={vm?.status || 'unknown'} pulse />
                  {vm?.status || 'unknown'}
                </span>
                <span>{t('common.node', 'Node')}: {nodeLabel}</span>
                {displayIP && <span className="font-mono">{displayIP}</span>}
              </div>
            </div>
          </div>

          {/* Power buttons */}
          <div className="flex items-center gap-2">
            <Button
              render={<Link to={`/console/${serverId}/${vmid}?node=${node}&type=${type}`} target="_blank" />}
              variant="outline"
              size="sm"
            >
              <Terminal className="mr-1.5 h-4 w-4" />
              {t('common.console', 'Console')}
            </Button>

            {!isRunning ? (
              <Button
                size="sm"
                onClick={() => handlePower('start')}
                disabled={power.isPending}
              >
                <Play className="mr-1.5 h-4 w-4" />
                {t('common.start', 'Start')}
              </Button>
            ) : (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handlePower('restart')}
                  disabled={power.isPending}
                >
                  <RotateCcw className="mr-1.5 h-4 w-4" />
                  {t('common.restart', 'Restart')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handlePower('shutdown')}
                  disabled={power.isPending}
                >
                  <Power className="mr-1.5 h-4 w-4" />
                  {t('common.shutdown', 'Shutdown')}
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => handlePower('stop')}
                  disabled={power.isPending}
                >
                  <Square className="mr-1.5 h-4 w-4" />
                  {t('common.stop', 'Stop')}
                </Button>
              </>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger render={<Button variant="outline" size="sm" />}>
                <MoreHorizontal className="h-4 w-4" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">

                <DropdownMenuItem onClick={() => setDialog('clone')}>
                  <Copy className="mr-2 h-4 w-4" /> {t('common.clone', 'Клонировать')}
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setDialog('migrate')}>
                  <ArrowRightLeft className="mr-2 h-4 w-4" /> {t('instances.migrate', 'Миграция')}
                </DropdownMenuItem>
                {isQemu && (
                  <DropdownMenuItem render={<Link to={`/console/${serverId}/${vmid}?node=${node}&type=${type}&mode=serial`} target="_blank" />}>
                    <Terminal className="mr-2 h-4 w-4" /> {t('instances.serial_console', 'Serial-консоль')}
                  </DropdownMenuItem>
                )}
                {isQemu && (
                  <DropdownMenuItem onClick={() => setDialog('iso')}>
                    <Disc className="mr-2 h-4 w-4" /> {t('common.iso', 'ISO образ')}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onClick={() => setDialog('change-password')}>
                  <KeyRound className="mr-2 h-4 w-4" /> {t('common.changePassword', 'Изменить пароль')}
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setDialog('notes')}>
                  <FileText className="mr-2 h-4 w-4" /> {t('common.notes', 'Примечание')}
                </DropdownMenuItem>
                {isQemu && (
                  <DropdownMenuItem onClick={() => setDialog('execute')}>
                    <Terminal className="mr-2 h-4 w-4" /> {t('common.runCommand', 'Запуск команд')}
                  </DropdownMenuItem>
                )}
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => setDialog('reinstall')}>
                  <Sparkles className="mr-2 h-4 w-4" /> {t('common.reinstall', 'Переустановить')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">{t('instances.overview', 'Overview')}</TabsTrigger>
          <TabsTrigger value="graphs">{t('instances.graphs', 'Graphs')}</TabsTrigger>
          <TabsTrigger value="snapshots">{t('common.snapshots', 'Snapshots')}</TabsTrigger>
          <TabsTrigger value="networking">{t('instances.networking', 'Networking')}</TabsTrigger>
          {type !== 'lxc' && (
            <TabsTrigger value="hardware">{t('instances.hardware', 'Hardware')}</TabsTrigger>
          )}
          <TabsTrigger value="firewall">{t('instances.firewall', 'Firewall')}</TabsTrigger>
          <TabsTrigger value="settings">{t('nav.settings', 'Settings')}</TabsTrigger>
          <TabsTrigger value="destroy" className="text-destructive data-[state=active]:text-destructive">
            {t('instances.destroy', 'Destroy')}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-4">
          <OverviewTab serverId={sid} vmid={vid} type={type} node={node} />
        </TabsContent>
        <TabsContent value="graphs" className="mt-4">
          <GraphsTab serverId={sid} vmid={vid} type={type} node={node} />
        </TabsContent>
        <TabsContent value="snapshots" className="mt-4">
          <SnapshotsTab serverId={sid} vmid={vid} type={type} node={node} />
        </TabsContent>
        <TabsContent value="networking" className="mt-4">
          <NetworkingTab serverId={sid} vmid={vid} type={type} node={node} />
        </TabsContent>
        {type !== 'lxc' && (
          <TabsContent value="hardware" className="mt-4">
            <HardwareTab serverId={sid} vmid={vid} type={type} node={node} />
          </TabsContent>
        )}
        <TabsContent value="firewall" className="mt-4">
          <FirewallTab serverId={sid} vmid={vid} type={type} node={node} />
        </TabsContent>
        <TabsContent value="settings" className="mt-4">
          <SettingsTab serverId={sid} vmid={vid} type={type} node={node} />
        </TabsContent>
        <TabsContent value="destroy" className="mt-4">
          <DestroyTab serverId={sid} vmid={vid} type={type} node={node} name={vm?.name} />
        </TabsContent>
      </Tabs>

      <InstanceActionDialogs
        open={dialog}
        onOpenChange={setDialog}
        serverId={sid}
        vmid={vid}
        type={type}
        node={node}
        name={vm?.name}
        description={vm?.description}
      />
      <PowerConfirmDialog
        open={pendingPower !== null}
        onClose={() => setPendingPower(null)}
        onConfirm={confirmPower}
        action={pendingPower}
        vmName={vm?.name}
        isPending={power.isPending}
      />
    </div>
  );
}
