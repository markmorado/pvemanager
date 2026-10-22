import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  useReactTable,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  getFacetedRowModel,
  getFacetedUniqueValues,
  flexRender,
  type ColumnDef,
  type SortingState,
  type ColumnFiltersState,
} from '@tanstack/react-table';
import { Monitor, Search, Download, Trash2, Loader2, Rocket, PackagePlus, Pencil, ArrowUpDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { OsLogo } from './OsLogo';
import { DeployDialog, DownloadCTDialog, EditTemplateDialog } from './TemplateDialogs';
import type { OSTemplate } from '@/types';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ColumnFilter, multiSelectFilter } from '@/components/shared/column-filter';
import { useTemplates, useAutoImportTemplates, useDeleteTemplate } from '@/hooks/use-templates';
import { useServers } from '@/hooks/use-nodes';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { GroupIcon } from '@/components/shared/GroupIcon';

function SortHeader({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" className="-ml-3 h-8" onClick={onClick}>
      {label}
      <ArrowUpDown className="ml-1 h-3 w-3" />
    </Button>
  );
}

export default function OsTemplatesTab() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const [sorting, setSorting] = useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [importDialogOpen, setImportDialogOpen] = useState(false);
  const [importServerId, setImportServerId] = useState<string>('');
  const [deployTpl, setDeployTpl] = useState<OSTemplate | null>(null);
  const [editTpl, setEditTpl] = useState<OSTemplate | null>(null);
  const [downloadOpen, setDownloadOpen] = useState(false);

  const { data: templates = [], isLoading } = useTemplates();
  const { data: servers = [] } = useServers();
  const autoImport = useAutoImportTemplates();
  const deleteTpl = useDeleteTemplate();

  const handleImport = () => {
    if (!importServerId) return;
    autoImport.mutate(Number(importServerId), {
      onSuccess: (res) => {
        const count = res.imported_count ?? 0;
        toast.success(
          count > 0
            ? t('templates.imported_count', { count })
            : t('templates.imported_none'),
        );
        setImportDialogOpen(false);
      },
      onError: (err: Error) => toast.error(err.message),
    });
  };

  const columns = useMemo<ColumnDef<OSTemplate>[]>(() => [
    {
      accessorKey: 'name',
      filterFn: 'includesString',
      meta: { filter: 'text' },
      header: ({ column }) => (
        <SortHeader label={t('common.name')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ row }) => {
        const tpl = row.original;
        return (
          <div className="flex items-center gap-2">
            <div className="shrink-0">
              {tpl.vm_type === 'qemu' ? (
                <OsLogo name={tpl.name} className="h-6 w-6" />
              ) : tpl.group_icon ? (
                <GroupIcon icon={tpl.group_icon} />
              ) : (
                <Monitor className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
            <span className="font-medium truncate" title={tpl.name}>{tpl.name}</span>
          </div>
        );
      },
      size: 260,
    },
    {
      accessorKey: 'id',
      header: ({ column }) => (
        <SortHeader label={t('templates.col_id')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ getValue }) => <span className="text-xs text-muted-foreground">{getValue<number>()}</span>,
      size: 70,
    },
    {
      accessorKey: 'vm_type',
      filterFn: multiSelectFilter,
      meta: { filter: 'select', formatOption: (v: string) => (v === 'lxc' ? t('templates.type_lxc') : t('templates.type_kvm')) },
      header: ({ column }) => (
        <SortHeader label={t('common.type')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ getValue }) => {
        const type = getValue<string>();
        return (
          <Badge variant={type === 'lxc' ? 'secondary' : 'default'} className="text-xs font-normal">
            {type === 'lxc' ? t('templates.type_lxc') : t('templates.type_kvm')}
          </Badge>
        );
      },
      size: 90,
    },
    {
      id: 'group',
      accessorFn: (tpl) => tpl.group_name || '',
      filterFn: multiSelectFilter,
      meta: { filter: 'select' },
      header: ({ column }) => (
        <SortHeader label={t('templates.group')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ getValue }) => {
        const v = getValue<string>();
        return v ? <span className="text-sm">{v}</span> : <span className="text-muted-foreground">—</span>;
      },
      size: 140,
    },
    {
      id: 'server',
      accessorFn: (tpl) => tpl.server_name || '',
      filterFn: multiSelectFilter,
      meta: { filter: 'select' },
      header: ({ column }) => (
        <SortHeader label={t('templates.server')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ getValue }) => {
        const v = getValue<string>();
        return v ? <span className="text-sm text-muted-foreground">{v}</span> : <span className="text-muted-foreground">—</span>;
      },
      size: 140,
    },
    {
      accessorKey: 'updated_at',
      header: ({ column }) => (
        <SortHeader label={t('templates.col_updated')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ getValue }) => {
        const v = getValue<string>();
        return <span className="text-xs text-muted-foreground whitespace-nowrap">{v ? new Date(v).toLocaleDateString() : '—'}</span>;
      },
      size: 110,
    },
    {
      accessorKey: 'default_disk',
      header: ({ column }) => (
        <SortHeader label={t('templates.col_size')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ getValue }) => <span className="text-xs tabular-nums">{getValue<number>()} GB</span>,
      size: 90,
    },
    {
      id: 'actions',
      enableSorting: false,
      cell: ({ row }) => {
        const tpl = row.original;
        return (
          <div className="flex items-center justify-end gap-0.5">
            {tpl.vm_type === 'qemu' && tpl.vmid != null && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                title={t('templates.deploy')}
                onClick={() => setDeployTpl(tpl)}
              >
                <Rocket className="h-3.5 w-3.5" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              title={t('templates.edit_group')}
              onClick={() => setEditTpl(tpl)}
            >
              <Pencil className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0 text-destructive"
              title={t('common.delete')}
              onClick={async () => { if (await confirm(t('common.confirm_delete'))) deleteTpl.mutate(tpl.id); }}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        );
      },
      size: 110,
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t]);

  const table = useReactTable({
    data: templates,
    columns,
    state: { sorting, columnFilters },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getFacetedRowModel: getFacetedRowModel(),
    getFacetedUniqueValues: getFacetedUniqueValues(),
    initialState: { pagination: { pageSize: 50 } },
  });

  const nameFilterValue = (table.getColumn('name')?.getFilterValue() as string) ?? '';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative flex-1 min-w-[220px] max-w-xs">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-9"
            placeholder={t('templates.search')}
            value={nameFilterValue}
            onChange={(e) => table.getColumn('name')?.setFilterValue(e.target.value || undefined)}
          />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Button variant="outline" onClick={() => setDownloadOpen(true)}>
            <PackagePlus className="mr-2 h-4 w-4" />{t('templates.download_ct')}
          </Button>
          <Button onClick={() => setImportDialogOpen(true)}>
            <Download className="mr-2 h-4 w-4" />{t('templates.auto_import')}
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="space-y-3 p-4" aria-busy="true" aria-label={t('common.loading')}>
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="flex items-center gap-4">
                  <Skeleton className="h-4 w-4 rounded" />
                  <Skeleton className="h-4 flex-1 max-w-[220px]" />
                  <Skeleton className="hidden h-4 w-16 sm:block" />
                  <Skeleton className="hidden h-4 w-24 md:block" />
                  <Skeleton className="ml-auto h-4 w-8" />
                </div>
              ))}
            </div>
          ) : templates.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Monitor className="mb-3 h-10 w-10 opacity-40" />
              <p className="text-sm font-medium">{t('templates.no_templates')}</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                {table.getHeaderGroups().map((headerGroup) => (
                  <TableRow key={headerGroup.id}>
                    {headerGroup.headers.map((header) => {
                      const filterVariant = header.column.columnDef.meta?.filter;
                      const sorted = header.column.getIsSorted();
                      const ariaSort = !header.column.getCanSort()
                        ? undefined
                        : sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : 'none';
                      return (
                        <TableHead key={header.id} style={{ width: header.getSize() }} aria-sort={ariaSort}>
                          {header.isPlaceholder ? null : (
                            <div className="flex items-center gap-0.5">
                              {flexRender(header.column.columnDef.header, header.getContext())}
                              {filterVariant && (
                                <ColumnFilter
                                  column={header.column}
                                  variant={filterVariant}
                                  formatOption={header.column.columnDef.meta?.formatOption}
                                />
                              )}
                            </div>
                          )}
                        </TableHead>
                      );
                    })}
                  </TableRow>
                ))}
              </TableHeader>
              <TableBody>
                {table.getRowModel().rows.map((row) => (
                  <TableRow key={row.id}>
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {templates.length > 0 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            {t('instances.showing', 'Showing')} {table.getState().pagination.pageIndex * table.getState().pagination.pageSize + 1}–
            {Math.min(
              (table.getState().pagination.pageIndex + 1) * table.getState().pagination.pageSize,
              table.getFilteredRowModel().rows.length,
            )}{' '}
            {t('instances.of', 'of')} {table.getFilteredRowModel().rows.length}
          </span>
          <div className="flex items-center gap-1">
            <Button variant="outline" size="icon" className="h-8 w-8" onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()}>
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="px-2 tabular-nums">{table.getState().pagination.pageIndex + 1} / {Math.max(table.getPageCount(), 1)}</span>
            <Button variant="outline" size="icon" className="h-8 w-8" onClick={() => table.nextPage()} disabled={!table.getCanNextPage()}>
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      <DeployDialog template={deployTpl} onClose={() => setDeployTpl(null)} />
      <EditTemplateDialog template={editTpl} onClose={() => setEditTpl(null)} />
      <DownloadCTDialog open={downloadOpen} onClose={() => setDownloadOpen(false)} />

      <Dialog open={importDialogOpen} onOpenChange={setImportDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('templates.auto_import')}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <Select value={importServerId} onValueChange={v => { if (v !== null) setImportServerId(v); }}>
              <SelectTrigger><SelectValue placeholder={t('templates.select_server')} /></SelectTrigger>
              <SelectContent>
                {servers.map(s => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button onClick={handleImport} disabled={!importServerId || autoImport.isPending} className="w-full">
              {autoImport.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t('templates.import_btn')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
