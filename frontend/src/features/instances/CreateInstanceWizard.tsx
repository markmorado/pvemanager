import { useState, useRef } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useMutation } from '@tanstack/react-query';
import { Server, Cpu, CheckCircle, ChevronLeft, ChevronRight, Loader2, Monitor, Container, HardDrive, KeyRound, Gauge, Disc, Download, RefreshCw, AlertTriangle } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ServerStatusBadge } from '@/components/shared/ServerStatusBadge';
import { GroupIcon } from '@/components/shared/GroupIcon';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useServers, useNodes } from '@/hooks/use-nodes';
import { useTemplates, useTemplateGroups } from '@/hooks/use-templates';
import { useIPAMNetworks } from '@/hooks/use-ipam';
import { scopedNetworks, autoPickNetworkId } from '@/features/ipam/network-scope';
import { useWorkspaceStore } from '@/stores/workspace-store';
import { useLXCTemplates, useLXCStorages, useDeployLXC } from '@/hooks/use-lxc-templates';
import { useNodeIsos, useCreateIsoVM } from '@/hooks/use-instances';
import DownloadIsoDialog from '@/features/images/DownloadIsoDialog';
import { useMySSHKeys, useUserSSHKeys } from '@/hooks/use-ssh-keys';
import { useProfile, useMyQuota } from '@/hooks/use-settings';
import { useUsers } from '@/hooks/use-users';
import { apiClient } from '@/lib/api-client';
import { useDeployTasksStore } from '@/stores/deploy-tasks-store';
import type { VMDeployRequest, OSTemplate, ProxmoxServer, LXCTemplate } from '@/types';
import {
  buildQuotaMetrics, exceededMetrics, exhaustedMetrics,
  type QuotaMetric, type WizardResources,
} from './quota';
import { toast } from 'sonner';

type InstanceKind = 'vm' | 'lxc' | 'iso';

