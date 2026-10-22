import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import {
  Search, Plus, Shield, ShieldOff, Trash2, RotateCcw, Unlock, Pencil, Server, LogOut, Ban, KeyRound, Gauge, UserCog,
} from 'lucide-react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { ServerStatusBadge } from '@/components/shared/ServerStatusBadge';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogClose, DialogFooter,
} from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { User } from '@/types';
import {
  useUsers, useCreateUser, useUpdateUser, useDeleteUser,
  useResetPassword, useUnlockUser, useTerminateUserSessions, useDisableUser2FA,
  useRoles, useCreateRole, useUpdateRole, useDeleteRole, usePermissions,
  useSessions, useTerminateSession, useTerminateAllSessions,
  useUserServerAssignments, useSetUserServers,
  useUserQuota, useSetUserQuota,
  useSecuritySettings, useUpdateSecuritySettings,
  useBlockedIps, useBlockIp, useUnblockIp, useSecurityEvents,
  type Role, type SecuritySettings,
} from '@/hooks/use-users';
import { SSHKeysManager } from '@/features/settings/SSHKeysManager';
import { useAuthStore } from '@/stores/auth-store';
import { useHasPermission } from '@/lib/permissions';

// ==================== Helpers ====================

function isUserLocked(u: User): boolean {
  if (u.is_locked) return true;
  if (u.locked_until) {
    return new Date(u.locked_until).getTime() > Date.now();
  }
  return false;
}

function fmtDate(s?: string | null): string {
  if (!s) return '—';
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

function getErrMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

// ==================== Page ====================

export default function UsersPage() {
  const { t } = useTranslation();
  // Each tab is gated on the permission its own API requires: the user list and
  // active sessions need `user:view` (already required to open this page), while
  // roles and the security settings are separate permissions.
  const canViewRoles = useHasPermission('role:view');
  const canManageSecurity = useHasPermission('setting:manage');

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('nav.users')}</h1>
      <Tabs defaultValue="users">
        <TabsList>
          <TabsTrigger value="users">{t('users.users')}</TabsTrigger>
          {canViewRoles && <TabsTrigger value="roles">{t('users.roles')}</TabsTrigger>}
          <TabsTrigger value="sessions">{t('users.sessions')}</TabsTrigger>
          {canManageSecurity && <TabsTrigger value="security">{t('users.security')}</TabsTrigger>}
        </TabsList>
        <TabsContent value="users"><UsersTab /></TabsContent>
        {canViewRoles && <TabsContent value="roles"><RolesTab /></TabsContent>}
        <TabsContent value="sessions"><SessionsTab /></TabsContent>
        {canManageSecurity && <TabsContent value="security"><SecurityTab /></TabsContent>}
      </Tabs>
    </div>
  );
}

// ==================== Users Tab ====================

