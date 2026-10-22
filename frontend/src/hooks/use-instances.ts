import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { apiClient } from '@/lib/api-client';
import { getTasksWebSocket } from '@/lib/websocket';
import { useBulkTasksStore, type BulkTask, type BulkTaskResult } from '@/stores/bulk-tasks-store';
import type { VMInstance, Snapshot, VMConfig, DiskInfo } from '@/types';

// ==================== Query Keys ====================
export const vmKeys = {
  all: ['virtual-machines'] as const,
  resources: (serverId?: number) => ['resources', serverId] as const,
  resourcesAll: ['resources', 'all'] as const,
  detail: (serverId: number, vmid: number) => ['vm', serverId, vmid] as const,
  config: (serverId: number, vmid: number) => ['vm', serverId, vmid, 'config'] as const,
  status: (serverId: number, vmid: number) => ['vm', serverId, vmid, 'status'] as const,
  snapshots: (serverId: number, vmid: number) => ['vm', serverId, vmid, 'snapshots'] as const,
  interfaces: (serverId: number, vmid: number) => ['vm', serverId, vmid, 'interfaces'] as const,
};

// ==================== VM List ====================
// No HTTP polling: live metrics arrive via WebSocket (`useInstancesMetricsSync`).
// We still refetch on reconnect/focus to recover from missed events.
export function useVirtualMachines() {
  return useQuery({
    queryKey: vmKeys.all,
    queryFn: () => apiClient.get<VMInstance[]>('/proxmox/api/virtual-machines'),
    refetchInterval: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
}

/** Subscribes to WebSocket vm_status_update / vm_deleted / vm_created events and patches the VM list cache in real time. */
export function useVMStatusSync() {
  const qc = useQueryClient();
  useEffect(() => {
    const ws = getTasksWebSocket();
    ws.connect();
    const unsubStatus = ws.onType('vm_status_update', (data: unknown) => {
      const msg = data as { vmid: number; server_id: number; status: string };
      qc.setQueryData<VMInstance[]>(vmKeys.all, (old) =>
        old?.map((vm) =>
          vm.server_id === msg.server_id && vm.vmid === msg.vmid
            ? { ...vm, status: msg.status as VMInstance['status'] }
            : vm
        )
      );
    });
    const unsubDeleted = ws.onType('vm_deleted', (data: unknown) => {
      const msg = data as { vmid: number; server_id: number };
      qc.setQueryData<VMInstance[]>(vmKeys.all, (old) =>
        old?.filter((vm) => !(vm.server_id === msg.server_id && vm.vmid === msg.vmid))
      );
      // Also invalidate any per-VM detail queries so stale pages refresh
      qc.invalidateQueries({ queryKey: vmKeys.resourcesAll });
    });
    const unsubCreated = ws.onType('vm_created', () => {
      // A new VM appeared on Proxmox; refetch the full list
      qc.invalidateQueries({ queryKey: vmKeys.all });
      qc.invalidateQueries({ queryKey: vmKeys.resourcesAll });
    });
    return () => {
      unsubStatus();
      unsubDeleted();
      unsubCreated();
    };
  }, [qc]);
}

export type LiveInstanceMetrics = {
  server_id: number; vmid: number; node?: string; status?: string; type?: string;
  cpu?: number; mem?: number; maxmem?: number; disk?: number; maxdisk?: number;
  uptime?: number; netin?: number; netout?: number;
  diskread?: number; diskwrite?: number;
  diskread_rate?: number; diskwrite_rate?: number;
  netin_rate?: number; netout_rate?: number;
};

/**
 * Subscribes to live VM/LXC metrics via WebSocket channel `instances_list_metrics`.
 * Returns a Map keyed by `"server_id:vmid"` that updates every ~1 s.
 * Uses its own useState — bypasses TanStack Query structural sharing entirely.
 */
export function useInstancesMetricsSync(): Map<string, LiveInstanceMetrics> {
  const [metricsMap, setMetricsMap] = useState(() => new Map<string, LiveInstanceMetrics>());
  useEffect(() => {
    const ws = getTasksWebSocket();
    ws.connect();
    const apply = (data: unknown) => {
      const items = (data as { data?: { items?: LiveInstanceMetrics[] } })?.data?.items;
      if (!items?.length) return;
      const next = new Map<string, LiveInstanceMetrics>();
      for (const it of items) next.set(`${it.server_id}:${it.vmid}`, it);
      setMetricsMap(next);
    };
    const unsub = ws.subscribe('instances_list_metrics', apply);
    return () => unsub();
  }, []);
  return metricsMap;
}

export function useAllResources() {
  return useQuery({
    queryKey: vmKeys.resourcesAll,
    queryFn: () => apiClient.get<Record<string, unknown>>('/proxmox/api/resources/all'),
    refetchInterval: 30000,
  });
}

// ==================== VM Detail ====================
interface VMStatusResponse {
  status: string;
  cpu: number;
  mem: number;
  maxmem: number;
  disk: number;
  maxdisk: number;
  uptime: number;
  netin: number;
  netout: number;
  name?: string;
  pid?: number;
  [key: string]: unknown;
}

export interface VMLiveStatus extends VMStatusResponse {
  diskread_rate?: number;
  diskwrite_rate?: number;
  netin_rate?: number;
  netout_rate?: number;
  disks?: DiskInfo[];
}

export function useVMStatus(serverId: number, vmid: number, type: string, node: string, enabled = true) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useQuery<VMStatusResponse>({
    queryKey: vmKeys.status(serverId, vmid),
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/${prefix}/${vmid}/status?node=${node}`),
    refetchInterval: 10000,
    enabled,
  });
}

/**
 * Subscribes to the live per-instance WebSocket channel and returns real-time
 * VM/LXC metrics including I/O rates and per-disk info.
 * Falls back to the HTTP-polled status until the first WS message arrives.
 */
export function useVMLiveMetrics(
  serverId: number,
  vmid: number,
  type: string,
  node: string,
): VMLiveStatus | null {
  const channel = `instance_metrics:${serverId}_${vmid}_${type}_${node}`;
  // Tagged with its channel: the detail page stays mounted when the route
  // switches to another instance, and an untagged sample kept showing the
  // previous instance's metrics until the new channel's first message.
  const [live, setLive] = useState<{ channel: string; data: VMLiveStatus } | null>(null);
  const { data: http } = useVMStatus(serverId, vmid, type, node);

  useEffect(() => {
    const ws = getTasksWebSocket();
    ws.connect();
    const unsub = ws.subscribe(channel, (msg: unknown) => {
      const payload = msg as { data?: VMLiveStatus };
      if (payload?.data) setLive({ channel, data: payload.data });
    });
    return () => unsub();
  }, [channel]);

  return (live?.channel === channel ? live.data : null) ?? (http as VMLiveStatus) ?? null;
}

export function useVMConfig(serverId: number, vmid: number, type: string, node: string, enabled = true) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useQuery<VMConfig>({
    queryKey: vmKeys.config(serverId, vmid),
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/${prefix}/${vmid}/config?node=${node}`),
    enabled,
  });
}