const DNS_NAME_RE = /^[a-zA-Z](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$/;
function isValidDnsName(name: string): boolean {
  return name.length > 0 && name.length <= 63 && DNS_NAME_RE.test(name);
}

function NumericInput({ value, onChange, ...props }: Omit<React.ComponentProps<typeof Input>, 'value' | 'onChange'> & {
  value: number;
  onChange: (n: number) => void;
}) {
  const [raw, setRaw] = useState(String(value));
  const committed = useRef(value);
  if (value !== committed.current) {
    committed.current = value;
    setRaw(String(value));
  }
  return (
    <Input
      {...props}
      type="number"
      value={raw}
      onChange={e => {
        setRaw(e.target.value);
        const n = e.target.value === '' ? 0 : Number(e.target.value);
        if (!Number.isNaN(n)) {
          committed.current = n;
          onChange(n);
        }
      }}
    />
  );
}

const STEPS = ['server', 'type', 'template', 'config', 'confirm'] as const;
type Step = (typeof STEPS)[number];

// ostype Proxmox → подпись в списке. l26 покрывает все современные ядра Linux.
const OS_TYPES = [
  { value: 'l26', label: 'Linux (5.x/6.x)' },
  { value: 'win11', label: 'Windows 11 / 2022' },
  { value: 'win10', label: 'Windows 10 / 2016-2019' },
  { value: 'win8', label: 'Windows 8 / 2012' },
  { value: 'solaris', label: 'Solaris' },
  { value: 'other', label: 'Other' },
] as const;

export default function CreateInstanceWizard({ onClose }: { onClose?: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const handleClose = onClose ?? (() => navigate(-1));
  const [step, setStep] = useState<Step>('server');
  const stepIdx = STEPS.indexOf(step);

  const addDeployTask = useDeployTasksStore((s) => s.addTask);

  // ── Shared state ──────────────────────────────────────────────────────────
  const [kind, setKind] = useState<InstanceKind>('lxc');
  const [selectedServer, setSelectedServer] = useState<ProxmoxServer | null>(null);

  // ── VM state ──────────────────────────────────────────────────────────────
  const [selectedNode, setSelectedNode] = useState('');
  const [selectedTemplate, setSelectedTemplate] = useState<OSTemplate | null>(null);
  const [config, setConfig] = useState<{
    name: string;
    cores: number;
    memory: number;
    disk: number;
    storage: string;
    bridge: string;
    ipam_network_id: number | null;
    ip_address: string;
    gateway: string;
    start_after_create: boolean;
    cloud_init_user: string;
    cloud_init_password: string;
    ssh_keys: string;
  }>({
    name: '',
    cores: 2,
    memory: 2048,
    disk: 30,
    storage: 'local-lvm',
    bridge: 'vmbr0',
    ipam_network_id: null,
    ip_address: '',
    gateway: '',
    start_after_create: true,
    cloud_init_user: '',
    cloud_init_password: '',
    ssh_keys: '',
  });

  // ── LXC state ─────────────────────────────────────────────────────────────
  const [selectedLXCTemplate, setSelectedLXCTemplate] = useState<LXCTemplate | null>(null);
  const [lxcConfig, setLxcConfig] = useState({
    name: '',
    cores: 1,
    memory: 512,
    swap: 512,
    disk: 8,
    storage: 'local-lvm',
    bridge: 'vmbr0',
    ipam_network_id: null as number | null,
    ip_address: '',
    gateway: '',
    password: '',
    ssh_keys: '',
    nameserver: '',
    unprivileged: true,
    start_after_create: true,
    onboot: false,
  });

  // ── ISO (пустая ВМ под установку ОС) state ────────────────────────────────
  const [isoDownloadOpen, setIsoDownloadOpen] = useState(false);
  const [isoConfig, setIsoConfig] = useState({
    name: '',
    iso: '',
    extra_iso: '',
    cores: 2,
    memory: 4096,
    disk: 32,
    storage: 'local-lvm',
    bridge: 'vmbr0',
    ostype: 'l26' as string,
    disk_bus: 'scsi' as 'scsi' | 'sata' | 'virtio',
    net_model: 'virtio' as 'virtio' | 'e1000' | 'rtl8139' | 'vmxnet3',
    bios: 'seabios' as 'seabios' | 'ovmf',
    tpm: false,
    ipam_network_id: null as number | null,
    ip_address: '',
    gateway: '',
    start_after_create: true,
    onboot: false,
  });

  // Queries
  const { data: servers = [] } = useServers();
  const { data: groups = [] } = useTemplateGroups();
  const { data: templates = [] } = useTemplates(undefined, selectedServer?.id);
  const { data: networks = [] } = useIPAMNetworks();
  const { data: lxcTemplates = [], isLoading: lxcLoading } = useLXCTemplates(selectedServer?.id);
  const { data: lxcStorages = [] } = useLXCStorages(selectedServer?.id, selectedNode || undefined, 'rootdir');
  const { data: vmStorages = [] } = useLXCStorages(selectedServer?.id, selectedNode || undefined, 'images');
  const { data: nodesData } = useNodes(selectedServer?.id ?? 0);
  const nodes = nodesData?.nodes ?? [];
  const isoList = useNodeIsos(selectedServer?.id ?? 0, selectedNode, kind === 'iso' && !!selectedNode);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);

  // Нода нужна только для размещения; сеть подбирается по активной рабочей области.
  const handleVMNodeChange = (node: string) => setSelectedNode(node);

  // ── SSH Keys & ownership ──────────────────────────────────────────────────
  const { data: profile } = useProfile();
  const isAdmin = !!profile?.is_admin;
  const { data: allUsers = [] } = useUsers();
  const [ownerId, setOwnerId] = useState<number | null>(null);
  const effectiveOwnerId = isAdmin && ownerId ? ownerId : null;
  const { data: myKeys = [] } = useMySSHKeys();
  const { data: targetUserKeys = [] } = useUserSSHKeys(effectiveOwnerId);
  const [selectedKeyIds, setSelectedKeyIds] = useState<number[]>([]);
  const availableKeys = isAdmin && effectiveOwnerId
    ? [...myKeys, ...targetUserKeys.filter(k => !myKeys.find(m => m.id === k.id))]
    : myKeys;
  const toggleKey = (id: number) =>
    setSelectedKeyIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  // ── Quotas ────────────────────────────────────────────────────────────────
  // Раньше о превышении квоты сообщал только бэкенд (429) — уже после того, как
  // пользователь прошёл весь мастер. Считаем то же самое здесь, чтобы сказать
  // заранее и назвать конкретную метрику.
  //
  // Блокировки применяются только к не-админам: квота у нас загружается своя, а
  // админ может создавать инстанс на другого пользователя, и его собственный
  // лимит тут ни при чём. Подсказку с цифрами при этом видят все.
  const { data: myQuota } = useMyQuota();
  const enforceQuota = !isAdmin;

  const activeResources: WizardResources =
    kind === 'vm'
      ? { cores: config.cores, memoryMb: config.memory, diskGb: config.disk }
      : kind === 'iso'
        ? { cores: isoConfig.cores, memoryMb: isoConfig.memory, diskGb: isoConfig.disk }
        : { cores: lxcConfig.cores, memoryMb: lxcConfig.memory, diskGb: lxcConfig.disk };

  // Метрики, по которым пользователь ограничен. Показываем их всем, включая
  // админов, — блокируем только не-админов (см. выше).
  const quotaMetrics = buildQuotaMetrics(myQuota, activeResources);
  const quotaBlockers = enforceQuota ? exhaustedMetrics(quotaMetrics) : [];
  const quotaOverruns = enforceQuota ? exceededMetrics(quotaMetrics) : [];
  // Придерживаем превышение до шага, где поля ресурсов на экране: иначе
  // конфигурация по умолчанию, не влезающая в остаток квоты, заблокировала бы
  // «Далее» ещё на выборе сервера — и уменьшить её было бы негде.
  const blockOnOverrun = quotaOverruns.length > 0 && (step === 'config' || step === 'confirm');

  const deploy = useMutation({
    mutationFn: (data: VMDeployRequest) =>
      apiClient.post<{ task_id: number; status: string; name: string }>('/templates/api/deploy', data),
    onSuccess: (res) => {
      addDeployTask({
        id: res.task_id,
        name: res.name,
        status: 'pending',
        step: t('wizard.deploy_starting'),
        progress: 0,
        vmid: null,
        node: null,
        error_message: null,
      });
      toast.success(t('wizard.deploy_queued', { name: res.name }));
      navigate('/instances');
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const deployLXC = useDeployLXC();
  const createIsoVM = useCreateIsoVM();

  const next = () => {
    const i = STEPS.indexOf(step);
    if (i < STEPS.length - 1) setStep(STEPS[i + 1]);
  };
  const prev = () => {
    const i = STEPS.indexOf(step);
    if (i > 0) setStep(STEPS[i - 1]);
  };

  const canNext = () => {
    if (step === 'server') return !!selectedServer;
    if (step === 'type') return !!kind;
    if (step === 'template') {
      if (kind === 'vm') return !!selectedTemplate;
      if (kind === 'iso') return !!selectedNode && !!isoConfig.iso;
      return !!selectedLXCTemplate;
    }
    if (step === 'config') {
      if (kind === 'vm') return isValidDnsName(config.name) && !!config.cloud_init_user.trim() && !!config.cloud_init_password.trim();
      if (kind === 'iso') return isValidDnsName(isoConfig.name);
      return isValidDnsName(lxcConfig.name);
    }
    return true;
  };

  // Windows не грузится с SeaBIOS+VirtIO «из коробки»: UEFI, SATA-диск и e1000
  // дают установщику увидеть диск и сеть без подгрузки virtio-драйверов.
  const selectOsType = (ostype: string) => {
    setIsoConfig(p => {
      if (!ostype.startsWith('win')) {
        return { ...p, ostype, bios: 'seabios', tpm: false, disk_bus: 'scsi', net_model: 'virtio' };
      }
      return {
        ...p,
        ostype,
        bios: 'ovmf',
        tpm: ostype === 'win11',
        disk_bus: 'sata',
        net_model: 'e1000',
      };
    });
  };

  const handleDeploy = () => {
    if (kind === 'iso') {
      if (!selectedServer || !selectedNode || !isoConfig.iso) return;
      createIsoVM.mutate(
        {
          server_id: selectedServer.id,
          node: selectedNode,
          name: isoConfig.name,
          iso: isoConfig.iso,
          extra_iso: isoConfig.extra_iso || undefined,
          cores: isoConfig.cores,
          memory: isoConfig.memory,
          disk: isoConfig.disk,
          storage: isoConfig.storage,
          bridge: isoConfig.bridge,
          ostype: isoConfig.ostype,
          disk_bus: isoConfig.disk_bus,
          net_model: isoConfig.net_model,
          bios: isoConfig.bios,
          tpm: isoConfig.tpm,
          ip_address: isoConfig.ip_address || undefined,
          gateway: isoConfig.gateway || undefined,
          ipam_network_id: isoConfig.ipam_network_id || undefined,
          owner_id: effectiveOwnerId ?? undefined,
          start_after_create: isoConfig.start_after_create,
          onboot: isoConfig.onboot,
        },
        {
          onSuccess: (res) => {
            addDeployTask({
              id: res.task_id,
              name: res.name,
              status: 'pending',
              step: t('wizard.deploy_starting'),
              progress: 0,
              vmid: null,
              node: selectedNode,
              error_message: null,
              kind: 'vm_iso',
              server_id: selectedServer.id,
            });
            toast.success(t('wizard.deploy_queued', { name: res.name }));
            navigate('/instances');
          },
          onError: (err: Error) => toast.error(err.message),
        },
      );
      return;
    }
    if (kind === 'lxc') {
      if (!selectedLXCTemplate || !selectedServer) return;
      deployLXC.mutate(
        {
          server_id: selectedServer.id,
          ostemplate: selectedLXCTemplate.volid,
          node: selectedLXCTemplate.node || undefined,
          name: lxcConfig.name,
          cores: lxcConfig.cores,
          memory: lxcConfig.memory,
          swap: lxcConfig.swap,
          disk: lxcConfig.disk,
          storage: lxcConfig.storage,
          bridge: lxcConfig.bridge,
          ip_address: lxcConfig.ip_address || undefined,
          gateway: lxcConfig.gateway || undefined,
          ipam_network_id: lxcConfig.ipam_network_id || undefined,
          password: lxcConfig.password || undefined,
          ssh_keys: lxcConfig.ssh_keys || undefined,
          ssh_key_ids: selectedKeyIds.length ? selectedKeyIds : undefined,
          owner_id: effectiveOwnerId ?? undefined,
          nameserver: lxcConfig.nameserver || undefined,
          unprivileged: lxcConfig.unprivileged,
          start_after_create: lxcConfig.start_after_create,
          onboot: lxcConfig.onboot,
        },
        {
          onSuccess: (res) => {
            addDeployTask({
              id: res.task_id,
              name: res.name,
              status: 'pending',
              step: t('wizard.deploy_starting'),
              progress: 0,
              vmid: null,
              node: null,
              error_message: null,
            });
            toast.success(t('wizard.deploy_queued', { name: res.name }));
            navigate('/instances');
          },
          onError: (err: Error) => toast.error(err.message),
        },
      );
    } else {
      if (!selectedTemplate) return;
      deploy.mutate({
        template_id: selectedTemplate.id,
        name: config.name,
        target_node: selectedNode || undefined,
        cores: config.cores,
        memory: config.memory,
        disk: config.disk,
        target_storage: config.storage,
        start_after_create: config.start_after_create,
        network_bridge: config.bridge,
        ip_address: config.ip_address || undefined,
        gateway: config.gateway || undefined,
        ipam_network_id: config.ipam_network_id || undefined,
        cloud_init_user: config.cloud_init_user || undefined,
        cloud_init_password: config.cloud_init_password || undefined,
        ssh_keys: config.ssh_keys || undefined,
        ssh_key_ids: selectedKeyIds.length ? selectedKeyIds : undefined,
        owner_id: effectiveOwnerId ?? undefined,
      });
    }
  };

  // ── Filtered IPAM networks ────────────────────────────────────────────────
  // Сети активной рабочей области (список уже отфильтрован бэкендом по X-Active-Workspace).
  const vmFilteredNetworks = scopedNetworks(networks, activeWorkspaceId);
  const lxcFilteredNetworks = vmFilteredNetworks;

  // Apply template defaults when template selected
  const selectTemplate = (tpl: OSTemplate) => {
    setSelectedTemplate(tpl);
    setConfig(prev => ({
      ...prev,
      cores: tpl.default_cores,
      memory: tpl.default_memory,
      disk: tpl.default_disk,
    }));
  };

  // Места нет ни под какой инстанс — мастер не открываем: настраивать нечего,
  // деплой всё равно завершится 429 в самом конце.
  if (quotaBlockers.length > 0) {
    return (
      <div className="space-y-6">
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>{t('wizard.quota_blocked_title')}</AlertTitle>
          <AlertDescription>
            <p>{t('wizard.quota_blocked_desc')}</p>
            <ul className="mt-3 space-y-1.5">
              {quotaBlockers.map(m => (
                <li key={m.key} className="flex items-center justify-between gap-4 rounded-md border px-2.5 py-1.5 text-xs">
                  <span>{t(m.labelKey)}</span>
                  <span className="font-medium">
                    {t('wizard.quota_used_of', { used: m.used, limit: m.limit })}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-xs">{t('wizard.quota_blocked_hint')}</p>
          </AlertDescription>
        </Alert>
        <div className="flex border-t pt-4">
          <Button variant="outline" onClick={handleClose}>
            <ChevronLeft className="mr-1 h-4 w-4" />{t('wizard.back')}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Step indicator */}
      <div className="flex items-center gap-2">
        {STEPS.map((s, i) => (
            <div key={s} className="flex items-center gap-2">
              <div className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-medium ${
                i < stepIdx ? 'bg-success/20 text-success' :
                i === stepIdx ? 'bg-primary/20 text-primary ring-2 ring-primary/50' :
                'bg-muted text-muted-foreground'
              }`}>
                {i < stepIdx ? <CheckCircle className="h-4 w-4" /> : i + 1}
              </div>
              {i < STEPS.length - 1 && <div className={`h-0.5 w-8 ${i < stepIdx ? 'bg-success/50' : 'bg-border'}`} />}
            </div>
          ))}
        </div>

      {/* Step 1: Server */}
      {step === 'server' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.select_server')}</h3>
          <div className="grid gap-3 sm:grid-cols-2">
            {servers.map(srv => (
              <Card
                key={srv.id}
                className={`cursor-pointer transition-colors hover:border-primary/50 ${selectedServer?.id === srv.id ? 'border-primary bg-primary/5' : ''}`}
                onClick={() => {
                  setSelectedServer(srv); setSelectedLXCTemplate(null); setSelectedTemplate(null);
                  setSelectedNode('');
                  const nid = autoPickNetworkId(networks, activeWorkspaceId);
                  setConfig(p => ({ ...p, ipam_network_id: nid }));
                  setLxcConfig(p => ({ ...p, ipam_network_id: nid }));
                  setIsoConfig(p => ({ ...p, ipam_network_id: nid, iso: '', extra_iso: '' }));
                }}
              >
                <CardContent className="flex items-center gap-3 p-4">
                  <Server className="h-8 w-8 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <p className="font-medium truncate">{srv.name}</p>
                    <p className="text-sm text-muted-foreground font-mono">{srv.ip_address}:{srv.port}</p>
                  </div>
                  <ServerStatusBadge
                    isOnline={srv.is_online}
                    errorKind={srv.last_error_kind}
                    lastError={srv.last_error}
                  />
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {/* Step 2: Type selection */}
      {step === 'type' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.select_type')}</h3>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Card
              interactive
              className={kind === 'vm' ? 'border-primary bg-primary/5' : ''}
              onClick={() => setKind('vm')}
            >
              <CardContent className="flex items-center gap-4 p-5">
                <HardDrive className="h-10 w-10 text-primary shrink-0" />
                <div>
                  <p className="font-semibold">{t('wizard.type_vm')}</p>
                  <p className="text-sm text-muted-foreground">{t('wizard.type_vm_desc')}</p>
                </div>
                {kind === 'vm' && <CheckCircle className="ml-auto h-5 w-5 text-primary shrink-0" />}
              </CardContent>
            </Card>
            <Card
              interactive
              className={kind === 'lxc' ? 'border-success bg-success/5' : ''}
              onClick={() => setKind('lxc')}
            >
              <CardContent className="flex items-center gap-4 p-5">
                <Container className="h-10 w-10 text-success shrink-0" />
                <div>
                  <p className="font-semibold">{t('wizard.type_lxc')}</p>
                  <p className="text-sm text-muted-foreground">{t('wizard.type_lxc_desc')}</p>
                </div>
                {kind === 'lxc' && <CheckCircle className="ml-auto h-5 w-5 text-success shrink-0" />}
              </CardContent>
            </Card>
            <Card
              interactive
              className={kind === 'iso' ? 'border-primary bg-primary/5' : ''}
              onClick={() => setKind('iso')}
            >
              <CardContent className="flex items-center gap-4 p-5">
                <Disc className="h-10 w-10 text-primary shrink-0" />
                <div>
                  <p className="font-semibold">{t('wizard.type_iso')}</p>
                  <p className="text-sm text-muted-foreground">{t('wizard.type_iso_desc')}</p>
                </div>
                {kind === 'iso' && <CheckCircle className="ml-auto h-5 w-5 text-primary shrink-0" />}
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* Step 3: ISO selection — пустая ВМ под установку ОС */}
      {step === 'template' && kind === 'iso' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.select_iso')}</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Label>{t('wizard.target_node')}</Label>
              <Select value={selectedNode} onValueChange={v => { setSelectedNode(v ?? ''); setIsoConfig(p => ({ ...p, iso: '', extra_iso: '' })); }}>
                <SelectTrigger className="mt-1"><SelectValue placeholder={t('wizard.select_node_first')} /></SelectTrigger>
                <SelectContent>
                  {nodes.map(n => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
                </SelectContent>
              </Select>
              <p className="mt-1 text-xs text-muted-foreground">{t('wizard.iso_node_hint')}</p>
            </div>
            <div>
              <Label>{t('wizard.iso_image')}</Label>
              <div className="mt-1 flex gap-2">
                <Select value={isoConfig.iso} onValueChange={v => setIsoConfig(p => ({ ...p, iso: v ?? '' }))}>
                  <SelectTrigger className="flex-1">
                    <SelectValue placeholder={isoList.isLoading ? t('common.loading') : t('wizard.iso_select_placeholder')} />
                  </SelectTrigger>
                  <SelectContent>
                    {(isoList.data?.isos || []).map(iso => (
                      <SelectItem key={iso.volid} value={iso.volid}>{iso.name || iso.volid}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button variant="outline" size="icon" disabled={!selectedNode || isoList.isFetching}
                  title={t('wizard.iso_refresh')} onClick={() => isoList.refetch()}>
                  <RefreshCw className={`h-4 w-4 ${isoList.isFetching ? 'animate-spin' : ''}`} />
                </Button>
              </div>
              {selectedNode && !isoList.isLoading && (isoList.data?.isos?.length ?? 0) === 0 && (
                <p className="mt-1 text-xs text-warning">{t('wizard.no_isos')}</p>
              )}
              <Button variant="link" size="sm" className="mt-1 h-auto p-0" disabled={!selectedNode}
                onClick={() => setIsoDownloadOpen(true)}>
                <Download className="mr-1 h-3.5 w-3.5" /> {t('wizard.iso_download')}
              </Button>
            </div>
            <div className="sm:col-span-2">
              <Label>{t('wizard.iso_extra')}</Label>
              <Select value={isoConfig.extra_iso || '__none__'} onValueChange={v => setIsoConfig(p => ({ ...p, extra_iso: v && v !== '__none__' ? v : '' }))}>
                <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">{t('wizard.iso_extra_none')}</SelectItem>
                  {(isoList.data?.isos || []).map(iso => (
                    <SelectItem key={iso.volid} value={iso.volid}>{iso.name || iso.volid}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="mt-1 text-xs text-muted-foreground">{t('wizard.iso_extra_hint')}</p>
            </div>
          </div>
        </div>
      )}

      {/* Step 3: Template — VM */}
      {step === 'template' && kind === 'vm' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.select_template')}</h3>
          {groups.map(group => {
            const groupTemplates = templates.filter(t => t.group_id === group.id && t.vm_type === 'qemu');
            if (groupTemplates.length === 0) return null;
            return (
              <div key={group.id} className="space-y-2">
                <p className="text-sm font-medium text-muted-foreground">{group.name}</p>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {groupTemplates.map(tpl => (
                    <Card
                      key={tpl.id}
                      className={`cursor-pointer transition-colors hover:border-primary/50 ${selectedTemplate?.id === tpl.id ? 'border-primary bg-primary/5' : ''}`}
                      onClick={() => selectTemplate(tpl)}
                    >
                      <CardContent className="p-3">
                        <div className="flex items-center gap-2">
                          {tpl.group_icon ? (
                            <GroupIcon icon={tpl.group_icon} />
                          ) : (
                            <Monitor className="h-5 w-5 text-muted-foreground" />
                          )}
                          <span className="font-medium text-sm truncate">{tpl.name}</span>
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {tpl.default_cores} vCPU · {tpl.default_memory} MB · {tpl.default_disk} GB
                        </p>
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </div>
            );
          })}
          {templates.filter(t => t.vm_type === 'qemu').length === 0 && (
            <p className="text-center text-muted-foreground py-8">{t('wizard.no_templates')}</p>
          )}
        </div>
      )}

      {/* Step 3: Template — LXC */}
      {step === 'template' && kind === 'lxc' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.select_lxc_template')}</h3>
          {lxcLoading && (
            <div className="flex items-center justify-center py-8 text-muted-foreground gap-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>{t('common.loading')}</span>
            </div>
          )}
          {!lxcLoading && lxcTemplates.length === 0 && (
            <p className="text-center text-muted-foreground py-8">{t('wizard.no_lxc_templates')}</p>
          )}
          {!lxcLoading && lxcTemplates.length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {lxcTemplates.map(tpl => (
                <Card
                  key={tpl.volid}
                  className={`cursor-pointer transition-colors hover:border-success/50 ${selectedLXCTemplate?.volid === tpl.volid ? 'border-success bg-success/5' : ''}`}
                  onClick={() => {
                    setSelectedLXCTemplate(tpl);
                    const nid = autoPickNetworkId(networks, activeWorkspaceId);
                    setLxcConfig(p => ({ ...p, ipam_network_id: nid }));
                  }}
                >
                  <CardContent className="p-3">
                    <div className="flex items-center gap-2">
                      <Container className="h-5 w-5 text-success shrink-0" />
                      <span className="font-medium text-sm truncate">{tpl.name}</span>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground font-mono">
                      {tpl.storage} · {tpl.node}
                    </p>
                    {tpl.size > 0 && (
                      <p className="text-xs text-muted-foreground">
                        {(tpl.size / 1024 / 1024).toFixed(0)} MB
                      </p>
                    )}
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Step 4: Configuration — VM */}
      {step === 'config' && kind === 'vm' && (
        <div className="space-y-6">
          <h3 className="text-lg font-semibold">{t('wizard.configure')}</h3>
          <QuotaHint metrics={quotaMetrics} />
          <div className="grid gap-6 sm:grid-cols-2">
            {/* Basic */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.basic')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('common.name')}</Label>
                  <Input value={config.name} onChange={e => setConfig(p => ({ ...p, name: e.target.value }))} placeholder={t('common.placeholder_vm_name')} />
                  {config.name && !isValidDnsName(config.name) && (
                    <p className="mt-1 text-xs text-destructive">{t('wizard.invalid_name')}</p>
                  )}
                </div>
                <div>
                  <Label>{t('wizard.target_node')}</Label>
                  <Select value={selectedNode || '__auto__'} onValueChange={v => handleVMNodeChange(v && v !== '__auto__' ? v : '')}>
                    <SelectTrigger><SelectValue placeholder={t('wizard.auto_select')} /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__auto__">{t('wizard.auto_select')}</SelectItem>
                      {nodes.map(n => (
                        <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="start_after"
                    checked={config.start_after_create}
                    onChange={e => setConfig(p => ({ ...p, start_after_create: e.target.checked }))}
                  />
                  <Label htmlFor="start_after">{t('wizard.start_after_create')}</Label>
                </div>
              </CardContent>
            </Card>

            {/* Resources */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm flex items-center gap-2"><Cpu className="h-4 w-4" />{t('wizard.resources')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>vCPU</Label>
                  <NumericInput
                    min={selectedTemplate?.min_cores || 1}
                    value={config.cores}
                    onChange={v => setConfig(p => ({ ...p, cores: v }))}
                  />
                </div>
                <div>
                  <Label>{t('wizard.memory_mb')}</Label>
                  <NumericInput
                    min={selectedTemplate?.min_memory || 256}
                    step={256}
                    value={config.memory}
                    onChange={v => setConfig(p => ({ ...p, memory: v }))}
                  />
                </div>
                <div>
                  <Label>{t('wizard.disk_gb')}</Label>
                  <NumericInput
                    min={selectedTemplate?.min_disk || 5}
                    value={config.disk}
                    onChange={v => setConfig(p => ({ ...p, disk: v }))}
                  />
                </div>
                <div>
                  <Label>{t('wizard.storage')}</Label>
                  <Select value={config.storage} onValueChange={v => setConfig(p => ({ ...p, storage: v ?? '' }))}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {vmStorages.length > 0
                        ? vmStorages.map(s => <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>)
                        : <SelectItem value="local-lvm">local-lvm</SelectItem>
                      }
                    </SelectContent>
                  </Select>
                </div>
              </CardContent>
            </Card>

            {/* Network */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.network')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.bridge')}</Label>
                  <Input value={config.bridge} onChange={e => setConfig(p => ({ ...p, bridge: e.target.value }))} />
                </div>
                <div>
                  <Label>IPAM {t('wizard.network')}</Label>
                  <Select value={String(config.ipam_network_id || '')} onValueChange={v => setConfig(p => ({ ...p, ipam_network_id: v ? Number(v) : null }))}>
                    <SelectTrigger><SelectValue placeholder={t('wizard.manual_ip')} /></SelectTrigger>
                    <SelectContent>
                      {vmFilteredNetworks.map(n => (
                        <SelectItem key={n.id} value={String(n.id)}>{n.name} ({n.network})</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!config.ipam_network_id && (
                  <>
                    <div>
                      <Label>IP</Label>
                      <Input value={config.ip_address} onChange={e => setConfig(p => ({ ...p, ip_address: e.target.value }))} placeholder="10.10.10.5" />
                    </div>
                    <div>
                      <Label>Gateway</Label>
                      <Input value={config.gateway} onChange={e => setConfig(p => ({ ...p, gateway: e.target.value }))} placeholder="10.10.10.1" />
                    </div>
                  </>
                )}
              </CardContent>
            </Card>

            {/* Cloud-init */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">Cloud-Init</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.ci_user')} <span className="text-destructive">*</span></Label>
                  <Input
                    value={config.cloud_init_user}
                    onChange={e => setConfig(p => ({ ...p, cloud_init_user: e.target.value }))}
                    placeholder={t('common.placeholder_ubuntu')}
                    className={!config.cloud_init_user.trim() ? 'border-destructive' : ''}
                  />
                </div>
                <div>
                  <Label>{t('wizard.ci_password')} <span className="text-destructive">*</span></Label>
                  <Input
                    type="password"
                    value={config.cloud_init_password}
                    onChange={e => setConfig(p => ({ ...p, cloud_init_password: e.target.value }))}
                    className={!config.cloud_init_password.trim() ? 'border-destructive' : ''}
                  />
                </div>
                {isAdmin && (
                  <SSHOwnerSelector
                    users={allUsers}
                    value={ownerId}
                    onChange={(v) => { setOwnerId(v); setSelectedKeyIds([]); }}
                  />
                )}
                <SSHKeyPicker keys={availableKeys} selected={selectedKeyIds} onToggle={toggleKey} />
                {availableKeys.length === 0 && (
                  <div>
                    <Label>SSH Keys <span className="text-muted-foreground text-xs font-normal">{t('common.safer')}</span></Label>
                    <textarea
                      className="w-full rounded-md border bg-transparent px-3 py-2 text-sm font-mono min-h-[60px]"
                      value={config.ssh_keys}
                      onChange={e => setConfig(p => ({ ...p, ssh_keys: e.target.value }))}
                      placeholder={t('common.placeholder_ssh_key')}
                    />
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* Step 4: Configuration — ISO */}
      {step === 'config' && kind === 'iso' && (
        <div className="space-y-6">
          <h3 className="text-lg font-semibold">{t('wizard.configure')}</h3>
          <QuotaHint metrics={quotaMetrics} />
          <div className="grid gap-6 sm:grid-cols-2">
            {/* Basic */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.basic')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('common.name')}</Label>
                  <Input value={isoConfig.name} onChange={e => setIsoConfig(p => ({ ...p, name: e.target.value }))} placeholder={t('common.placeholder_vm_name')} />
                  {isoConfig.name && !isValidDnsName(isoConfig.name) && (
                    <p className="mt-1 text-xs text-destructive">{t('wizard.invalid_name')}</p>
                  )}
                </div>
                <div>
                  <Label>{t('wizard.os_type')}</Label>
                  <Select value={isoConfig.ostype} onValueChange={v => v && selectOsType(v)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {OS_TYPES.map(o => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
                    </SelectContent>
                  </Select>
                  <p className="mt-1 text-xs text-muted-foreground">{t('wizard.os_type_hint')}</p>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="iso_start_after"
                    checked={isoConfig.start_after_create}
                    onChange={e => setIsoConfig(p => ({ ...p, start_after_create: e.target.checked }))}
                  />
                  <Label htmlFor="iso_start_after">{t('wizard.start_after_create')}</Label>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="iso_onboot"
                    checked={isoConfig.onboot}
                    onChange={e => setIsoConfig(p => ({ ...p, onboot: e.target.checked }))}
                  />
                  <Label htmlFor="iso_onboot">{t('instances.start_on_boot')}</Label>
                </div>
                {isAdmin && (
                  <SSHOwnerSelector
                    users={allUsers}
                    value={ownerId}
                    onChange={(v) => setOwnerId(v)}
                  />
                )}
              </CardContent>
            </Card>

            {/* Resources */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm flex items-center gap-2"><Cpu className="h-4 w-4" />{t('wizard.resources')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>vCPU</Label>
                  <NumericInput min={1} value={isoConfig.cores} onChange={v => setIsoConfig(p => ({ ...p, cores: v }))} />
                </div>
                <div>
                  <Label>{t('wizard.memory_mb')}</Label>
                  <NumericInput min={256} step={256} value={isoConfig.memory} onChange={v => setIsoConfig(p => ({ ...p, memory: v }))} />
                </div>
                <div>
                  <Label>{t('wizard.disk_gb')}</Label>
                  <NumericInput min={1} value={isoConfig.disk} onChange={v => setIsoConfig(p => ({ ...p, disk: v }))} />
                </div>
                <div>
                  <Label>{t('wizard.storage')}</Label>
                  <Select value={isoConfig.storage} onValueChange={v => setIsoConfig(p => ({ ...p, storage: v ?? '' }))}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {vmStorages.length > 0
                        ? vmStorages.map(s => <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>)
                        : <SelectItem value="local-lvm">local-lvm</SelectItem>
                      }
                    </SelectContent>
                  </Select>
                </div>
              </CardContent>
            </Card>

            {/* Hardware */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm flex items-center gap-2"><Disc className="h-4 w-4" />{t('wizard.iso_hardware')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.disk_bus')}</Label>
                  <Select value={isoConfig.disk_bus} onValueChange={v => v && setIsoConfig(p => ({ ...p, disk_bus: v as typeof p.disk_bus }))}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="scsi">VirtIO SCSI</SelectItem>
                      <SelectItem value="sata">SATA</SelectItem>
                      <SelectItem value="virtio">VirtIO Block</SelectItem>
                    </SelectContent>
                  </Select>
                  {isoConfig.ostype.startsWith('win') && isoConfig.disk_bus !== 'sata' && (
                    <p className="mt-1 text-xs text-warning">{t('wizard.disk_bus_windows_hint')}</p>
                  )}
                </div>
                <div>
                  <Label>BIOS</Label>
                  <Select value={isoConfig.bios} onValueChange={v => v && setIsoConfig(p => ({ ...p, bios: v as typeof p.bios }))}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="seabios">SeaBIOS (Legacy)</SelectItem>
                      <SelectItem value="ovmf">OVMF (UEFI)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <Label>{t('wizard.net_model')}</Label>
                  <Select value={isoConfig.net_model} onValueChange={v => v && setIsoConfig(p => ({ ...p, net_model: v as typeof p.net_model }))}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="virtio">VirtIO</SelectItem>
                      <SelectItem value="e1000">Intel E1000</SelectItem>
                      <SelectItem value="rtl8139">Realtek RTL8139</SelectItem>
                      <SelectItem value="vmxnet3">VMware vmxnet3</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="iso_tpm"
                    checked={isoConfig.tpm}
                    onChange={e => setIsoConfig(p => ({ ...p, tpm: e.target.checked }))}
                  />
                  <Label htmlFor="iso_tpm">{t('wizard.tpm')}</Label>
                </div>
              </CardContent>
            </Card>

            {/* Network */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.network')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.bridge')}</Label>
                  <Input value={isoConfig.bridge} onChange={e => setIsoConfig(p => ({ ...p, bridge: e.target.value }))} />
                </div>
                <div>
                  <Label>IPAM {t('wizard.network')}</Label>
                  <Select value={String(isoConfig.ipam_network_id || '')} onValueChange={v => setIsoConfig(p => ({ ...p, ipam_network_id: v ? Number(v) : null }))}>
                    <SelectTrigger><SelectValue placeholder={t('wizard.manual_ip')} /></SelectTrigger>
                    <SelectContent>
                      {vmFilteredNetworks.map(n => (
                        <SelectItem key={n.id} value={String(n.id)}>{n.name} ({n.network})</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!isoConfig.ipam_network_id && (
                  <>
                    <div>
                      <Label>IP</Label>
                      <Input value={isoConfig.ip_address} onChange={e => setIsoConfig(p => ({ ...p, ip_address: e.target.value }))} placeholder="10.10.10.5" />
                    </div>
                    <div>
                      <Label>Gateway</Label>
                      <Input value={isoConfig.gateway} onChange={e => setIsoConfig(p => ({ ...p, gateway: e.target.value }))} placeholder="10.10.10.1" />
                    </div>
                  </>
                )}
                <p className="text-xs text-muted-foreground">{t('wizard.iso_ip_hint')}</p>
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* Step 4: Configuration — LXC */}
      {step === 'config' && kind === 'lxc' && (
        <div className="space-y-6">
          <h3 className="text-lg font-semibold">{t('wizard.configure')}</h3>
          <QuotaHint metrics={quotaMetrics} />
          <div className="grid gap-6 sm:grid-cols-2">
            {/* Basic */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.basic')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.hostname')}</Label>
                  <Input
                    value={lxcConfig.name}
                    onChange={e => setLxcConfig(p => ({ ...p, name: e.target.value }))}
                    placeholder={t('common.placeholder_vm_name')}
                  />
                  {lxcConfig.name && !isValidDnsName(lxcConfig.name) && (
                    <p className="mt-1 text-xs text-destructive">{t('wizard.invalid_name')}</p>
                  )}
                </div>
                <div>
                  <Label>{t('wizard.storage')}</Label>
                  <Select value={lxcConfig.storage} onValueChange={v => setLxcConfig(p => ({ ...p, storage: v ?? '' }))}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {lxcStorages.length > 0
                        ? lxcStorages.map(s => <SelectItem key={s.storage} value={s.storage}>{s.storage} ({s.type})</SelectItem>)
                        : <SelectItem value="local-lvm">local-lvm</SelectItem>
                      }
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="lxc_start_after"
                    checked={lxcConfig.start_after_create}
                    onChange={e => setLxcConfig(p => ({ ...p, start_after_create: e.target.checked }))}
                  />
                  <Label htmlFor="lxc_start_after">{t('wizard.start_after_create')}</Label>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="lxc_unprivileged"
                    checked={lxcConfig.unprivileged}
                    onChange={e => setLxcConfig(p => ({ ...p, unprivileged: e.target.checked }))}
                  />
                  <Label htmlFor="lxc_unprivileged">{t('wizard.unprivileged')}</Label>
                </div>
              </CardContent>
            </Card>

            {/* Resources */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm flex items-center gap-2"><Cpu className="h-4 w-4" />{t('wizard.resources')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>vCPU</Label>
                  <NumericInput min={1} value={lxcConfig.cores} onChange={v => setLxcConfig(p => ({ ...p, cores: v }))} />
                </div>
                <div>
                  <Label>{t('wizard.memory_mb')}</Label>
                  <NumericInput min={64} step={128} value={lxcConfig.memory} onChange={v => setLxcConfig(p => ({ ...p, memory: v }))} />
                </div>
                <div>
                  <Label>{t('wizard.swap_mb')}</Label>
                  <NumericInput min={0} step={128} value={lxcConfig.swap} onChange={v => setLxcConfig(p => ({ ...p, swap: v }))} />
                </div>
                <div>
                  <Label>{t('wizard.disk_gb')}</Label>
                  <NumericInput min={1} value={lxcConfig.disk} onChange={v => setLxcConfig(p => ({ ...p, disk: v }))} />
                </div>
              </CardContent>
            </Card>

            {/* Network */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.network')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.bridge')}</Label>
                  <Input value={lxcConfig.bridge} onChange={e => setLxcConfig(p => ({ ...p, bridge: e.target.value }))} />
                </div>
                <div>
                  <Label>IPAM {t('wizard.network')}</Label>
                  <Select value={String(lxcConfig.ipam_network_id || '')} onValueChange={v => setLxcConfig(p => ({ ...p, ipam_network_id: v ? Number(v) : null }))}>
                    <SelectTrigger><SelectValue placeholder={t('wizard.manual_ip')} /></SelectTrigger>
                    <SelectContent>
                      {lxcFilteredNetworks.map(n => (
                        <SelectItem key={n.id} value={String(n.id)}>{n.name} ({n.network})</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!lxcConfig.ipam_network_id && (
                  <>
                    <div>
                      <Label>IP (CIDR)</Label>
                      <Input value={lxcConfig.ip_address} onChange={e => setLxcConfig(p => ({ ...p, ip_address: e.target.value }))} placeholder={t('wizard.ip_cidr_placeholder')} />
                    </div>
                    <div>
                      <Label>Gateway</Label>
                      <Input value={lxcConfig.gateway} onChange={e => setLxcConfig(p => ({ ...p, gateway: e.target.value }))} placeholder="10.10.10.1" />
                    </div>
                  </>
                )}
                <div>
                  <Label>DNS</Label>
                  <Input value={lxcConfig.nameserver} onChange={e => setLxcConfig(p => ({ ...p, nameserver: e.target.value }))} placeholder="8.8.8.8" />
                </div>
              </CardContent>
            </Card>

            {/* Auth */}
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">{t('wizard.lxc_password')}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <Label>{t('wizard.lxc_password')}</Label>
                  <Input
                    type="password"
                    value={lxcConfig.password}
                    onChange={e => setLxcConfig(p => ({ ...p, password: e.target.value }))}
                    placeholder={t('wizard.lxc_password_placeholder')}
                  />
                </div>
                {isAdmin && (
                  <SSHOwnerSelector
                    users={allUsers}
                    value={ownerId}
                    onChange={(v) => { setOwnerId(v); setSelectedKeyIds([]); }}
                  />
                )}
                <SSHKeyPicker keys={availableKeys} selected={selectedKeyIds} onToggle={toggleKey} />
                {availableKeys.length === 0 && (
                  <div>
                    <Label>SSH Keys <span className="text-muted-foreground text-xs font-normal">{t('common.safer')}</span></Label>
                    <textarea
                      className="w-full rounded-md border bg-transparent px-3 py-2 text-sm font-mono min-h-[60px]"
                      value={lxcConfig.ssh_keys}
                      onChange={e => setLxcConfig(p => ({ ...p, ssh_keys: e.target.value }))}
                      placeholder={t('common.placeholder_ssh_key')}
                    />
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* Step 5: Confirm — VM */}
      {step === 'confirm' && kind === 'vm' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.confirm')}</h3>
          <Card>
            <CardContent className="p-6 space-y-3">
              <Row label={t('wizard.server_label')} value={selectedServer?.name || ''} />
              <Row label={t('common.type')} value="VM (QEMU)" />
              <Row label={t('wizard.template_label')} value={selectedTemplate?.name || ''} />
              <Row label={t('common.name')} value={config.name} />
              <Row label={t('common.vcpu')} value={String(config.cores)} />
              <Row label={t('wizard.memory_mb')} value={`${config.memory} MB`} />
              <Row label={t('wizard.disk_gb')} value={`${config.disk} GB`} />
              <Row label={t('wizard.storage')} value={config.storage} />
              <Row label={t('wizard.bridge')} value={config.bridge} />
              {config.ipam_network_id && <Row label={t('common.ipam')} value={networks.find(n => n.id === config.ipam_network_id)?.name || 'Auto'} />}
              {config.ip_address && <Row label={t('common.primary_ip')} value={config.ip_address} />}
              {config.cloud_init_user && <Row label={t('wizard.ci_user')} value={config.cloud_init_user} />}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Step 5: Confirm — ISO */}
      {step === 'confirm' && kind === 'iso' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.confirm')}</h3>
          <Card>
            <CardContent className="p-6 space-y-3">
              <Row label={t('wizard.server_label')} value={selectedServer?.name || ''} />
              <Row label={t('common.type')} value="VM (QEMU) · ISO" />
              <Row label={t('wizard.target_node')} value={selectedNode} />
              <Row label={t('wizard.iso_image')} value={isoConfig.iso.split('/').pop() || isoConfig.iso} />
              {isoConfig.extra_iso && <Row label={t('wizard.iso_extra')} value={isoConfig.extra_iso.split('/').pop() || isoConfig.extra_iso} />}
              <Row label={t('common.name')} value={isoConfig.name} />
              <Row label={t('common.vcpu')} value={String(isoConfig.cores)} />
              <Row label={t('wizard.memory_mb')} value={`${isoConfig.memory} MB`} />
              <Row label={t('wizard.disk_gb')} value={`${isoConfig.disk} GB`} />
              <Row label={t('wizard.storage')} value={isoConfig.storage} />
              <Row label={t('wizard.disk_bus')} value={isoConfig.disk_bus} />
              <Row label="BIOS" value={isoConfig.bios === 'ovmf' ? 'OVMF (UEFI)' : 'SeaBIOS'} />
              {isoConfig.tpm && <Row label={t('wizard.tpm')} value={t('common.yes')} />}
              <Row label={t('wizard.bridge')} value={`${isoConfig.bridge} · ${isoConfig.net_model}`} />
              {isoConfig.ipam_network_id && <Row label={t('common.ipam')} value={networks.find(n => n.id === isoConfig.ipam_network_id)?.name || 'Auto'} />}
              {isoConfig.ip_address && <Row label={t('common.primary_ip')} value={isoConfig.ip_address} />}
            </CardContent>
          </Card>
          <p className="text-sm text-muted-foreground">{t('wizard.iso_next_steps')}</p>
        </div>
      )}

      {/* Step 5: Confirm — LXC */}
      {step === 'confirm' && kind === 'lxc' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">{t('wizard.confirm')}</h3>
          <Card>
            <CardContent className="p-6 space-y-3">
              <Row label={t('wizard.server_label')} value={selectedServer?.name || ''} />
              <Row label={t('common.type')} value="LXC Container" />
              <Row label={t('wizard.lxc_template_label')} value={selectedLXCTemplate?.name || ''} />
              <Row label={t('wizard.hostname')} value={lxcConfig.name} />
              <Row label={t('common.vcpu')} value={String(lxcConfig.cores)} />
              <Row label={t('wizard.memory_mb')} value={`${lxcConfig.memory} MB`} />
              <Row label={t('wizard.swap_mb')} value={`${lxcConfig.swap} MB`} />
              <Row label={t('wizard.disk_gb')} value={`${lxcConfig.disk} GB`} />
              <Row label={t('wizard.storage')} value={lxcConfig.storage} />
              <Row label={t('wizard.bridge')} value={lxcConfig.bridge} />
              {lxcConfig.ipam_network_id && <Row label={t('common.ipam')} value={networks.find(n => n.id === lxcConfig.ipam_network_id)?.name || 'Auto'} />}
              {lxcConfig.ip_address && <Row label={t('common.primary_ip')} value={lxcConfig.ip_address} />}
              <Row label={t('wizard.unprivileged')} value={lxcConfig.unprivileged ? t('common.yes') : t('common.no')} />
            </CardContent>
          </Card>
        </div>
      )}

      {/* Загрузка ISO прямо из мастера: файл появится в списке после фоновой задачи */}
      {selectedServer && (
        <DownloadIsoDialog
          open={isoDownloadOpen}
          onClose={() => { setIsoDownloadOpen(false); isoList.refetch(); }}
          serverId={selectedServer.id}
          node={selectedNode}
        />
      )}

      {/* Выбранная конфигурация не влезает в квоту — говорим об этом до отправки */}
      {blockOnOverrun && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>{t('wizard.quota_exceeded_title')}</AlertTitle>
          <AlertDescription>
            <ul className="mt-2 space-y-1.5">
              {quotaOverruns.map(m => (
                <li key={m.key} className="flex items-center justify-between gap-4 rounded-md border px-2.5 py-1.5 text-xs">
                  <span>{t(m.labelKey)}</span>
                  <span className="font-medium">
                    {t('wizard.quota_used_of', { used: m.used + m.add, limit: m.limit })}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-xs">{t('wizard.quota_exceeded_hint')}</p>
          </AlertDescription>
        </Alert>
      )}

      {/* Navigation */}
      <div className="flex items-center justify-between border-t pt-4">
        <Button variant="outline" onClick={stepIdx === 0 ? handleClose : prev}>
          {stepIdx === 0 ? t('common.cancel') : <><ChevronLeft className="mr-1 h-4 w-4" />{t('wizard.back')}</>}
        </Button>
        {step === 'confirm' ? (
          <Button
            onClick={handleDeploy}
            disabled={
              deploy.isPending || deployLXC.isPending || createIsoVM.isPending ||
              blockOnOverrun
            }
          >
            {(deploy.isPending || deployLXC.isPending || createIsoVM.isPending) && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('wizard.deploy')}
          </Button>
        ) : (
          <Button onClick={next} disabled={!canNext() || blockOnOverrun}>
            {t('wizard.next')}<ChevronRight className="ml-1 h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  );
}
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-sm font-medium">{value}</span>
    </div>
  );
}

/**
 * Shows the deploying user's remaining quota at the config step, projecting the
 * currently-entered resources. Renders nothing when the user has no quota set.
 * Note: reflects the current user's quota (the common self-deploy case), not an
 * admin-selected owner's quota.
 */
function QuotaHint({ metrics }: { metrics: QuotaMetric[] }) {
  const { t } = useTranslation();
  if (metrics.length === 0) return null;

  return (
    <Card className="border-warning/40">
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <Gauge className="h-4 w-4" />{t('users.quota.manage')}
        </CardTitle>
        {/* Цифры — прогноз с учётом создаваемого инстанса, а не текущий расход:
            без подписи «4 / 4» читается как «лимит уже исчерпан». */}
        <p className="text-xs text-muted-foreground">{t('wizard.quota_projection_note')}</p>
      </CardHeader>
      <CardContent className="grid gap-2 sm:grid-cols-2">
        {metrics.map(m => {
          const projected = m.used + m.add;
          return (
            <div key={m.key} className="flex items-center justify-between text-xs rounded-md border px-2.5 py-1.5">
              <span className="text-muted-foreground">{t(m.labelKey)}</span>
              <span className={projected > m.limit ? 'text-destructive font-medium' : ''}>
                {projected} / {m.limit}
              </span>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

function SSHOwnerSelector({
  users, value, onChange,
}: {
  users: { id: number; username: string; full_name?: string | null }[];
  value: number | null;
  onChange: (v: number | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <div>
      <Label>{t('wizard.ssh_keys_owner')}</Label>
      <Select value={value ? String(value) : '__none__'} onValueChange={v => onChange(v === '__none__' ? null : Number(v))}>
        <SelectTrigger><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="__none__">{t('wizard.ssh_keys_owner_self')}</SelectItem>
          {users.map(u => (
            <SelectItem key={u.id} value={String(u.id)}>
              {u.username}{u.full_name ? ` (${u.full_name})` : ''}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function SSHKeyPicker({
  keys, selected, onToggle,
}: {
  keys: { id: number; name: string; fingerprint?: string | null; comment?: string | null }[];
  selected: number[];
  onToggle: (id: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <div>
      <Label className="flex items-center gap-2">
        <KeyRound className="h-3.5 w-3.5" />
        {t('wizard.ssh_keys_select')}
      </Label>
      {keys.length === 0 ? (
        <p className="text-xs text-muted-foreground mt-1">{t('wizard.no_saved_keys')}</p>
      ) : (
        <div className="mt-1 space-y-1 max-h-40 overflow-y-auto rounded-md border p-2">
          {keys.map(k => (
            <label
              key={k.id}
              className="flex items-start gap-2 text-xs cursor-pointer hover:bg-accent rounded px-2 py-1"
            >
              <Checkbox
                className="mt-0.5"
                checked={selected.includes(k.id)}
                onChange={() => onToggle(k.id)}
              />
              <div className="min-w-0 flex-1">
                <div className="font-medium truncate">{k.name}</div>
                {k.fingerprint && <div className="font-mono text-muted-foreground truncate">{k.fingerprint}</div>}
                {k.comment && <div className="text-muted-foreground truncate">{k.comment}</div>}
              </div>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
