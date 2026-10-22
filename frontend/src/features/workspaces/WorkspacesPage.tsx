import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Search, Trash2, Users, Server, Pencil } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { ServerStatusBadge } from '@/components/shared/ServerStatusBadge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogClose, DialogFooter } from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useWorkspaces, useCreateWorkspace, useUpdateWorkspace, useDeleteWorkspace, useWorkspace, useUpdateWorkspaceServers, useUpdateWorkspaceUsers } from '@/hooks/use-workspaces';
import { useServers } from '@/hooks/use-nodes';
import { useUsers } from '@/hooks/use-users';

export default function WorkspacesPage() {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const { data: workspaces = [] } = useWorkspaces();
  const createWorkspace = useCreateWorkspace();
  const updateWorkspace = useUpdateWorkspace();
  const deleteWorkspace = useDeleteWorkspace();
  const [editOpen, setEditOpen] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');

  const filtered = workspaces.filter(w => {
    if (!search) return true;
    const s = search.toLowerCase();
    return w.name?.toLowerCase().includes(s) || w.description?.toLowerCase().includes(s);
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t('nav.workspaces')}</h1>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger render={<Button size="sm" />}><Plus className="h-4 w-4 mr-1" />{t('workspaces.create')}</DialogTrigger>
          <DialogContent>
            <DialogHeader><DialogTitle>{t('workspaces.create')}</DialogTitle></DialogHeader>
            <div className="space-y-3">
              <div><Label>{t('common.name')}</Label><Input value={name} onChange={e => setName(e.target.value)} className="mt-1" /></div>
              <div><Label>{t('ipam.description')}</Label><Input value={description} onChange={e => setDescription(e.target.value)} className="mt-1" /></div>
            </div>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
              <Button onClick={() => { createWorkspace.mutate({ name, description }); setOpen(false); setName(''); setDescription(''); }}>{t('common.create')}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      <div className="relative max-w-sm">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input placeholder={t('common.search')} value={search} onChange={e => setSearch(e.target.value)} className="pl-8" />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {filtered.map(ws => (
          <Card key={ws.id}>
            <CardHeader className="pb-2">
              <div className="flex items-start justify-between">
                <CardTitle className="text-base">{ws.name}</CardTitle>
                <div className="flex gap-1">
                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => {
                    setEditId(ws.id);
                    setEditName(ws.name);
                    setEditDescription(ws.description || '');
                    setEditOpen(true);
                  }}>
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => deleteWorkspace.mutate(ws.id)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
              {ws.description && <p className="text-xs text-muted-foreground">{ws.description}</p>}
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex items-center gap-4 text-sm text-muted-foreground">
                <div className="flex items-center gap-1"><Server className="h-3.5 w-3.5" />{ws.server_count ?? ws.servers?.length ?? 0} {t('workspaces.servers')}</div>
                <div className="flex items-center gap-1"><Users className="h-3.5 w-3.5" />{ws.user_count ?? ws.users?.length ?? 0} {t('workspaces.users')}</div>
              </div>
              {ws.servers && ws.servers.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {ws.servers.map((s: any) => (
                    <Badge key={s.id ?? s} variant="outline" className="text-xs">{s.name ?? s}</Badge>
                  ))}
                </div>
              )}
              <p className="text-xs text-muted-foreground">{t('workspaces.created')}: {ws.created_at ? new Date(ws.created_at).toLocaleDateString() : '—'}</p>
            </CardContent>
          </Card>
        ))}
        {filtered.length === 0 && (
          <div className="col-span-full text-center text-muted-foreground py-12">{t('common.no_data')}</div>
        )}
      </div>

      {editId && (
        <EditWorkspaceDialog
          open={editOpen}
          onOpenChange={setEditOpen}
          workspaceId={editId}
          initialName={editName}
          initialDescription={editDescription}
          onSave={(name, description) => {
            updateWorkspace.mutate({ id: editId, name, description });
            setEditOpen(false);
          }}
        />
      )}
    </div>
  );
}