export interface MetricPoint {
  time: number;
  cpu: number | null; mem: number | null; maxmem: number | null;
  netin: number | null; netout: number | null;
  diskread: number | null; diskwrite: number | null;
  diskpct: number | null; iops_read: number | null; iops_write: number | null;
}
export interface MetricsResponse {
  data: MetricPoint[]; from: number; to: number; meta: { nics: string[] };
}

export function useVMMetrics(
  serverId: number, vmid: number, type: string, node: string,
  opts: { timeframe?: string; from?: number; to?: number; nic?: string },
) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  const { timeframe = 'hour', from, to, nic = 'all' } = opts;
  return useQuery<MetricsResponse>({
    queryKey: ['vm', serverId, vmid, 'metrics', { timeframe, from, to, nic }],
    queryFn: () => {
      const qs = new URLSearchParams({ node, nic });
      if (from != null && to != null) {
        qs.set('from_ts', String(from));
        qs.set('to_ts', String(to));
      } else {
        qs.set('timeframe', timeframe);
      }
      return apiClient.get(`/proxmox/api/${serverId}/${prefix}/${vmid}/metrics?${qs}`);
    },
    // Keep showing the previous series while a new range/NIC loads, so switching
    // the interface selector doesn't blank the whole grid into a spinner.
    placeholderData: keepPreviousData,
  });
}

