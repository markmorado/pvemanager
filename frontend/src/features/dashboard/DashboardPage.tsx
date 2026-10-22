import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, ChevronDown } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { apiClient } from '@/lib/api-client'
import { getTasksWebSocket } from '@/lib/websocket'
import { useInstancesMetricsSync } from '@/hooks/use-instances'
import { useAllTasks, useActiveTaskCount } from '@/hooks/use-tasks'
import { useIPAMNetworks } from '@/hooks/use-ipam'

// Theme-aware — pulls from the --chart-1..5 tokens in index.css so pie slices
// stay in sync with light/dark instead of a second, hardcoded hex palette.
const CHART_COLORS = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)']

type Rec = Record<string, unknown>

// ==================== Helpers ====================
function barColor(pct: number) {
  return pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warning' : 'bg-success'
}

// SVG stroke colour — reference theme tokens so light/dark stay in sync.
function gaugeStroke(pct: number) {
  return pct >= 90 ? 'var(--danger)' : pct >= 70 ? 'var(--warning)' : 'var(--success)'
}

// ==================== Resource Bar ====================
function ResourceBar({ label, percent }: { label: string; percent: number }) {
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono tabular-nums font-medium">{percent.toFixed(1)}%</span>
      </div>
      <div className="h-1.5 rounded-full bg-track overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${barColor(percent)}`}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
    </div>
  )
}

// Worst-case health across a set of percentages — drives status accent bars.
function healthColor(...percents: number[]) {
  const worst = Math.max(...percents)
  return worst >= 90 ? 'bg-danger' : worst >= 70 ? 'bg-warning' : 'bg-success'
}

// ==================== SVG Circular Gauge ====================
function CircularGauge({ label, value }: { label: string; value: number }) {
  const r = 26
  const circ = 2 * Math.PI * r
  const clamped = Math.min(Math.max(value, 0), 100)
  const offset = circ * (1 - clamped / 100)

  return (
    <div className="flex flex-col items-center gap-1.5">
      <div className="relative w-[68px] h-[68px]">
        <svg viewBox="0 0 72 72" className="w-full h-full -rotate-90">
          <circle cx="36" cy="36" r={r} fill="none" stroke="var(--track)" strokeWidth="7" />
          <circle
            cx="36" cy="36" r={r}
            fill="none" stroke={gaugeStroke(value)} strokeWidth="7"
            strokeLinecap="round"
            strokeDasharray={circ}
            strokeDashoffset={offset}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="font-mono text-sm font-bold tabular-nums">{Math.round(clamped)}</span>
        </div>
      </div>
      <span className="text-2xs font-mono text-muted-foreground tracking-wide uppercase">{label}</span>
    </div>
  )
}