function EditWorkspaceDialog({
  open, onOpenChange, workspaceId, initialName, initialDescription, onSave,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  workspaceId: number;
  initialName: string;
  initialDescription: string;
  onSave: (name: string, description: string) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription);

  const { data: workspace } = useWorkspace(workspaceId);
  // allWorkspaces: этот диалог назначает серверы рабочей области, поэтому
  // список не должен фильтроваться заголовком активной рабочей области.
  const { data: allServers = [] } = useServers({ allWorkspaces: true });
  const { data: allUsers = [] } = useUsers();
  const updateServers = useUpdateWorkspaceServers();
  const updateUsers = useUpdateWorkspaceUsers();

  // Track selected server/user IDs
  const [selectedServerIds, setSelectedServerIds] = useState<number[]>([]);
  const [selectedUserIds, setSelectedUserIds] = useState<number[]>([]);

  // Populate selections once per loaded workspace (refetches keep the user's edits).
  const [populatedFor, setPopulatedFor] = useState<number | null>(null);
  if (workspace && workspace.id !== populatedFor) {
    setPopulatedFor(workspace.id);
    setSelectedServerIds(workspace.servers?.map((s) => s.id) ?? []);
    setSelectedUserIds(workspace.users?.map((u) => u.id) ?? []);
  }

  const handleOpen = (v: boolean) => {
    if (v) {
      setName(initialName);
      setDescription(initialDescription);
    }
    onOpenChange(v);
  };

  const toggleServer = (id: number) =>
    setSelectedServerIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const toggleUser = (id: number) =>
    setSelectedUserIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const handleSave = () => {
    onSave(name, description);
    updateServers.mutate({ id: workspaceId, ids: selectedServerIds });
    updateUsers.mutate({ id: workspaceId, ids: selectedUserIds });
  };

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader><DialogTitle>{t('workspaces.edit')}</DialogTitle></DialogHeader>
        <Tabs defaultValue="info">
          <TabsList className="w-full">
            <TabsTrigger value="info" className="flex-1">{t('common.name')}</TabsTrigger>
            <TabsTrigger value="servers" className="flex-1">{t('workspaces.assign_servers')}</TabsTrigger>
            <TabsTrigger value="users" className="flex-1">{t('workspaces.assign_users')}</TabsTrigger>
          </TabsList>

          <TabsContent value="info" className="space-y-3 mt-3">
            <div><Label>{t('common.name')}</Label><Input value={name} onChange={e => setName(e.target.value)} className="mt-1" /></div>
            <div><Label>{t('ipam.description')}</Label><Input value={description} onChange={e => setDescription(e.target.value)} className="mt-1" /></div>
          </TabsContent>

          <TabsContent value="servers" className="mt-3">
            {allServers.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-4">{t('workspaces.no_servers')}</p>
            ) : (
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {allServers.map(srv => (
                  <label key={srv.id} className="flex items-center gap-3 rounded-md border p-2.5 cursor-pointer hover:bg-accent">
                    <Checkbox
                      checked={selectedServerIds.includes(srv.id)}
                      onChange={() => toggleServer(srv.id)}
                    />
                    <div className="min-w-0">
                      <p className="text-sm font-medium">{srv.name}</p>
                      <p className="text-xs text-muted-foreground font-mono">{srv.ip_address}</p>
                    </div>
                    <ServerStatusBadge
                      isOnline={srv.is_online}
                      errorKind={srv.last_error_kind}
                      lastError={srv.last_error}
                      className="ml-auto shrink-0 text-xs"
                    />
                  </label>
                ))}
              </div>
            )}
          </TabsContent>

          <TabsContent value="users" className="mt-3">
            {allUsers.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-4">{t('workspaces.no_users')}</p>
            ) : (
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {allUsers.map((u: any) => (
                  <label key={u.id} className="flex items-center gap-3 rounded-md border p-2.5 cursor-pointer hover:bg-accent">
                    <Checkbox
                      checked={selectedUserIds.includes(u.id)}
                      onChange={() => toggleUser(u.id)}
                    />
                    <div className="min-w-0">
                      <p className="text-sm font-medium">{u.username}</p>
                      <p className="text-xs text-muted-foreground truncate">{u.email}</p>
                    </div>
                    {u.is_admin && <Badge variant="outline" className="ml-auto shrink-0 text-xs">Admin</Badge>}
                  </label>
                ))}
              </div>
            )}
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
          <Button onClick={handleSave}>{t('common.save')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