interface InterfaceInfo {
  name: string;
  mac?: string;
  ips?: Array<{ address: string; prefix: number; type: string }>;
}

export function useVMInterfaces(serverId: number, vmid: number, type: string, node: string, enabled = true) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useQuery<{ interfaces: InterfaceInfo[] }>({
    queryKey: vmKeys.interfaces(serverId, vmid),
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/${prefix}/${vmid}/interfaces?node=${node}`),
    enabled,
    retry: 1,
  });
}

// ==================== Snapshots ====================
export function useSnapshots(serverId: number, vmid: number, type: string, node: string, enabled = true) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useQuery<{ snapshots: Snapshot[] }>({
    queryKey: vmKeys.snapshots(serverId, vmid),
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/${prefix}/${vmid}/snapshots?node=${node}`),
    enabled,
  });
}

export function useCreateSnapshot(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: (body: { snapname: string; description?: string; vmstate?: boolean }) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/snapshots?node=${node}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.snapshots(serverId, vmid) });
    },
  });
}

export function useDeleteSnapshot(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: (snapname: string) =>
      apiClient.delete(`/proxmox/api/${serverId}/${prefix}/${vmid}/snapshots/${snapname}?node=${node}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.snapshots(serverId, vmid) });
    },
  });
}

export function useRollbackSnapshot(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: (snapname: string) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/snapshots/${snapname}/rollback?node=${node}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.snapshots(serverId, vmid) });
      qc.invalidateQueries({ queryKey: vmKeys.status(serverId, vmid) });
    },
  });
}

// ==================== Owner (admin) ====================

export interface VMOwnerInfo {
  owner_id: number | null;
  owner: { id: number; username: string; full_name?: string } | null;
  users: { id: number; username: string; full_name?: string }[];
}

export function useVMOwner(serverId: number, vmid: number, enabled = true) {
  return useQuery<VMOwnerInfo>({
    queryKey: ['vm-owner', serverId, vmid],
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/vm/${vmid}/owner`),
    enabled: enabled && serverId > 0 && vmid > 0,
    retry: false,
  });
}

export function useSetVMOwner(serverId: number, vmid: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (user_id: number | null) =>
      apiClient.put(`/proxmox/api/${serverId}/vm/${vmid}/owner`, { user_id }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vm-owner', serverId, vmid] });
      // Refresh the instances list so the owner column updates without a manual reload.
      qc.invalidateQueries({ queryKey: vmKeys.all });
    },
  });
}

// ==================== Snapshot archives (admin audit) ====================

export interface SnapshotArchive {
  id: number;
  server_id: number;
  server_name?: string;
  vmid: number;
  vm_name?: string;
  vm_type?: string;
  node?: string;
  snapname: string;
  description?: string;
  snaptime?: number;
  parent?: string;
  vmstate?: number;
  snapshot_config?: string;
  deleted_by?: string;
  deletion_reason?: string;
  archived_at?: string;
}

export function useSnapshotArchives(params?: { server_id?: number; vmid?: number; limit?: number; offset?: number }) {
  const qs = new URLSearchParams();
  if (params?.server_id) qs.set('server_id', String(params.server_id));
  if (params?.vmid) qs.set('vmid', String(params.vmid));
  if (params?.limit) qs.set('limit', String(params.limit));
  if (params?.offset) qs.set('offset', String(params.offset));
  return useQuery<{ total: number; offset: number; limit: number; archives: SnapshotArchive[] }>({
    queryKey: ['snapshot-archives', params],
    queryFn: () => apiClient.get(`/proxmox/api/snapshot-archives?${qs}`),
  });
}

