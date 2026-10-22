import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Trash2, X, ClipboardList, RefreshCw, Loader2, FileText } from 'lucide-react';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useAllTasks, useCancelTask, useClearCompletedTasks, useTaskLog, useTaskStatus, type TaskItem } from '@/hooks/use-tasks';

export default function TasksPage() {
  const { t } = useTranslation();
  const [statusFilter, setStatusFilter] = useState('all');
  const { data: tasksData, refetch, isLoading } = useAllTasks();
  const tasks = tasksData?.tasks ?? [];
  const cancelTask = useCancelTask();
  const clearCompleted = useClearCompletedTasks();
  const [logTask, setLogTask] = useState<TaskItem | null>(null);

  const filtered = tasks.filter(task => {
    if (statusFilter === 'all') return true;
    return task.status === statusFilter;
  });

  const statusColor = (status: string) => {
    switch (status) {
      case 'running': case 'in_progress': return 'default';
      case 'completed': case 'success': return 'secondary';
      case 'failed': case 'error': return 'destructive';
      case 'queued': case 'pending': return 'outline';
      default: return 'outline';
    }
  };

  const isActive = (status: string) =>
    status === 'running' || status === 'queued' || status === 'in_progress' || status === 'pending';

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-6 w-6" />
          <h1 className="text-2xl font-bold">{t('nav.tasks')}</h1>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isLoading}>
            <RefreshCw className={`h-4 w-4 mr-1 ${isLoading ? 'animate-spin' : ''}`} />{t('common.refresh')}
          </Button>
          <Button variant="outline" size="sm" onClick={() => clearCompleted.mutate()}>
            <Trash2 className="h-4 w-4 mr-1" />{t('tasks.clear_completed')}
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <Select value={statusFilter} onValueChange={v => { if (v !== null) setStatusFilter(v); }}>
              <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('common.all')}</SelectItem>
                <SelectItem value="running">{t('tasks.running')}</SelectItem>
                <SelectItem value="queued">{t('tasks.queued')}</SelectItem>
                <SelectItem value="completed">{t('tasks.completed')}</SelectItem>
                <SelectItem value="failed">{t('tasks.failed')}</SelectItem>
              </SelectContent>
            </Select>
            <span className="text-sm text-muted-foreground">{filtered.length} {t('tasks.tasks_count')}</span>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[110px]">{t('common.status')}</TableHead>
                <TableHead className="w-[110px]">{t('common.type')}</TableHead>
                <TableHead>{t('tasks.description')}</TableHead>
                <TableHead className="w-[200px]">{t('tasks.step_message')}</TableHead>
                <TableHead className="w-[100px]">{t('tasks.server')}</TableHead>
                <TableHead className="w-[90px]">{t('tasks.node')}</TableHead>
                <TableHead className="w-[80px]">{t('common.user')}</TableHead>
                <TableHead className="w-[140px]">{t('tasks.started')}</TableHead>
                <TableHead className="w-[120px]">{t('tasks.progress')}</TableHead>
                <TableHead className="w-[40px]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((task, i) => {
                const active = isActive(task.status);
                const stepMsg = String(task.step ?? task.current_step ?? task.message ?? '');
                const errMsg = String(task.error_message ?? task.exitstatus ?? '');
                const displayMsg = task.status === 'failed' ? (errMsg || stepMsg) : (active ? stepMsg : '');

                return (
                  <TableRow key={task.id ?? task.upid ?? i} className={active ? 'bg-blue-500/5' : ''}>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        {active && <Loader2 className="h-3 w-3 animate-spin text-blue-500 shrink-0" />}
                        <Badge variant={statusColor(task.status) as any}>{task.status}</Badge>
                      </div>
                    </TableCell>
                    <TableCell className="text-xs font-mono">{task.type || String(task.task_type ?? '') || '—'}</TableCell>
                    <TableCell className="text-xs max-w-[200px]">
                      <span className="line-clamp-2">{String(task.description ?? '') || task.upid || '—'}</span>
                    </TableCell>
                    <TableCell className="text-xs">
                      {displayMsg ? (
                        <Tooltip>
                          <TooltipTrigger>
                            <span className={`line-clamp-2 ${task.status === 'failed' ? 'text-destructive' : 'text-muted-foreground'}`}>
                              {displayMsg}
                            </span>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-sm break-all">{displayMsg}</TooltipContent>
                        </Tooltip>
                      ) : '—'}
                    </TableCell>
                    <TableCell className="text-xs">{task.server_name || '—'}</TableCell>
                    <TableCell className="text-xs">{task.node || '—'}</TableCell>
                    <TableCell className="text-xs">{task.user || '—'}</TableCell>
                    <TableCell className="text-xs whitespace-nowrap">{task.starttime ? new Date(task.starttime * 1000).toLocaleString() : task.created_at ? new Date(String(task.created_at)).toLocaleString() : '—'}</TableCell>
                    <TableCell className="text-xs">
                      {task.progress != null ? (
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-16 rounded-full bg-muted overflow-hidden">
                            <div
                              className={`h-full rounded-full transition-all ${task.status === 'failed' ? 'bg-destructive' : 'bg-blue-500'}`}
                              style={{ width: `${task.progress}%` }}
                            />
                          </div>
                          <span>{Number(task.progress ?? 0)}%</span>
                        </div>
                      ) : '—'}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {task.upid && task.node && task.server_id && (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={() => setLogTask(task)}
                            title={t('tasks.view_log')}
                          >
                            <FileText className="h-3.5 w-3.5" />
                          </Button>
                        )}
                        {active && (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 text-destructive"
                            onClick={() => cancelTask.mutate(Number(task.id ?? 0))}
                            disabled={cancelTask.isPending}
                          >
                            <X className="h-3.5 w-3.5" />
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
              {filtered.length === 0 && (
                <TableRow>
                  <TableCell colSpan={10} className="text-center text-muted-foreground py-8">
                    {t('common.no_data')}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <TaskLogDialog task={logTask} onClose={() => setLogTask(null)} />
    </div>
  );
}

function TaskLogDialog({ task, onClose }: { task: TaskItem | null; onClose: () => void }) {
  const { t } = useTranslation();
  const open = !!task;
  const serverId = task?.server_id ?? 0;
  const upid = task?.upid ?? '';
  const node = task?.node ?? '';
  // Poll while the task is still running.
  const running = task?.status === 'running' || task?.status === 'in_progress';
  const { data: statusData } = useTaskStatus(serverId, upid, node, open && running);
  // Keep tailing the log only while the task is still running.
  const live = running && statusData?.status !== 'stopped';
  const { data: logData, isLoading } = useTaskLog(serverId, upid, node, open, live);

  const lines = logData?.logs ?? [];
  const status = statusData?.status ?? task?.status;
  const exit = statusData?.exitstatus as string | undefined;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {t('tasks.task_log')}
            {status != null && <Badge variant="outline">{String(status)}</Badge>}
            {exit && exit !== 'OK' && <Badge variant="destructive">{exit}</Badge>}
          </DialogTitle>
        </DialogHeader>
        <p className="text-xs font-mono text-muted-foreground break-all">{upid}</p>
        {isLoading ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.loading')}</p>
        ) : lines.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t('common.no_data')}</p>
        ) : (
          <pre className="max-h-[60vh] overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap">
            {lines.map(l => l.t).join('\n')}
          </pre>
        )}
      </DialogContent>
    </Dialog>
  );
}