// ==================== Cluster Overview Card ====================
function ClusterOverviewCard({ serverList, nodeStats, allVMs, memPercent, diskPercent, availableIPs }: {
  serverList: Rec[]; nodeStats: Rec[]; allVMs: Rec[]
  memPercent: number; diskPercent: number; availableIPs: number
}) {
  const { t } = useTranslation()
  const clusterName =
    (serverList[0]?.cluster_name as string) ||
    (serverList[0]?.server_name as string) ||
    t('dashboard.cluster')
  const totalVMs = allVMs.filter(v => v.type === 'qemu').length
  const totalCTs = allVMs.filter(v => v.type === 'lxc').length
  const totalNodeMem = nodeStats.reduce((s, n) => s + (Number(n.maxmem) || 0), 0)
  const totalVMMem = allVMs.reduce((s, v) => s + (Number(v.maxmem) || 0), 0)
  const overselling = (totalNodeMem > 0 ? totalVMMem / totalNodeMem : 1).toFixed(1)

  const cols = availableIPs > 0 ? 'grid-cols-3' : 'grid-cols-2'

  return (
    <Card className="h-full gap-0 py-0 overflow-hidden">
      <div className={`h-[3px] w-full ${healthColor(memPercent, diskPercent)}`} />
      <CardContent className="p-5 pt-4 flex flex-col gap-4 h-full">
        <div className="flex items-center gap-1.5">
          <button className="flex items-center gap-1.5 group text-left rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card">
            <span className="text-base font-semibold group-hover:text-primary transition-colors">{clusterName}</span>
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          </button>
        </div>

        <div className={`grid ${cols} gap-4`}>
          {availableIPs > 0 && (
            <div>
              <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground mb-1">{t('dashboard.ipv4_available')}</p>
              <p className="font-mono text-3xl font-bold tabular-nums text-success">{availableIPs.toLocaleString()}</p>
            </div>
          )}
          <div>
            <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground mb-1">{t('dashboard.nodes_count')}</p>
            <p className="font-mono text-3xl font-bold tabular-nums">{nodeStats.length}</p>
          </div>
          <div>
            <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground mb-1">VM</p>
            <p className="font-mono text-3xl font-bold tabular-nums">{totalVMs + totalCTs}</p>
          </div>
        </div>

        <div className="space-y-2.5 flex-1">
          <ResourceBar label="RAM" percent={memPercent} />
          <ResourceBar label="Storage" percent={diskPercent} />
        </div>

        <div className="flex items-center justify-between text-xs pt-1">
          <span className="text-muted-foreground">
            {t('dashboard.ram_oversell')} <span className="font-mono text-foreground font-medium">{overselling}</span>
          </span>
          <span className="flex items-center gap-1 text-muted-foreground">
            <ExternalLink className="h-3 w-3" /> Grafana
          </span>
        </div>
      </CardContent>
    </Card>
  )
}

// ==================== Tasks Today Card ====================
function TasksTodayCard() {
  const { t } = useTranslation()
  // Up to 500 rows from five tables just to count today's tasks: the live
  // "running" number comes from useActiveTaskCount, so refresh this slowly.
  const { data: tasksData } = useAllTasks({ limit: 500 }, { refetchInterval: 30_000 })
  const { data: activeData } = useActiveTaskCount()

  const startOfDay = new Date()
  startOfDay.setHours(0, 0, 0, 0)
  const startTs = startOfDay.getTime() / 1000

  const all = tasksData?.tasks ?? []
  const today = all.filter(t => {
    if (t.created_at) return new Date(t.created_at) >= startOfDay
    if (t.starttime != null) return Number(t.starttime) >= startTs
    return false
  })

  const completed = today.filter(t =>
    t.exitstatus === 'OK' || t.status === 'ok' || t.status === 'completed'
  ).length
  const running = activeData?.count ?? today.filter(t => t.started_at && !t.completed_at && !t.endtime).length
  const queued = today.filter(t => t.status === 'queued' || t.status === 'pending').length
  const errors = today.filter(t =>
    t.exitstatus === 'ERROR' || t.status === 'error' || t.status === 'failed'
  ).length

  const rows = [
    { label: t('dashboard.tasks_done'), value: completed, red: false },
    { label: t('dashboard.tasks_running'), value: running, red: false },
    { label: t('dashboard.tasks_queued'), value: queued, red: false },
    { label: t('dashboard.tasks_errors'), value: errors, red: errors > 0 },
  ]

  return (
    <Card className="h-full">
      <CardHeader className="pb-2 pt-5 px-5">
        <CardTitle className="text-sm font-semibold">{t('dashboard.tasks_today')}</CardTitle>
      </CardHeader>
      <CardContent className="px-5 pb-5 space-y-2.5">
        {rows.map(({ label, value, red }) => (
          <div key={label} className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">{label}</span>
            <span className={`font-mono text-sm font-semibold tabular-nums ${red ? 'text-danger' : ''}`}>{value}</span>
          </div>
        ))}
        <div className="border-t border-border pt-2.5 flex items-center justify-between">
          <span className="text-sm font-medium">{t('dashboard.tasks_total')}</span>
          <span className="font-mono text-sm font-bold tabular-nums">{today.length}</span>
        </div>
      </CardContent>
    </Card>
  )
}