export function useSnapshotArchiveDetail(archiveId: number | null) {
  return useQuery<SnapshotArchive>({
    queryKey: ['snapshot-archive', archiveId],
    queryFn: () => apiClient.get(`/proxmox/api/snapshot-archives/${archiveId}`),
    enabled: !!archiveId,
  });
}

// ==================== Saved config (DB) ====================

export interface SavedVMConfig {
  found: boolean;
  config?: {
    cores?: number; memory?: number; disk_size?: number;
    ip_address?: string; ip_prefix?: number; gateway?: string; nameserver?: string;
    cloud_init_user?: string; cloud_init_password?: string; ssh_keys?: string;
    name?: string; template_id?: number;
  };
}

export function useSavedConfig(serverId: number, vmid: number, enabled = true) {
  return useQuery<SavedVMConfig>({
    queryKey: ['vm-saved-config', serverId, vmid],
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/vm/${vmid}/saved-config`),
    enabled: enabled && serverId > 0 && vmid > 0,
  });
}

// ==================== Execute script (guest agent) ====================

export interface ExecuteScriptResult {
  success?: boolean;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  error?: string;
  [k: string]: unknown;
}

export function useExecuteScript(serverId: number, vmid: number) {
  return useMutation({
    // Endpoint takes script/interpreter as multipart form fields and node/timeout as query params,
    // so we bypass the JSON apiClient and post FormData directly.
    mutationFn: async ({ script, interpreter, node, timeout }: { script: string; interpreter: string; node: string; timeout: number }) => {
      const fd = new FormData();
      fd.set('script', script);
      fd.set('interpreter', interpreter);
      const qs = new URLSearchParams({ node, timeout: String(timeout) });
      // postForm, not a bare fetch: it adds X-Active-Workspace, handles an
      // expired session (401) and turns 422 detail arrays into readable text.
      return apiClient.postForm<ExecuteScriptResult>(`/proxmox/api/${serverId}/vm/${vmid}/execute-script?${qs}`, fd);
    },
  });
}

// ==================== Power Actions ====================
// reset/resume leave the guest running; treating them as "stopped" flashed the
// row as stopped and kept polling for a state that never came.
const RUNNING_AFTER = new Set(['start', 'restart', 'reboot', 'reset', 'resume']);
const expectedStatusAfter = (action: string) => (RUNNING_AFTER.has(action) ? 'running' : 'stopped');

interface PowerActionResult {
  status: string;
  action: string;
  vmid: number;
  node: string;
  upid?: string;
}

export function usePowerAction(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation<PowerActionResult, Error, { action: string; force?: boolean }, { previousVMs: VMInstance[] | undefined }>({
    mutationFn: ({ action, force }) => {
      const params = new URLSearchParams({ node });
      if (force) params.set('force', '1');
      return apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/${action}?${params}`);
    },
    onMutate: async ({ action }) => {
      await qc.cancelQueries({ queryKey: vmKeys.all });
      const previousVMs = qc.getQueryData<VMInstance[]>(vmKeys.all);
      const expectedStatus = expectedStatusAfter(action);
      qc.setQueryData<VMInstance[]>(vmKeys.all, (old) =>
        old?.map((vm) =>
          vm.server_id === serverId && vm.vmid === vmid
            ? { ...vm, status: expectedStatus as VMInstance['status'] }
            : vm
        )
      );
      return { previousVMs };
    },
    onError: (_err, _vars, context) => {
      if (context?.previousVMs) {
        qc.setQueryData(vmKeys.all, context.previousVMs);
      }
    },
    onSuccess: (_data, { action }) => {
      const expectedStatus = expectedStatusAfter(action);
      let attempts = 0;
      const maxAttempts = 12; // 12 * 3s = 36s max
      const poll = () => {
        attempts++;
        apiClient
          .get<VMStatusResponse>(`/proxmox/api/${serverId}/${prefix}/${vmid}/status?node=${node}`)
          .then((liveStatus) => {
            qc.setQueryData<VMInstance[]>(vmKeys.all, (old) =>
              old?.map((vm) =>
                vm.server_id === serverId && vm.vmid === vmid
                  ? { ...vm, status: liveStatus.status as VMInstance['status'] }
                  : vm
              )
            );
            if (liveStatus.status !== expectedStatus && attempts < maxAttempts) {
              setTimeout(poll, 3000);
            }
          })
          .catch(() => {
            if (attempts < maxAttempts) setTimeout(poll, 3000);
          });
      };
      setTimeout(poll, 3000);
    },
  });
}

