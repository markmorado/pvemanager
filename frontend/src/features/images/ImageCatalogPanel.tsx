import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table';
import { Search, Download, ArrowUpDown, ChevronLeft, ChevronRight, HardDriveDownload } from 'lucide-react';
import { OsLogo } from '@/features/templates/OsLogo';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useServers, useNodes } from '@/hooks/use-nodes';
import { useImageCatalog, useImageLXCTemplates, useNodeArch, useNodeIsos } from '@/hooks/use-image-catalog';
import { useProfile } from '@/hooks/use-settings';
import { formatBytes } from '@/lib/format';
import DownloadImageDialog, { type SelectedImage } from './DownloadImageDialog';
import DownloadIsoDialog, { type IsoSource } from './DownloadIsoDialog';
import MirrorsManager from './MirrorsManager';
import type { CatalogImage } from '@/types';

function SortHeader({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" className="-ml-3 h-8" onClick={onClick}>
      {label}
      <ArrowUpDown className="ml-1 h-3 w-3" />
    </Button>
  );
}

export type ImageCatalogSection = 'vm-images' | 'lxc' | 'iso' | 'repositories';

export default function ImageCatalogPanel({ section }: { section: ImageCatalogSection }) {
  const { t } = useTranslation();
  const [serverId, setServerId] = useState<number | null>(null);
  const [nodePick, setNode] = useState<string>('');
  const [search, setSearch] = useState('');
  // По умолчанию показываем amd64 (доминирующая платформа PVE); уточняется по арх ноды
  const [archFilter, setArchFilter] = useState<string>('amd64');
  const [selected, setSelected] = useState<SelectedImage | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [isoSource, setIsoSource] = useState<IsoSource | null>(null);
  const [isoDialogOpen, setIsoDialogOpen] = useState(false);

  const { data: servers = [] } = useServers();
  const { data: profile } = useProfile();
  const isAdmin = !!profile?.is_admin;
  const { data: nodesResp } = useNodes(serverId ?? 0);
  const nodes = nodesResp?.nodes ?? [];
  // Выбранная нода, а пока выбора нет — первая нода сервера.
  const node = nodePick || nodes[0]?.node || '';

  const { data: catalog } = useImageCatalog();
  const { data: lxcRepo = [] } = useImageLXCTemplates(serverId ?? undefined, node || undefined);
  const { data: nodeArch } = useNodeArch(serverId ?? undefined, node || undefined);
  const { data: nodeIsosResp } = useNodeIsos(serverId ?? undefined, node || undefined);

  // Авто-выбор первого сервера и ноды
  useEffect(() => {
    if (serverId == null && servers.length > 0) setServerId(servers[0].id);
  }, [servers, serverId]);
  // Сбросить протухший выбор сервера, если его нет в списке активной рабочей
  // области (например, после переключения рабочей области) — иначе выбор
  // молча остаётся, а Select показывает голый id сервера вместо имени.
  useEffect(() => {
    if (serverId != null && servers.length > 0 && !servers.some((s) => s.id === serverId)) {
      setServerId(null);
      setNode('');
    }
  }, [servers, serverId]);
  // Подстроить фильтр архитектуры под выбранную ноду (если арх определилась)
  useEffect(() => {
    if (nodeArch?.arch) setArchFilter(nodeArch.arch);
  }, [nodeArch?.arch, node]);

  const cloudImages = useMemo<CatalogImage[]>(() => {
    const all = [...(catalog?.builtin ?? []), ...(catalog?.mirrors ?? [])];
    return all.filter((img) => {
      if (img.kind !== 'qcow2') return false;
      if (archFilter && img.arch !== archFilter) return false;
      const q = search.toLowerCase();
      return !q || `${img.name} ${img.os ?? ''} ${img.version ?? ''}`.toLowerCase().includes(q);
    });
  }, [catalog, archFilter, search]);

  // ISO во встроенном каталоге нет — ссылки на дистрибутивы живут в зеркалах пользователя.
  const isoImages = useMemo<CatalogImage[]>(() => {
    const all = [...(catalog?.builtin ?? []), ...(catalog?.mirrors ?? [])];
    const q = search.toLowerCase();
    return all.filter((img) => img.kind === 'iso'
      && (!q || `${img.name} ${img.os ?? ''} ${img.version ?? ''}`.toLowerCase().includes(q)));
  }, [catalog, search]);

  // Реальные ISO-файлы на хранилищах выбранной ноды (в т.ч. существовавшие
  // до подключения ноды к панели). Фильтр по поисковой строке.
  const nodeIsos = useMemo(() => {
    const all = nodeIsosResp?.isos ?? [];
    const q = search.toLowerCase();
    return all.filter((iso) => !q || `${iso.name} ${iso.volid}`.toLowerCase().includes(q));
  }, [nodeIsosResp, search]);

  const openIsoDownload = (src: IsoSource | null) => {
    setIsoSource(src);
    setIsoDialogOpen(true);
  };

  const lxcTemplates = useMemo(() => {
    const q = search.toLowerCase();
    return lxcRepo.filter((tpl) => !q || `${tpl.template} ${tpl.os ?? ''} ${tpl.headline ?? ''}`.toLowerCase().includes(q));
  }, [lxcRepo, search]);

  const openDownload = (img: SelectedImage) => {
    setSelected(img);
    setDialogOpen(true);
  };

  const canDownload = serverId != null && !!node;
  const needsContext = section === 'vm-images' || section === 'lxc' || section === 'iso';

  const [sorting, setSorting] = useState<SortingState>([]);

  const cloudImageColumns = useMemo<ColumnDef<CatalogImage>[]>(() => [
    {
      accessorKey: 'name',
      header: ({ column }) => (
        <SortHeader label={t('common.name')} onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')} />
      ),
      cell: ({ row }) => {
        const img = row.original;
        return (
          <div className="flex items-center gap-2">
            <OsLogo name={img.os || img.name} className="h-6 w-6" />
            <span className="font-medium truncate" title={img.name}>{img.name}</span>
            {img.source === 'mirror' && <Badge variant="outline" className="text-2xs font-normal">{t('images.mirror_badge')}</Badge>}
          </div>
        );
      },
      size: 320,
    },
    {
      accessorKey: 'arch',
      header: t('images.arch'),
      cell: ({ getValue }) => <Badge variant="secondary" className="font-normal">{getValue<string>()}</Badge>,
      size: 110,
    },
    {
      id: 'actions',
      enableSorting: false,
      cell: ({ row }) => {
        const img = row.original;
        return (
          <div className="flex justify-end">
            <Button size="sm" disabled={!canDownload}
              onClick={() => openDownload({ source_id: img.id, kind: 'qcow2', name: img.name, arch: String(img.arch) })}>
              <Download className="h-4 w-4" />
            </Button>
          </div>
        );
      },
      size: 70,
    },
  ], [t, canDownload]);

  const cloudImagesTable = useReactTable({
    data: cloudImages,
    columns: cloudImageColumns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 50 } },
  });

  return (
    <div className="space-y-4">
      {needsContext && (
        <div className="flex flex-wrap gap-3">
          <Select value={serverId != null ? String(serverId) : ''} onValueChange={(v) => { if (v !== null) { setServerId(Number(v)); setNode(''); } }}>
            <SelectTrigger className="w-[200px]"><SelectValue placeholder={t('images.server')} /></SelectTrigger>
            <SelectContent>
              {servers.map((s) => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={node} onValueChange={(v) => { if (v) setNode(v); }}>
            <SelectTrigger className="w-[180px]"><SelectValue placeholder={t('images.node')} /></SelectTrigger>
            <SelectContent>
              {nodes.length === 0 && <SelectItem value="__empty__" disabled>{t('images.no_nodes')}</SelectItem>}
              {nodes.map((n) => <SelectItem key={n.node} value={n.node}>{n.node}</SelectItem>)}
            </SelectContent>
          </Select>
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input className="pl-9" placeholder={t('images.search')} value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          {section === 'vm-images' && (
            <Select value={archFilter || '__all__'} onValueChange={(v) => { if (v !== null) setArchFilter(v === '__all__' ? '' : v); }}>
              <SelectTrigger className="w-[150px]"><SelectValue placeholder={t('images.arch')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{t('images.arch_all')}</SelectItem>
                <SelectItem value="amd64">amd64</SelectItem>
                <SelectItem value="arm64">arm64</SelectItem>
              </SelectContent>
            </Select>
          )}
        </div>
      )}

      {/* Cloud-образы (qcow2 → шаблон ВМ) */}
      {section === 'vm-images' && (
        <div className="space-y-4">
          <Card>
            <CardContent className="p-0">
              {cloudImages.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
                  <HardDriveDownload className="mb-3 h-10 w-10 opacity-40" />
                  <p className="text-sm font-medium">{t('images.no_images_filter')}</p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    {cloudImagesTable.getHeaderGroups().map((headerGroup) => (
                      <TableRow key={headerGroup.id}>
                        {headerGroup.headers.map((header) => {
                          const sorted = header.column.getIsSorted();
                          const ariaSort = !header.column.getCanSort()
                            ? undefined
                            : sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : 'none';
                          return (
                            <TableHead key={header.id} style={{ width: header.getSize() }} aria-sort={ariaSort}>
                              {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                            </TableHead>
                          );
                        })}
                      </TableRow>
                    ))}
                  </TableHeader>
                  <TableBody>
                    {cloudImagesTable.getRowModel().rows.map((row) => (
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

          {cloudImages.length > 0 && (
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {t('instances.showing', 'Showing')} {cloudImagesTable.getState().pagination.pageIndex * cloudImagesTable.getState().pagination.pageSize + 1}–
                {Math.min(
                  (cloudImagesTable.getState().pagination.pageIndex + 1) * cloudImagesTable.getState().pagination.pageSize,
                  cloudImages.length,
                )}{' '}
                {t('instances.of', 'of')} {cloudImages.length}
              </span>
              <div className="flex items-center gap-1">
                <Button variant="outline" size="icon" className="h-8 w-8" onClick={() => cloudImagesTable.previousPage()} disabled={!cloudImagesTable.getCanPreviousPage()}>
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span className="px-2 tabular-nums">{cloudImagesTable.getState().pagination.pageIndex + 1} / {Math.max(cloudImagesTable.getPageCount(), 1)}</span>
                <Button variant="outline" size="icon" className="h-8 w-8" onClick={() => cloudImagesTable.nextPage()} disabled={!cloudImagesTable.getCanNextPage()}>
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ISO-образы (установочные) */}
      {section === 'iso' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            <Button size="sm" variant="outline" disabled={!canDownload}
              onClick={() => openIsoDownload(null)}>
              <Download className="mr-2 h-4 w-4" /> {t('images.download_by_url')}
            </Button>
          </div>

          {!canDownload && (
            <div className="text-sm text-muted-foreground">{t('images.select_server_node')}</div>
          )}

          {/* Реальные ISO на хранилище ноды (загруженные ранее, в т.ч. до подключения к панели) */}
          {canDownload && nodeIsos.length > 0 && (
            <div className="space-y-2">
              <div className="text-sm font-medium text-muted-foreground">{t('images.node_storage')}</div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {nodeIsos.map((iso) => (
                  <Card key={iso.volid}>
                    <CardContent className="flex items-center gap-3 p-4">
                      <OsLogo name={iso.name} />
                      <div className="min-w-0 flex-1">
                        <div className="font-medium truncate" title={iso.name}>{iso.name}</div>
                        <div className="flex items-center gap-1.5 mt-1">
                          <Badge variant="outline">{iso.storage}</Badge>
                          {iso.size != null && <Badge variant="secondary">{formatBytes(iso.size)}</Badge>}
                        </div>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </div>
          )}

          {/* Сохранённые ISO-ссылки из каталога/зеркал */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {isoImages.map((img) => (
              <Card key={img.id}>
                <CardContent className="flex items-center gap-3 p-4">
                  <OsLogo name={img.os || img.name} />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium truncate">{img.name}</div>
                    <div className="text-xs text-muted-foreground truncate" title={img.url ?? ''}>{img.url}</div>
                  </div>
                  <Button size="sm" disabled={!canDownload}
                    onClick={() => openIsoDownload({ source_id: img.id, name: img.name, url: img.url ?? undefined })}>
                    <Download className="h-4 w-4" />
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
          {isoImages.length === 0 && (
            <div className="text-sm text-muted-foreground">
              {t('images.no_iso_links')}
            </div>
          )}
        </div>
      )}

      {/* LXC-шаблоны (репозиторий Proxmox) */}
      {section === 'lxc' && (
        <div className="space-y-3">
          {!canDownload && (
            <div className="text-sm text-muted-foreground">{t('images.select_server_node_list')}</div>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {lxcTemplates.map((tpl) => (
              <Card key={tpl.template}>
                <CardContent className="flex items-center gap-3 p-4">
                  <OsLogo name={tpl.os || tpl.template} />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium truncate" title={tpl.template}>{tpl.headline || tpl.template}</div>
                    <div className="text-xs text-muted-foreground truncate">{tpl.template}</div>
                  </div>
                  <Button size="sm" disabled={!canDownload}
                    onClick={() => openDownload({ kind: 'vztmpl', name: tpl.template, template: tpl.template })}>
                    <Download className="h-4 w-4" />
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {/* Репозитории / зеркала */}
      {section === 'repositories' && (
        isAdmin ? (
          <MirrorsManager />
        ) : (
          <div className="text-sm text-muted-foreground py-8 text-center">{t('common.access_denied')}</div>
        )
      )}

      <DownloadImageDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        serverId={serverId ?? 0}
        node={node}
        image={selected}
      />

      <DownloadIsoDialog
        open={isoDialogOpen}
        onClose={() => setIsoDialogOpen(false)}
        serverId={serverId ?? 0}
        node={node}
        source={isoSource}
      />
    </div>
  );
}
