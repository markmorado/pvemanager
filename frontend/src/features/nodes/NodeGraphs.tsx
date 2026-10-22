import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useNodeMetrics } from '@/hooks/use-nodes';
import { formatBytes } from '@/lib/format';
import {
  ChartCard,
  TIMEFRAME_PRESETS,
  tickFormatterFor,
} from '@/components/shared/MetricChart';

interface Props {
  serverId: number;
  nodeNames: string[];
}

// PVE keeps a year of node RRD data, unlike the 30-day instance history.
const PRESETS = [...TIMEFRAME_PRESETS, { tf: 'year', label: '1y' }];

export default function NodeGraphs({ serverId, nodeNames }: Props) {
  const { t } = useTranslation();
  const [nodePick, setNode] = useState('');
  const [timeframe, setTimeframe] = useState('hour');

  // Nodes arrive asynchronously; use the first one until a node is picked, and
  // fall back to it if the picked node disappears from the cluster.
  const node = nodeNames.includes(nodePick) ? nodePick : (nodeNames[0] ?? '');

  const { data: points = [], isLoading } = useNodeMetrics(serverId, node, timeframe);
  const xTick = tickFormatterFor(timeframe);
  const rows = points as unknown as Array<Record<string, unknown>>;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {nodeNames.length > 1 && (
          <select
            value={node}
            onChange={(e) => setNode(e.target.value)}
            className="rounded border border-border bg-background px-2 py-1 text-sm text-foreground focus:outline-none"
          >
            {nodeNames.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        )}
        <div className="flex gap-1">
          {PRESETS.map((p) => (
            <Button
              key={p.tf}
              variant={timeframe === p.tf ? 'default' : 'outline'}
              size="sm"
              onClick={() => setTimeframe(p.tf)}
            >
              {p.label}
            </Button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : rows.length === 0 ? (
        <div className="flex items-center justify-center py-20 text-sm text-muted-foreground">
          {t('common.no_data')}
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.cpu_usage')}
            data={rows}
            dataKey="cpu"
            color="hsl(217, 91%, 60%)"
            formatValue={(v) => `${v.toFixed(1)}%`}
            unit="%"
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.io_delay', 'IO delay')}
            data={rows}
            dataKey="iowait"
            color="hsl(45, 93%, 47%)"
            formatValue={(v) => `${v.toFixed(2)}%`}
            unit="%"
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.memory_usage')}
            data={rows}
            dataKey="memused"
            color="hsl(142, 76%, 36%)"
            formatValue={(v) => formatBytes(v)}
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.load_average', 'Load average')}
            data={rows}
            dataKey="loadavg"
            color="hsl(0, 84%, 60%)"
            formatValue={(v) => v.toFixed(2)}
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.network_in')}
            data={rows}
            dataKey="netin"
            color="hsl(262, 83%, 58%)"
            formatValue={(v) => `${formatBytes(v)}/s`}
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.network_out')}
            data={rows}
            dataKey="netout"
            color="hsl(25, 95%, 53%)"
            formatValue={(v) => `${formatBytes(v)}/s`}
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.swap_usage', 'Swap')}
            data={rows}
            dataKey="swapused"
            color="hsl(300, 60%, 50%)"
            formatValue={(v) => formatBytes(v)}
          />
          <ChartCard
            tickFormatter={xTick}
            title={t('graphs.root_fs', 'Root filesystem')}
            data={rows}
            dataKey="rootused"
            color="hsl(180, 70%, 40%)"
            formatValue={(v) => formatBytes(v)}
          />
        </div>
      )}
    </div>
  );
}