// ==================== Delete VM ====================
export function useDeleteVM(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: () => apiClient.delete(`/proxmox/api/${serverId}/${prefix}/${vmid}?node=${node}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.all });
      qc.invalidateQueries({ queryKey: vmKeys.resourcesAll });
    },
  });
}

// ==================== Bulk Operations ====================
interface BulkItem {
  server_id: number;
  vmid: number;
  vm_type: string;
  name: string;
  node: string;
  target_node?: string;  // для action='migrate'
  online?: boolean;
}

interface BulkOperationResponse {
  success: boolean;
  task_id: number;
  message: string;
}

export function useBulkOperation() {
  const qc = useQueryClient();
  return useMutation<BulkOperationResponse, Error, { action: string; items: BulkItem[] }>({
    mutationFn: (body) =>
      apiClient.post('/proxmox/api/bulk-operation', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.all });
      qc.invalidateQueries({ queryKey: vmKeys.resourcesAll });
    },
  });
}

/**
 * Subscribes to WebSocket `task_update` events for bulk operations and patches
 * the bulk-tasks store with live progress (completed/failed/results), so the
 * instances list can show per-row status overlays and a progress bar — the same
 * way deploy tasks surface instance creation. Only tasks started in this session
 * (registered via the store's addTask) are tracked. When a task finishes it
 * refetches the VM list and auto-clears after a short delay.
 */
export function useBulkTasksSync() {
  const qc = useQueryClient();
  const updateTask = useBulkTasksStore((s) => s.updateTask);
  const removeTask = useBulkTasksStore((s) => s.removeTask);
  useEffect(() => {
    const ws = getTasksWebSocket();
    ws.connect();
    const unsub = ws.onType('task_update', (data: unknown) => {
      const task = (data as { task?: {
        kind?: string; id: number; status: string;
        total_items: number; completed_items: number; failed_items: number;
        results?: BulkTaskResult[];
      } }).task;
      if (!task || task.kind !== 'bulk') return;
      updateTask(task.id, {
        status: task.status as BulkTask['status'],
        total: task.total_items,
        completed: task.completed_items,
        failed: task.failed_items,
        results: task.results ?? [],
      });
      if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
        qc.invalidateQueries({ queryKey: vmKeys.all });
        qc.invalidateQueries({ queryKey: vmKeys.resourcesAll });
        setTimeout(() => removeTask(task.id), 4000);
      }
    });
    return () => unsub();
  }, [qc, updateTask, removeTask]);
}

// ==================== Disk Resize ====================
export function useResizeDisk(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: (body: { disk: string; size: string }) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/disk/resize?node=${node}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) });
      qc.invalidateQueries({ queryKey: vmKeys.status(serverId, vmid) });
    },
  });
}

export function useUnlockInstance(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: () => apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/unlock?node=${node}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) });
      qc.invalidateQueries({ queryKey: vmKeys.status(serverId, vmid) });
    },
  });
}

export interface CloudInitInput {
  ciuser?: string;
  cipassword?: string;
  sshkeys?: string;
  ipconfig0?: string;
  nameserver?: string;
  searchdomain?: string;
}

export function useUpdateCloudInit(serverId: number, vmid: number, node: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CloudInitInput) =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/cloud-init?node=${node}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) }),
  });
}

export function useMoveDisk(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: (body: { disk: string; target_storage: string; delete?: boolean; format?: string }) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/disk/move?node=${node}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) }),
  });
}

export function useAddDisk(serverId: number, vmid: number, node: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { disk: string; storage: string; size_gb: number; ssd?: boolean; discard?: boolean; iothread?: boolean }) =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/disk/add?node=${node}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) }),
  });
}

export function useAttachPhysicalDisk(serverId: number, vmid: number, node: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { disk: string; devpath: string; aio?: string; discard?: boolean; ssd?: boolean; serial?: string }) =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/disk/attach-physical?node=${node}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) }),
  });
}

export function useDetachDisk(serverId: number, vmid: number, node: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { disk: string; destroy?: boolean }) =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/disk/detach?node=${node}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) }),
  });
}

// ==================== Config Update ====================
export function useUpdateConfig(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiClient.put(`/proxmox/api/${serverId}/${prefix}/${vmid}/config?node=${node}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) });
    },
  });
}