// ==================== Node Stats Card ====================
function NodeStatsCard({ cpuPercent, memPercent, serverName, serverCount }: {
  cpuPercent: number; memPercent: number; serverName: string; serverCount: number
}) {
  const { t } = useTranslation()
  return (
    <Card className="h-full gap-0 py-0 overflow-hidden">
      <div className={`h-[3px] w-full ${healthColor(cpuPercent, memPercent)}`} />
      <CardHeader className="pb-2 pt-4 px-5">
        <CardTitle className="text-sm font-semibold truncate">{serverName || t('dashboard.server')}</CardTitle>
      </CardHeader>
      <CardContent className="px-5 pb-5 space-y-4">
        <div className="flex justify-around">
          <CircularGauge label="CPU, %" value={cpuPercent} />
          <CircularGauge label="RAM" value={memPercent} />
        </div>
        <div className="space-y-1.5 text-xs">
          <div className="flex justify-between">
            <span className="text-muted-foreground">{t('dashboard.servers_count')}:</span>
            <span className="font-mono font-medium">{serverCount}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">{t('dashboard.platform')}:</span>
            <span className="font-medium">Proxmox VE</span>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

// ==================== Cluster Nodes Section ====================
function ClusterNodesSection({ nodeStats }: { nodeStats: Rec[] }) {
  const { t } = useTranslation()
  const [tab, setTab] = useState<'now' | 'day'>('now')

  // Top-10 nodes by disk usage % — one donut slice per node.
  // Node name lives in `name` (dashboard_metrics WS payload) or `node`
  // (REST resources/all); fall back gracefully.
  const pieData = [...nodeStats]
    .map(n => ({
      name: String(n.node || n.name || ''),
      value: Number(n.disk_pct) || 0,
    }))
    .filter(d => d.name && d.value > 0)
    .sort((a, b) => b.value - a.value)
    .slice(0, 10)

  const sorted = [...nodeStats]
    .sort((a, b) =>
      tab === 'now'
        ? (Number(b.cpu_pct) || 0) - (Number(a.cpu_pct) || 0)
        : (Number(b.disk_pct) || 0) - (Number(a.disk_pct) || 0)
    )
    .slice(0, 10)

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
      {/* Node table */}
      <Card>
        <CardHeader className="pb-3 pt-4 px-5">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <CardTitle className="text-sm font-semibold">{t('dashboard.cluster_nodes')}</CardTitle>
            <div className="flex gap-1">
              {(['now', 'day'] as const).map(tab_key => (
                <button
                  key={tab_key}
                  onClick={() => setTab(tab_key)}
                  className={`rounded-full px-3 py-1 text-xs font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card ${
                    tab === tab_key
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-muted text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {tab_key === 'now' ? t('dashboard.top10_now') : t('dashboard.top10_day')}
                </button>
              ))}
            </div>
          </div>
        </CardHeader>
        <CardContent className="px-5 pb-4">
          <div className="grid grid-cols-[1fr_56px_56px_56px] gap-2 px-2 py-1 text-2xs font-semibold uppercase tracking-wider text-muted-foreground border-b border-border mb-1">
            <span>{t('dashboard.node_name')}</span>
            <span className="text-right">CPU, %</span>
            <span className="text-right">RAM</span>
            <span className="text-right">Disk</span>
          </div>
          {sorted.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data')}</p>
          ) : (
            sorted.map((n, i) => {
              const cpu = Number(n.cpu_pct) || 0
              const ram = Number(n.ram_pct) || 0
              const disk = Number(n.disk_pct) || 0
              const online = n.status !== 'offline'
              return (
                <div
                  key={i}
                  className="grid grid-cols-[1fr_56px_56px_56px] gap-2 items-center px-2 py-2 rounded hover:bg-muted/40 text-sm"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${online ? 'bg-success' : 'bg-danger'}`} />
                    <span className="truncate">{String(n.node || n.name || `node-${i + 1}`)}</span>
                    {!!n.server_name && (
                      <span className="text-2xs text-muted-foreground shrink-0">
                        #{String(n.server_name)}
                      </span>
                    )}
                  </div>
                  <span className={`font-mono text-right tabular-nums text-xs font-medium ${cpu >= 90 ? 'text-danger' : cpu >= 70 ? 'text-warning' : ''}`}>
                    {cpu.toFixed(0)}
                  </span>
                  <span className={`font-mono text-right tabular-nums text-xs ${ram >= 90 ? 'text-danger' : ram >= 70 ? 'text-warning' : ''}`}>
                    {ram.toFixed(0)}
                  </span>
                  <span className="font-mono text-right tabular-nums text-xs text-muted-foreground">{disk.toFixed(0)}</span>
                </div>
              )
            })
          )}
        </CardContent>
      </Card>

      {/* Disk stats — donut, one slice per node by disk usage % */}
      <Card>
        <CardHeader className="pb-2 pt-4 px-5">
          <CardTitle className="text-sm font-semibold">{t('dashboard.disk_top')}</CardTitle>
        </CardHeader>
        <CardContent className="px-3 pb-4">
          {pieData.length === 0 ? (
            <div className="h-52 flex items-center justify-center text-sm text-muted-foreground">
              {t('common.no_data')}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <PieChart>
                <Pie
                  data={pieData}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  innerRadius={55}
                  outerRadius={90}
                  paddingAngle={2}
                  strokeWidth={0}
                >
                  {pieData.map((_, j) => (
                    <Cell key={j} fill={CHART_COLORS[j % CHART_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  // Theme vars are oklch(); wrapping them in hsl() yields an
                  // invalid (transparent) colour, so reference them directly.
                  contentStyle={{
                    background: 'var(--popover)',
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    fontSize: 11,
                    color: 'var(--popover-foreground)',
                  }}
                  itemStyle={{ color: 'var(--popover-foreground)' }}
                  formatter={(v, name) => [`${v}%`, name]}
                />
                <Legend
                  formatter={(value) => <span style={{ fontSize: 11 }}>{value}</span>}
                  iconType="circle"
                  iconSize={8}
                />
              </PieChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ==================== Dashboard Page ====================
export default function DashboardPage() {
  const qc = useQueryClient()
  const liveMetrics = useInstancesMetricsSync()
  const { data: ipamNetworks } = useIPAMNetworks()

  const { data: resources } = useQuery({
    queryKey: ['resources'],
    queryFn: async () => {
      const fresh = await apiClient.get<Rec>('/proxmox/api/resources/all')
      // The REST endpoint returns VMs/containers but NOT per-node stats — those
      // arrive only via the dashboard_metrics WebSocket. Graft the last known
      // nodes so this 15 s refetch doesn't blank the node table each cycle.
      const prev = qc.getQueryData<Rec>(['resources'])
      const prevServers = (prev?.servers as Rec[]) ?? []
      if (prevServers.length) {
        const nodesById = new Map(prevServers.map(s => [s.id, s.nodes]))
        const servers = ((fresh.servers as Rec[]) ?? []).map(s =>
          (s.nodes as Rec[])?.length ? s : { ...s, nodes: nodesById.get(s.id) ?? [] }
        )
        return { ...fresh, servers }
      }
      return fresh
    },
    refetchInterval: 15000,
  })

  useEffect(() => {
    const ws = getTasksWebSocket()
    if (!ws.isConnected) ws.connect()
    const unsub = ws.subscribe('dashboard_metrics', (data: unknown) => {
      const payload = data as { data?: Rec }
      const next = payload?.data
      if (!next) return
      const incoming = (next.servers as Rec[]) ?? []
      // Ignore ticks where every server failed to fetch — a transient Proxmox
      // timeout must not wipe the dashboard back to zero and then restore it.
      if (incoming.length === 0) return
      qc.setQueryData(['resources'], (prev: Rec | undefined) => {
        if (!prev) return next
        const prevServers = (prev.servers as Rec[]) ?? []
        const byId = new Map(prevServers.map(s => [s.id, s]))
        // Merge per server: when an incoming server came back degraded (empty
        // nodes/vms/containers due to a partial failure), keep the last good
        // values instead of flashing them to empty.
        const merged = incoming.map(s => {
          const old = byId.get(s.id)
          if (!old) return s
          return {
            ...old,
            ...s,
            nodes: (s.nodes as Rec[])?.length ? s.nodes : old.nodes,
            vms: (s.vms as Rec[])?.length ? s.vms : old.vms,
            containers: (s.containers as Rec[])?.length ? s.containers : old.containers,
          }
        })
        return { ...prev, ...next, servers: merged }
      })
    })
    return unsub
  }, [qc])

  const serverList = ((resources as Rec)?.servers as Rec[]) ?? []

  const allVMs = serverList.flatMap(s =>
    [...((s.vms as Rec[]) ?? []), ...((s.containers as Rec[]) ?? [])].map(v => {
      const live = liveMetrics.get(`${s.id}:${v.vmid}`)
      return live
        ? { ...v, cpu: live.cpu ?? v.cpu, mem: live.mem ?? v.mem, status: live.status ?? v.status }
        : v
    })
  )

  const nodeStats = serverList.flatMap(s =>
    ((s.nodes as Rec[]) ?? []).map((n): Rec => ({
      ...n,
      server_name: s.server_name,
      server_id: s.id,
    }))
  )

  let cpuPercent = 0, memPercent = 0, diskPercent = 0
  if (nodeStats.length > 0) {
    cpuPercent = nodeStats.reduce((s, n) => s + (Number(n.cpu_pct) || 0), 0) / nodeStats.length
    memPercent = nodeStats.reduce((s, n) => s + (Number(n.ram_pct) || 0), 0) / nodeStats.length
    diskPercent = nodeStats.reduce((s, n) => s + (Number(n.disk_pct) || 0), 0) / nodeStats.length
  } else {
    const tm = allVMs.reduce((s, v) => s + (Number(v.maxmem) || 0), 0)
    const um = allVMs.reduce((s, v) => s + (Number(v.mem) || 0), 0)
    memPercent = tm > 0 ? (um / tm) * 100 : 0
    const td = allVMs.reduce((s, v) => s + (Number(v.maxdisk) || 0), 0)
    const ud = allVMs.reduce((s, v) => s + (Number(v.disk) || 0), 0)
    diskPercent = td > 0 ? (ud / td) * 100 : 0
    const tc = allVMs.reduce((s, v) => s + (Number(v.maxcpu) || 0), 0)
    const uc = allVMs.reduce((s, v) => s + (Number(v.cpu) || 0) * (Number(v.maxcpu) || 0), 0)
    cpuPercent = tc > 0 ? (uc / tc) * 100 : 0
  }

  const availableIPs = (ipamNetworks ?? []).reduce((s, n) => s + (n.available_ips ?? 0), 0)
  const serverName =
    (serverList[0]?.cluster_name as string) ||
    (serverList[0]?.server_name as string) ||
    ''

  return (
    <div className="space-y-4">
      {/* Top row: cluster overview | tasks | node stats */}
      <div className="grid gap-4 lg:grid-cols-[1fr_220px_210px] animate-fade-up">
        <ClusterOverviewCard
          serverList={serverList}
          nodeStats={nodeStats}
          allVMs={allVMs}
          memPercent={memPercent}
          diskPercent={diskPercent}
          availableIPs={availableIPs}
        />
        <TasksTodayCard />
        <NodeStatsCard
          cpuPercent={cpuPercent}
          memPercent={memPercent}
          serverName={serverName}
          serverCount={serverList.length}
        />
      </div>

      {/* Bottom row: nodes table + disk chart */}
      <div className="animate-fade-up" style={{ animationDelay: '80ms' }}>
        <ClusterNodesSection nodeStats={nodeStats} />
      </div>
    </div>
  )
}