function UsersTab() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const currentUser = useAuthStore(s => s.user);
  const impersonate = useAuthStore(s => s.impersonate);
  const [search, setSearch] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [editUser, setEditUser] = useState<User | null>(null);
  const [resetUser, setResetUser] = useState<User | null>(null);
  const [serversUser, setServersUser] = useState<User | null>(null);
  const [quotaUser, setQuotaUser] = useState<User | null>(null);
  const [sshUser, setSshUser] = useState<User | null>(null);

  const { data: users = [] } = useUsers();
  const deleteUser = useDeleteUser();
  const unlockUser = useUnlockUser();
  const disable2FA = useDisableUser2FA();
  const terminateSessions = useTerminateUserSessions();

  const canImpersonate = !!currentUser?.is_admin && !currentUser?.impersonator;

  const handleImpersonate = async (u: User) => {
    try {
      await impersonate(u.id);
      toast.success(t('users.impersonate_started', { name: u.username }));
      navigate('/');
    } catch (e) {
      toast.error(getErrMsg(e));
    }
  };

  const filtered = users.filter(u => {
    if (!search) return true;
    const s = search.toLowerCase();
    return (
      u.username?.toLowerCase().includes(s) ||
      u.full_name?.toLowerCase().includes(s) ||
      u.email?.toLowerCase().includes(s)
    );
  });

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap gap-3 items-center">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder={t('common.search')}
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="pl-8"
            />
          </div>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4 mr-1" />
            {t('users.create')}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('settings.username')}</TableHead>
              <TableHead>{t('settings.display_name')}</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>{t('users.role')}</TableHead>
              <TableHead>{t('common.status')}</TableHead>
              <TableHead>{t('users.failed_attempts')}</TableHead>
              <TableHead>{t('users.last_login')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map(u => {
              const locked = isUserLocked(u);
              return (
                <TableRow key={u.id}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      {u.username}
                      {u.is_admin && <Badge variant="outline" className="text-xs">Admin</Badge>}
                    </div>
                  </TableCell>
                  <TableCell>{u.full_name || '—'}</TableCell>
                  <TableCell className="text-xs">{u.email || '—'}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{u.role_name || t('users.no_role')}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={locked ? 'destructive' : u.is_active ? 'default' : 'secondary'}>
                      {locked ? t('users.locked') : u.is_active ? t('users.active') : t('users.inactive')}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs">{u.failed_login_attempts ?? 0}</TableCell>
                  <TableCell className="text-xs">{fmtDate(u.last_login)}</TableCell>
                  <TableCell>
                    <div className="flex gap-1 justify-end">
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('users.edit')}
                        onClick={() => setEditUser(u)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('users.manage_servers')}
                        onClick={() => setServersUser(u)}
                      >
                        <Server className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('users.quota.manage')}
                        onClick={() => setQuotaUser(u)}
                      >
                        <Gauge className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('ssh_keys.manage_for_user')}
                        onClick={() => setSshUser(u)}
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                      </Button>
                      {canImpersonate && !u.is_admin && u.id !== currentUser?.id && u.is_active && (
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7"
                          title={t('users.impersonate')}
                          onClick={() => handleImpersonate(u)}
                        >
                          <UserCog className="h-3.5 w-3.5" />
                        </Button>
                      )}
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('users.reset_password')}
                        onClick={() => setResetUser(u)}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                      {locked && (
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7"
                          title={t('users.unlock')}
                          onClick={() => unlockUser.mutate(u.id, {
                            onSuccess: () => toast.success(t('users.unlock')),
                            onError: e => toast.error(getErrMsg(e)),
                          })}
                        >
                          <Unlock className="h-3.5 w-3.5" />
                        </Button>
                      )}
                      {u.two_factor_enabled && (
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7"
                          title={t('users.disable_2fa')}
                          onClick={() => disable2FA.mutate(u.id, {
                            onSuccess: r => toast.success(r.message),
                            onError: e => toast.error(getErrMsg(e)),
                          })}
                        >
                          <ShieldOff className="h-3.5 w-3.5" />
                        </Button>
                      )}
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('users.terminate_sessions')}
                        onClick={() => terminateSessions.mutate(u.id, {
                          onSuccess: r => toast.success(t('users.sessions_terminated', { count: r.count })),
                          onError: e => toast.error(getErrMsg(e)),
                        })}
                      >
                        <LogOut className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7 text-destructive"
                        disabled={u.is_admin}
                        onClick={() => deleteUser.mutate(u.id, {
                          onError: e => toast.error(getErrMsg(e)),
                        })}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
            {filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-muted-foreground py-8">
                  {t('common.no_data')}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>

      <CreateUserDialog open={createOpen} onOpenChange={setCreateOpen} />
      {editUser && (
        <EditUserDialog
          user={editUser}
          open={!!editUser}
          onOpenChange={o => !o && setEditUser(null)}
        />
      )}
      {resetUser && (
        <ResetPasswordDialog
          user={resetUser}
          open={!!resetUser}
          onOpenChange={o => !o && setResetUser(null)}
        />
      )}
      {serversUser && (
        <UserServersDialog
          user={serversUser}
          open={!!serversUser}
          onOpenChange={o => !o && setServersUser(null)}
        />
      )}
      {quotaUser && (
        <UserQuotaDialog
          user={quotaUser}
          open={!!quotaUser}
          onOpenChange={o => !o && setQuotaUser(null)}
        />
      )}
      {sshUser && (
        <UserSSHKeysDialog
          user={sshUser}
          open={!!sshUser}
          onOpenChange={o => !o && setSshUser(null)}
        />
      )}
    </Card>
  );
}

// ==================== Create User Dialog ====================

function CreateUserDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [roleId, setRoleId] = useState<string>('none');
  const [isActive, setIsActive] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const { data: roles = [] } = useRoles();
  const createUser = useCreateUser();

  const reset = () => {
    setUsername(''); setPassword(''); setFullName(''); setEmail('');
    setRoleId('none'); setIsActive(true); setIsAdmin(false);
  };

  const submit = () => {
    createUser.mutate(
      {
        username, password, email,
        full_name: fullName || undefined,
        role_id: roleId === 'none' ? null : Number(roleId),
        is_active: isActive,
        is_admin: isAdmin,
      },
      {
        onSuccess: () => { reset(); onOpenChange(false); toast.success(t('users.create')); },
        onError: e => toast.error(getErrMsg(e)),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('users.create')}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>{t('settings.username')}</Label>
            <Input value={username} onChange={e => setUsername(e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label>{t('users.new_password')}</Label>
            <Input type="password" value={password} onChange={e => setPassword(e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label>{t('settings.display_name')}</Label>
            <Input value={fullName} onChange={e => setFullName(e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label>Email</Label>
            <Input type="email" value={email} onChange={e => setEmail(e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label>{t('users.role')}</Label>
            <Select value={roleId} onValueChange={v => v !== null && setRoleId(v)}>
              <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">{t('users.no_role')}</SelectItem>
                {roles.map(r => (
                  <SelectItem key={r.id} value={String(r.id)}>{r.display_name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex gap-4 pt-1">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <Checkbox checked={isActive} onChange={e => setIsActive(e.target.checked)} />
              {t('users.is_active')}
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <Checkbox checked={isAdmin} onChange={e => setIsAdmin(e.target.checked)} />
              {t('users.is_admin')}
            </label>
          </div>
        </div>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
          <Button onClick={submit} disabled={createUser.isPending || !username || !password || !email}>
            {t('common.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Edit User Dialog ====================

function EditUserDialog({
  user, open, onOpenChange,
}: { user: User; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation();
  const [email, setEmail] = useState(user.email);
  const [fullName, setFullName] = useState(user.full_name || '');
  const [password, setPassword] = useState('');
  const [roleId, setRoleId] = useState<string>(user.role_id ? String(user.role_id) : 'none');
  const [isActive, setIsActive] = useState(user.is_active);
  const [isAdmin, setIsAdmin] = useState(user.is_admin);
  const [requirePwdChange, setRequirePwdChange] = useState(!!user.require_password_change);
  const { data: roles = [] } = useRoles();
  const updateUser = useUpdateUser();

  const submit = () => {
    const payload: Parameters<typeof updateUser.mutate>[0] = {
      id: user.id,
      email,
      full_name: fullName,
      role_id: roleId === 'none' ? null : Number(roleId),
      is_active: isActive,
      is_admin: isAdmin,
      require_password_change: requirePwdChange,
    };
    if (password) payload.password = password;
    updateUser.mutate(payload, {
      onSuccess: () => { onOpenChange(false); toast.success(t('common.save')); },
      onError: e => toast.error(getErrMsg(e)),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('users.edit')}: {user.username}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>Email</Label>
            <Input type="email" value={email} onChange={e => setEmail(e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label>{t('settings.display_name')}</Label>
            <Input value={fullName} onChange={e => setFullName(e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label>{t('users.new_password')}</Label>
            <Input
              type="password" value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="—"
              className="mt-1"
            />
          </div>
          <div>
            <Label>{t('users.role')}</Label>
            <Select value={roleId} onValueChange={v => v !== null && setRoleId(v)}>
              <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">{t('users.no_role')}</SelectItem>
                {roles.map(r => (
                  <SelectItem key={r.id} value={String(r.id)}>{r.display_name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-2 pt-1">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <Checkbox checked={isActive} onChange={e => setIsActive(e.target.checked)} />
              {t('users.is_active')}
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <Checkbox checked={isAdmin} onChange={e => setIsAdmin(e.target.checked)} />
              {t('users.is_admin')}
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer col-span-2">
              <Checkbox checked={requirePwdChange} onChange={e => setRequirePwdChange(e.target.checked)} />
              {t('users.require_password_change')}
            </label>
          </div>
        </div>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
          <Button onClick={submit} disabled={updateUser.isPending}>{t('common.save')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Reset Password Dialog ====================

function ResetPasswordDialog({
  user, open, onOpenChange,
}: { user: User; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation();
  const [pwd, setPwd] = useState('');
  const reset = useResetPassword();

  const submit = () => {
    reset.mutate(
      { userId: user.id, newPassword: pwd },
      {
        onSuccess: r => { setPwd(''); onOpenChange(false); toast.success(r.message); },
        onError: e => toast.error(getErrMsg(e)),
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{t('users.reset_password')}: {user.username}</DialogTitle>
        </DialogHeader>
        <div>
          <Label>{t('users.new_password')}</Label>
          <Input type="password" value={pwd} onChange={e => setPwd(e.target.value)} className="mt-1" autoFocus />
        </div>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
          <Button onClick={submit} disabled={!pwd || reset.isPending}>
            <KeyRound className="h-4 w-4 mr-1" />
            {t('users.reset_password')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== User Servers Dialog ====================

function UserServersDialog({
  user, open, onOpenChange,
}: { user: User; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation();
  const { data, isLoading } = useUserServerAssignments(user.id);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('users.manage_servers')}: {user.username}</DialogTitle>
        </DialogHeader>
        {isLoading || !data ? (
          <p className="text-sm text-muted-foreground py-4">{t('common.loading')}</p>
        ) : (
          <UserServersForm
            user={user}
            data={data}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function UserServersForm({
  user, data, onClose,
}: {
  user: User;
  data: NonNullable<ReturnType<typeof useUserServerAssignments>['data']>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const setServers = useSetUserServers();
  const [selected, setSelected] = useState<number[]>(
    data.servers.filter(s => s.assigned).map(s => s.id)
  );

  const toggle = (id: number) => {
    setSelected(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id]);
  };

  const submit = () => {
    setServers.mutate(
      { userId: user.id, serverIds: selected },
      {
        onSuccess: r => { onClose(); toast.success(r.message); },
        onError: e => toast.error(getErrMsg(e)),
      }
    );
  };

  return (
    <>
      <div className="space-y-3">
        <div className="text-xs text-muted-foreground">
          <span className="font-medium">{t('users.workspaces')}:</span>{' '}
          {data.user_workspaces.length
            ? data.user_workspaces.map(w => w.name).join(', ')
            : t('users.no_workspaces')}
        </div>
        {!data.user_workspaces.length && (
          <div className="text-xs text-warning">{t('users.workspace_required')}</div>
        )}
        <div className="space-y-2 max-h-80 overflow-y-auto">
          {data.servers.map(srv => (
            <label
              key={srv.id}
              className={`flex items-center gap-3 rounded-md border p-2.5 ${
                srv.compatible ? 'cursor-pointer hover:bg-accent' : 'opacity-60 cursor-not-allowed'
              }`}
            >
              <Checkbox
                checked={selected.includes(srv.id)}
                onChange={() => srv.compatible && toggle(srv.id)}
                disabled={!srv.compatible}
              />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{srv.name}</p>
                <p className="text-xs text-muted-foreground font-mono">{srv.ip_address}</p>
                {srv.workspaces.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    {t('users.workspaces')}: {srv.workspaces.map(w => w.name).join(', ')}
                  </p>
                )}
              </div>
              <div className="flex flex-col items-end gap-1 shrink-0">
                <ServerStatusBadge
                  isOnline={srv.is_online}
                  errorKind={srv.last_error_kind}
                  className="text-xs"
                />
                {!srv.compatible && (
                  <Badge variant="outline" className="text-xs">{t('users.incompatible')}</Badge>
                )}
              </div>
            </label>
          ))}
          {data.servers.length === 0 && (
            <p className="text-sm text-muted-foreground text-center py-4">{t('common.no_data')}</p>
          )}
        </div>
      </div>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
        <Button onClick={submit} disabled={setServers.isPending}>{t('common.save')}</Button>
      </DialogFooter>
    </>
  );
}

// ==================== User Quota Dialog ====================

function UserQuotaDialog({
  user, open, onOpenChange,
}: { user: User; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation();
  const { data, isLoading } = useUserQuota(user.id);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('users.quota.manage')}: {user.username}</DialogTitle>
        </DialogHeader>
        {isLoading || !data ? (
          <p className="text-sm text-muted-foreground py-4">{t('common.loading')}</p>
        ) : (
          <UserQuotaForm user={user} data={data} onClose={() => onOpenChange(false)} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function UserQuotaForm({
  user, data, onClose,
}: {
  user: User;
  data: NonNullable<ReturnType<typeof useUserQuota>['data']>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const setQuota = useSetUserQuota();

  // Empty string = unlimited
  const toField = (v: number | null) => (v === null || v === undefined ? '' : String(v));
  const [instances, setInstances] = useState(toField(data.max_instances));
  const [cores, setCores] = useState(toField(data.max_cores));
  const [memory, setMemory] = useState(toField(data.max_memory_mb));
  const [disk, setDisk] = useState(toField(data.max_disk_gb));

  const parse = (v: string): number | null => {
    const trimmed = v.trim();
    if (trimmed === '') return null;
    const n = Number(trimmed);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null;
  };

  const submit = () => {
    setQuota.mutate(
      {
        userId: user.id,
        max_instances: parse(instances),
        max_cores: parse(cores),
        max_memory_mb: parse(memory),
        max_disk_gb: parse(disk),
      },
      {
        onSuccess: () => { onClose(); toast.success(t('users.quota.saved')); },
        onError: e => toast.error(getErrMsg(e)),
      }
    );
  };

  const fields: Array<{
    label: string; value: string; set: (v: string) => void; used: number; limit: number | null;
  }> = [
    { label: t('users.quota.instances'), value: instances, set: setInstances, used: data.used_instances, limit: parse(instances) },
    { label: t('users.quota.cores'), value: cores, set: setCores, used: data.used_cores, limit: parse(cores) },
    { label: t('users.quota.memory_mb'), value: memory, set: setMemory, used: data.used_memory_mb, limit: parse(memory) },
    { label: t('users.quota.disk_gb'), value: disk, set: setDisk, used: data.used_disk_gb, limit: parse(disk) },
  ];

  return (
    <>
      <div className="space-y-4">
        <p className="text-xs text-muted-foreground">{t('users.quota.hint')}</p>
        {fields.map(f => {
          const pct = f.limit && f.limit > 0 ? Math.min(100, (f.used / f.limit) * 100) : 0;
          const over = f.limit !== null && f.used > f.limit;
          return (
            <div key={f.label} className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label className="text-sm">{f.label}</Label>
                <span className={`text-xs ${over ? 'text-destructive' : 'text-muted-foreground'}`}>
                  {t('users.quota.used')}: {f.used}
                  {f.limit !== null ? ` / ${f.limit}` : ` / ${t('users.quota.unlimited')}`}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <Input
                  type="number"
                  min={0}
                  value={f.value}
                  onChange={e => f.set(e.target.value)}
                  placeholder={t('users.quota.unlimited')}
                  className="w-40"
                />
                <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                  <div
                    className={`h-full ${over ? 'bg-destructive' : 'bg-primary'}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
        <Button onClick={submit} disabled={setQuota.isPending}>{t('common.save')}</Button>
      </DialogFooter>
    </>
  );
}

function UserSSHKeysDialog({
  user, open, onOpenChange,
}: { user: User; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t('ssh_keys.manage_for_user')}: {user.username}</DialogTitle>
        </DialogHeader>
        <SSHKeysManager userId={user.id} admin />
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>{t('common.close')}</DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Roles Tab ====================

function RolesTab() {
  const { t } = useTranslation();
  const [createOpen, setCreateOpen] = useState(false);
  const [editRole, setEditRole] = useState<Role | null>(null);
  const { data: roles = [] } = useRoles();
  const deleteRole = useDeleteRole();

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm">{t('users.roles')}</CardTitle>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4 mr-1" />
            {t('users.create_role')}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('common.name')}</TableHead>
              <TableHead>{t('ipam.description')}</TableHead>
              <TableHead>{t('users.permissions_count')}</TableHead>
              <TableHead>{t('users.users_count')}</TableHead>
              <TableHead>{t('common.status')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {roles.map(r => {
              const enabledCount = Object.values(r.permissions || {}).filter(Boolean).length;
              return (
                <TableRow key={r.id}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      <Shield className="h-4 w-4" />
                      <div>
                        <div>{t(`perms.role.${r.name}.name`, r.display_name)}</div>
                        <div className="text-xs text-muted-foreground">{r.name}</div>
                      </div>
                      {r.is_system && (
                        <Badge variant="outline" className="text-xs">{t('users.system_role')}</Badge>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{t(`perms.role.${r.name}.desc`, r.description || '—')}</TableCell>
                  <TableCell>{enabledCount}</TableCell>
                  <TableCell>{r.user_count}</TableCell>
                  <TableCell>
                    <Badge variant={r.is_active ? 'default' : 'secondary'}>
                      {r.is_active ? t('users.active') : t('users.inactive')}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1 justify-end">
                      <Button
                        variant="ghost" size="icon" className="h-7 w-7"
                        title={t('users.edit_role')}
                        onClick={() => setEditRole(r)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      {!r.is_system && (
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7 text-destructive"
                          onClick={() => deleteRole.mutate(r.id, {
                            onError: e => toast.error(getErrMsg(e)),
                          })}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
            {roles.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                  {t('common.no_data')}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>

      <RoleDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        mode="create"
      />
      {editRole && (
        <RoleDialog
          open={!!editRole}
          onOpenChange={o => !o && setEditRole(null)}
          mode="edit"
          role={editRole}
        />
      )}
    </Card>
  );
}

// ==================== Role Dialog (create/edit) ====================

function RoleDialog({
  open, onOpenChange, mode, role,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  mode: 'create' | 'edit';
  role?: Role;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(role?.name || '');
  const [displayName, setDisplayName] = useState(role?.display_name || '');
  const [description, setDescription] = useState(role?.description || '');
  const [isActive, setIsActive] = useState(role?.is_active ?? true);
  const [perms, setPerms] = useState<Record<string, boolean>>(role?.permissions || {});
  const { data: permCategories = {} } = usePermissions();
  const createRole = useCreateRole();
  const updateRole = useUpdateRole();

  const allCodes = useMemo(
    () => Object.values(permCategories).flatMap(cat => Object.keys(cat)),
    [permCategories]
  );

  const togglePerm = (code: string) => {
    setPerms(p => ({ ...p, [code]: !p[code] }));
  };

  const setAll = (val: boolean) => {
    const next: Record<string, boolean> = {};
    allCodes.forEach(c => { next[c] = val; });
    setPerms(next);
  };

  const submit = () => {
    if (mode === 'create') {
      createRole.mutate(
        { name, display_name: displayName || name, description, permissions: perms },
        {
          onSuccess: () => { onOpenChange(false); toast.success(t('users.create_role')); },
          onError: e => toast.error(getErrMsg(e)),
        }
      );
    } else if (role) {
      updateRole.mutate(
        {
          id: role.id,
          display_name: displayName,
          description,
          permissions: perms,
          is_active: isActive,
        },
        {
          onSuccess: () => { onOpenChange(false); toast.success(t('common.save')); },
          onError: e => toast.error(getErrMsg(e)),
        }
      );
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {mode === 'create' ? t('users.create_role') : `${t('users.edit_role')}: ${role ? t(`perms.role.${role.name}.name`, role.display_name) : ''}`}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>{t('common.name')}</Label>
              <Input
                value={name}
                onChange={e => setName(e.target.value)}
                disabled={mode === 'edit'}
                className="mt-1"
              />
            </div>
            <div>
              <Label>{t('settings.display_name')}</Label>
              <Input value={displayName} onChange={e => setDisplayName(e.target.value)} className="mt-1" />
            </div>
          </div>
          <div>
            <Label>{t('ipam.description')}</Label>
            <Input value={description} onChange={e => setDescription(e.target.value)} className="mt-1" />
          </div>
          {mode === 'edit' && (
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <Checkbox checked={isActive} onChange={e => setIsActive(e.target.checked)} />
              {t('users.is_active')}
            </label>
          )}
          <div>
            <div className="flex items-center justify-between mb-2">
              <Label>{t('users.permissions')}</Label>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => setAll(true)}>{t('users.select_all')}</Button>
                <Button variant="outline" size="sm" onClick={() => setAll(false)}>{t('users.clear_all')}</Button>
              </div>
            </div>
            <div className="border rounded-md max-h-80 overflow-y-auto p-3 space-y-3">
              {Object.entries(permCategories).map(([cat, items]) => (
                <div key={cat}>
                  <div className="text-xs font-semibold text-muted-foreground mb-1.5 uppercase">
                    {t(`perms.cat.${cat}`, cat)}
                  </div>
                  <div className="grid grid-cols-2 gap-1">
                    {Object.entries(items).map(([code, label]) => (
                      <label key={code} className="flex items-center gap-2 text-sm cursor-pointer hover:bg-accent rounded px-2 py-1">
                        <Checkbox
                          checked={!!perms[code]}
                          onChange={() => togglePerm(code)}
                        />
                        <span className="flex-1 truncate" title={code}>
                          {t(`perms.label.${code.replace(/:/g, '_')}`, label as string)}
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
              {Object.keys(permCategories).length === 0 && (
                <p className="text-sm text-muted-foreground text-center py-4">{t('common.loading')}</p>
              )}
            </div>
          </div>
        </div>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
          <Button onClick={submit} disabled={createRole.isPending || updateRole.isPending || !name}>
            {mode === 'create' ? t('common.create') : t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Sessions Tab ====================

function SessionsTab() {
  const { t } = useTranslation();
  const { data: sessions = [] } = useSessions();
  const terminate = useTerminateSession();
  const terminateAll = useTerminateAllSessions();

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm">{t('users.active_sessions')}</CardTitle>
          <Button
            variant="destructive" size="sm"
            disabled={sessions.length === 0 || terminateAll.isPending}
            onClick={() => terminateAll.mutate(undefined, {
              onSuccess: r => toast.success(t('users.sessions_terminated', { count: r.count })),
              onError: e => toast.error(getErrMsg(e)),
            })}
          >
            <LogOut className="h-4 w-4 mr-1" />
            {t('users.terminate_all_sessions')}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('settings.username')}</TableHead>
              <TableHead>IP</TableHead>
              <TableHead>{t('users.device')}</TableHead>
              <TableHead>{t('users.created')}</TableHead>
              <TableHead>{t('users.last_activity')}</TableHead>
              <TableHead>{t('users.expires')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {sessions.map(s => (
              <TableRow key={s.id}>
                <TableCell className="font-medium">{s.username}</TableCell>
                <TableCell className="font-mono text-xs">{s.ip_address || '—'}</TableCell>
                <TableCell className="text-xs max-w-[240px] truncate" title={s.device_info}>
                  {s.device_info || '—'}
                </TableCell>
                <TableCell className="text-xs">{fmtDate(s.created_at)}</TableCell>
                <TableCell className="text-xs">{fmtDate(s.last_activity)}</TableCell>
                <TableCell className="text-xs">{fmtDate(s.expires_at)}</TableCell>
                <TableCell>
                  <Button
                    variant="ghost" size="icon" className="h-7 w-7 text-destructive"
                    onClick={() => terminate.mutate(s.id, {
                      onError: e => toast.error(getErrMsg(e)),
                    })}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {sessions.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                  {t('common.no_data')}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

// ==================== Security Tab ====================

function SecurityTab() {
  return (
    <div className="space-y-4">
      <SecuritySettingsCard />
      <BlockedIpsCard />
      <SecurityEventsCard />
    </div>
  );
}

function SecuritySettingsCard() {
  const { t } = useTranslation();
  const { data } = useSecuritySettings();

  if (!data) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-sm">{t('users.security_settings')}</CardTitle></CardHeader>
        <CardContent><p className="text-sm text-muted-foreground">{t('common.loading')}</p></CardContent>
      </Card>
    );
  }

  return <SecuritySettingsForm initial={data} />;
}

function SecuritySettingsForm({ initial }: { initial: SecuritySettings }) {
  const { t } = useTranslation();
  const update = useUpdateSecuritySettings();
  const [draft, setDraft] = useState<SecuritySettings>(initial);

  const setNum = (k: keyof SecuritySettings, v: string) => {
    setDraft({ ...draft, [k]: Number(v) || 0 });
  };
  const setBool = (k: keyof SecuritySettings, v: boolean) => {
    setDraft({ ...draft, [k]: v });
  };

  const save = () => {
    update.mutate(draft, {
      onSuccess: () => toast.success(t('common.save')),
      onError: e => toast.error(getErrMsg(e)),
    });
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm">{t('users.security_settings')}</CardTitle>
          <Button size="sm" onClick={save} disabled={update.isPending}>{t('common.save')}</Button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          <div>
            <Label className="text-xs">{t('users.max_login_attempts')}</Label>
            <Input type="number" value={draft.max_login_attempts} onChange={e => setNum('max_login_attempts', e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label className="text-xs">{t('users.lockout_duration')}</Label>
            <Input type="number" value={draft.lockout_duration_minutes} onChange={e => setNum('lockout_duration_minutes', e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label className="text-xs">{t('users.session_timeout')}</Label>
            <Input type="number" value={draft.session_timeout_minutes} onChange={e => setNum('session_timeout_minutes', e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label className="text-xs">{t('users.ip_block_threshold')}</Label>
            <Input type="number" value={draft.ip_block_threshold} onChange={e => setNum('ip_block_threshold', e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label className="text-xs">{t('users.ip_block_duration')}</Label>
            <Input type="number" value={draft.ip_block_duration_minutes} onChange={e => setNum('ip_block_duration_minutes', e.target.value)} className="mt-1" />
          </div>
          <div>
            <Label className="text-xs">{t('users.password_min_length')}</Label>
            <Input type="number" value={draft.password_min_length} onChange={e => setNum('password_min_length', e.target.value)} className="mt-1" />
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-4 pt-3 border-t">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <Checkbox checked={draft.password_require_uppercase} onChange={e => setBool('password_require_uppercase', e.target.checked)} />
            {t('users.password_require_uppercase')}
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <Checkbox checked={draft.password_require_lowercase} onChange={e => setBool('password_require_lowercase', e.target.checked)} />
            {t('users.password_require_lowercase')}
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <Checkbox checked={draft.password_require_numbers} onChange={e => setBool('password_require_numbers', e.target.checked)} />
            {t('users.password_require_numbers')}
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <Checkbox checked={draft.password_require_special} onChange={e => setBool('password_require_special', e.target.checked)} />
            {t('users.password_require_special')}
          </label>
        </div>
      </CardContent>
    </Card>
  );
}

function BlockedIpsCard() {
  const { t } = useTranslation();
  const { data: blocked = [] } = useBlockedIps();
  const blockIp = useBlockIp();
  const unblockIp = useUnblockIp();
  const [open, setOpen] = useState(false);
  const [ip, setIp] = useState('');
  const [reason, setReason] = useState('');
  const [duration, setDuration] = useState('');

  const submit = () => {
    blockIp.mutate(
      {
        ip_address: ip,
        reason: reason || undefined,
        duration_minutes: duration ? Number(duration) : undefined,
      },
      {
        onSuccess: r => {
          toast.success(r.message);
          setOpen(false); setIp(''); setReason(''); setDuration('');
        },
        onError: e => toast.error(getErrMsg(e)),
      }
    );
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm">{t('users.blocked_ips')}</CardTitle>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button size="sm" />}>
              <Ban className="h-4 w-4 mr-1" />
              {t('users.block_ip')}
            </DialogTrigger>
            <DialogContent className="max-w-sm">
              <DialogHeader><DialogTitle>{t('users.block_ip')}</DialogTitle></DialogHeader>
              <div className="space-y-3">
                <div>
                  <Label>{t('users.ip_address')}</Label>
                  <Input value={ip} onChange={e => setIp(e.target.value)} className="mt-1" placeholder="1.2.3.4" />
                </div>
                <div>
                  <Label>{t('users.reason')}</Label>
                  <Input value={reason} onChange={e => setReason(e.target.value)} className="mt-1" />
                </div>
                <div>
                  <Label>{t('users.duration_minutes')}</Label>
                  <Input type="number" value={duration} onChange={e => setDuration(e.target.value)} className="mt-1" />
                </div>
              </div>
              <DialogFooter>
                <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
                <Button onClick={submit} disabled={!ip || blockIp.isPending}>{t('users.block_ip')}</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('users.ip_address')}</TableHead>
              <TableHead>{t('users.reason')}</TableHead>
              <TableHead>{t('users.blocked_at')}</TableHead>
              <TableHead>{t('users.expires_at')}</TableHead>
              <TableHead>{t('common.status')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {blocked.map(b => (
              <TableRow key={b.id}>
                <TableCell className="font-mono text-xs">{b.ip_address}</TableCell>
                <TableCell className="text-xs">{b.reason || '—'}</TableCell>
                <TableCell className="text-xs">{fmtDate(b.blocked_at)}</TableCell>
                <TableCell className="text-xs">
                  {b.is_permanent ? (
                    <Badge variant="outline" className="text-xs">{t('users.permanent')}</Badge>
                  ) : (
                    fmtDate(b.expires_at)
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant={b.is_active ? 'destructive' : 'secondary'}>
                    {b.is_active ? t('users.active') : t('users.inactive')}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Button
                    variant="ghost" size="icon" className="h-7 w-7"
                    title={t('users.unblock')}
                    onClick={() => unblockIp.mutate(b.id, {
                      onSuccess: () => toast.success(t('users.unblock')),
                      onError: e => toast.error(getErrMsg(e)),
                    })}
                  >
                    <Unlock className="h-3.5 w-3.5" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {blocked.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                  {t('common.no_data')}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function SecurityEventsCard() {
  const { t } = useTranslation();
  const { data: events = [] } = useSecurityEvents(100);

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">{t('users.security_events')}</CardTitle></CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('users.created')}</TableHead>
              <TableHead>{t('users.event_type')}</TableHead>
              <TableHead>{t('settings.username')}</TableHead>
              <TableHead>IP</TableHead>
              <TableHead>{t('common.message') || 'Message'}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.map(e => (
              <TableRow key={e.id}>
                <TableCell className="text-xs whitespace-nowrap">{fmtDate(e.created_at)}</TableCell>
                <TableCell className="text-xs"><Badge variant="outline">{e.event_type}</Badge></TableCell>
                <TableCell className="text-xs">{e.username || '—'}</TableCell>
                <TableCell className="font-mono text-xs">{e.ip_address || '—'}</TableCell>
                <TableCell className="text-xs">{e.message || '—'}</TableCell>
              </TableRow>
            ))}
            {events.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                  {t('common.no_data')}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