// ==================== Clone / Reinstall ====================
interface CloneRequest {
  new_name: string;
  full?: boolean;
  target_node?: string;
  target_storage?: string;
  description?: string;
  owner_id?: number;
}

export interface AsyncTaskResponse {
  task_id: number;
  status: string;
  name: string;
}

export function useCloneInstance(serverId: number, vmid: number, type: string, node: string) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation<AsyncTaskResponse, Error, CloneRequest>({
    mutationFn: (body) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/clone?node=${node}`, body),
  });
}

export function useReinstallInstance(serverId: number, vmid: number, node: string) {
  return useMutation<AsyncTaskResponse, Error, void>({
    mutationFn: () =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/reinstall?node=${node}`),
  });
}

// ==================== Migrate ====================
export interface MigrateRequest {
  target_node: string;
  target_storage?: string;
  online?: boolean;
}

export function useMigrateInstance(serverId: number, vmid: number, type: string, node: string) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation<AsyncTaskResponse, Error, MigrateRequest>({
    mutationFn: (body) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/migrate?node=${node}`, body),
  });
}

// ==================== Remote migrate (to a different cluster) ====================
export interface RemoteMigrateRequest {
  target_server_id: number;
  target_node: string;
  target_vmid?: number;
  target_storage?: string;
  target_bridge?: string;
  online?: boolean;
  delete_source?: boolean;
}

export function useRemoteMigrateInstance(serverId: number, vmid: number, type: string, node: string) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation<AsyncTaskResponse, Error, RemoteMigrateRequest>({
    mutationFn: (body) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/remote-migrate?node=${node}`, body),
  });
}

// ==================== Change Password ====================
export function useChangePassword(serverId: number, vmid: number, type: string, node: string) {
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation<AsyncTaskResponse, Error, { username: string; password: string }>({
    mutationFn: (body) =>
      apiClient.post(`/proxmox/api/${serverId}/${prefix}/${vmid}/change-password?node=${node}`, body),
  });
}

// ==================== Notes ====================
export function useUpdateNotes(serverId: number, vmid: number, type: string, node: string) {
  const qc = useQueryClient();
  const prefix = type === 'lxc' ? 'container' : 'vm';
  return useMutation<{ status: string }, Error, { description: string }>({
    mutationFn: (body) =>
      apiClient.put(`/proxmox/api/${serverId}/${prefix}/${vmid}/notes?node=${node}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) });
      qc.invalidateQueries({ queryKey: vmKeys.all });
    },
  });
}

// ==================== Execute Command (qemu-agent) ====================
interface ExecResult {
  success?: boolean;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  error?: string;
}

export function useExecuteCommand(serverId: number, vmid: number) {
  return useMutation<ExecResult, Error, { node: string; command: string; timeout?: number }>({
    mutationFn: ({ node, command, timeout = 30 }) => {
      const params = new URLSearchParams({ node, command, timeout: String(timeout) });
      return apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/execute?${params}`);
    },
  });
}

// ==================== Node networks (bridges) ====================
export interface NodeNetworkIface {
  iface?: string;
  name?: string;
  type?: string;
  active?: number;
  cidr?: string;
  comments?: string;
  ipam_network_id?: number;
  ipam_cidr?: string;
  ipam_name?: string;
  zone?: string;
}

export function useNodeNetworks(serverId: number, node: string, enabled = true) {
  return useQuery<{ node: string; interfaces: NodeNetworkIface[]; sdn_vnets?: NodeNetworkIface[] }>({
    queryKey: ['node-networks', serverId, node],
    queryFn: () => apiClient.get(`/proxmox/api/servers/${serverId}/nodes/${node}/networks`),
    enabled: enabled && !!node,
  });
}

// ==================== Node hardware (PCI/USB passthrough) ====================
export interface NodePciDevice {
  id: string;               // 0000:01:00.0
  vendor_name?: string;
  device_name?: string;
  subsystem_device_name?: string;
  iommugroup?: number;
  mdev?: number | boolean;
}

export interface NodeUsbDevice {
  busnum?: number | string;
  devnum?: number | string;
  vendid?: string;
  prodid?: string;
  port?: number | string;
  usbpath?: string;
  speed?: string;
  manufacturer?: string;
  prodname?: string;
}

export function useNodePciDevices(serverId: number, node: string, enabled = true) {
  return useQuery<NodePciDevice[]>({
    queryKey: ['node-pci', serverId, node],
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/nodes/${node}/hardware/pci`),
    enabled: enabled && !!node,
  });
}

export function useNodeUsbDevices(serverId: number, node: string, enabled = true) {
  return useQuery<NodeUsbDevice[]>({
    queryKey: ['node-usb', serverId, node],
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/nodes/${node}/hardware/usb`),
    enabled: enabled && !!node,
  });
}

// ==================== ISO mount/unmount (KVM only) ====================
export interface IsoItem {
  volid: string;
  storage: string;
  size?: number;
  format?: string;
  name?: string;
}

export function useNodeIsos(serverId: number, node: string, enabled = true) {
  return useQuery<{ isos: IsoItem[] }>({
    queryKey: ['isos', serverId, node],
    queryFn: () => apiClient.get(`/proxmox/api/${serverId}/node/${node}/isos`),
    enabled: enabled && !!node,
  });
}

export function useAttachIso(serverId: number, vmid: number, node: string) {
  const qc = useQueryClient();
  return useMutation<
    { status: string; boot_from_iso?: boolean; rebooted?: boolean },
    Error,
    { volid: string; device?: string; boot_from_iso?: boolean; reboot_after?: boolean }
  >({
    mutationFn: (body) =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/iso/attach?node=${node}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) });
      qc.invalidateQueries({ queryKey: vmKeys.status(serverId, vmid) });
      qc.invalidateQueries({ queryKey: ['resources'] });
    },
  });
}

export function useDetachIso(serverId: number, vmid: number, node: string) {
  const qc = useQueryClient();
  return useMutation<
    { status: string; boot_from_disk?: boolean; rebooted?: boolean },
    Error,
    { device?: string; boot_from_disk?: boolean; reboot_after?: boolean }
  >({
    mutationFn: (body) =>
      apiClient.post(`/proxmox/api/${serverId}/vm/${vmid}/iso/detach?node=${node}`, body || {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.config(serverId, vmid) });
      qc.invalidateQueries({ queryKey: vmKeys.status(serverId, vmid) });
      qc.invalidateQueries({ queryKey: ['resources'] });
    },
  });
}

// ==================== Пустая ВМ под установку с ISO ====================
export interface IsoVMCreateRequest {
  server_id: number;
  node: string;
  name: string;
  iso: string;
  vmid?: number;
  cores: number;
  memory: number;
  disk: number;
  storage: string;
  bridge: string;
  ostype: string;
  disk_bus: 'scsi' | 'sata' | 'virtio';
  net_model: 'virtio' | 'e1000' | 'rtl8139' | 'vmxnet3';
  bios: 'seabios' | 'ovmf';
  tpm: boolean;
  extra_iso?: string;
  ip_address?: string;
  gateway?: string;
  ipam_network_id?: number;
  owner_id?: number;
  start_after_create: boolean;
  onboot: boolean;
}

export function useCreateIsoVM() {
  const qc = useQueryClient();
  return useMutation<{ task_id: number; status: string; name: string }, Error, IsoVMCreateRequest>({
    mutationFn: (body) => apiClient.post('/proxmox/api/vm/create-from-iso', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: vmKeys.all });
    },
  });
}
