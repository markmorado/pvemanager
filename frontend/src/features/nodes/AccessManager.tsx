import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Trash2, RefreshCw, KeyRound, ShieldQuestion, Copy } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  useRealms, useCreateRealm, useDeleteRealm,
  useAccessUsers, useUserTokens, useCreateToken, useDeleteToken,
} from '@/hooks/use-access';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmDialog';

const REALM_TYPES = ['ldap', 'ad', 'openid'];

// ==================== Realm dialog ====================

function RealmDialog({ serverId, open, onClose }: { serverId: number; open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const createRealm = useCreateRealm(serverId);
  const [realm, setRealm] = useState('');
  const [type, setType] = useState('ldap');
  const [comment, setComment] = useState('');
  // type-specific
  const [server1, setServer1] = useState('');
  const [baseDn, setBaseDn] = useState('');
  const [userAttr, setUserAttr] = useState('uid');
  const [domain, setDomain] = useState('');
  const [issuerUrl, setIssuerUrl] = useState('');
  const [clientId, setClientId] = useState('');
  const [usernameClaim, setUsernameClaim] = useState('sub');
  const [secure, setSecure] = useState(true);

  // Fresh form on every opening.
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setRealm(''); setType('ldap'); setComment('');
      setServer1(''); setBaseDn(''); setUserAttr('uid'); setDomain('');
      setIssuerUrl(''); setClientId(''); setUsernameClaim('sub'); setSecure(true);
    }
  }

  const submit = () => {
    if (!realm.trim()) { toast.error(t('access.err_realm', 'Укажите имя realm')); return; }
    const data: Record<string, unknown> = { realm: realm.trim(), type };
    if (comment.trim()) data.comment = comment.trim();
    if (type === 'ldap') {
      data.server1 = server1.trim(); data.base_dn = baseDn.trim(); data.user_attr = userAttr.trim();
      data.secure = secure ? 1 : 0;
    } else if (type === 'ad') {
      data.server1 = server1.trim(); data.domain = domain.trim(); data.secure = secure ? 1 : 0;
    } else if (type === 'openid') {
      data['issuer-url'] = issuerUrl.trim(); data['client-id'] = clientId.trim();
      data['username-claim'] = usernameClaim.trim();
    }
    createRealm.mutate(data, {
      onSuccess: () => { toast.success(t('access.realm_created', 'Realm создан')); onClose(); },
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>{t('access.add_realm', 'Добавить realm')}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('access.realm_id', 'Имя (realm)')}</Label>
              <Input value={realm} onChange={(e) => setRealm(e.target.value)} className="font-mono" placeholder="company-ldap" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('access.type', 'Тип')}</Label>
              <Select value={type} onValueChange={(v) => { if (v) setType(v); }}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{REALM_TYPES.map(r => <SelectItem key={r} value={r}>{r.toUpperCase()}</SelectItem>)}</SelectContent>
              </Select>
            </div>
          </div>

          {(type === 'ldap' || type === 'ad') && (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label>{t('access.server', 'Сервер')}</Label>
                <Input value={server1} onChange={(e) => setServer1(e.target.value)} className="font-mono" placeholder="ldap.example.com" />
              </div>
              {type === 'ldap' && (
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5"><Label>Base DN</Label><Input value={baseDn} onChange={(e) => setBaseDn(e.target.value)} className="font-mono" placeholder="dc=example,dc=com" /></div>
                  <div className="space-y-1.5"><Label>{t('access.user_attr', 'User attribute')}</Label><Input value={userAttr} onChange={(e) => setUserAttr(e.target.value)} className="font-mono" /></div>
                </div>
              )}
              {type === 'ad' && (
                <div className="space-y-1.5"><Label>{t('access.domain', 'Домен')}</Label><Input value={domain} onChange={(e) => setDomain(e.target.value)} className="font-mono" placeholder="example.com" /></div>
              )}
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={secure} onChange={(e) => setSecure(e.target.checked)} />
                {t('access.secure', 'SSL/TLS (secure)')}
              </label>
            </div>
          )}

          {type === 'openid' && (
            <div className="space-y-3">
              <div className="space-y-1.5"><Label>Issuer URL</Label><Input value={issuerUrl} onChange={(e) => setIssuerUrl(e.target.value)} className="font-mono" placeholder="https://idp.example.com" /></div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5"><Label>Client ID</Label><Input value={clientId} onChange={(e) => setClientId(e.target.value)} className="font-mono" /></div>
                <div className="space-y-1.5"><Label>{t('access.username_claim', 'Username claim')}</Label><Input value={usernameClaim} onChange={(e) => setUsernameClaim(e.target.value)} className="font-mono" /></div>
              </div>
            </div>
          )}

          <div className="space-y-1.5"><Label>{t('common.comment', 'Комментарий')}</Label><Input value={comment} onChange={(e) => setComment(e.target.value)} /></div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
          <Button onClick={submit} disabled={createRealm.isPending}>{t('common.create', 'Создать')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ==================== Token create + secret reveal ====================

function TokenDialog({ serverId, userid, open, onClose }: { serverId: number; userid: string; open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const createToken = useCreateToken(serverId);
  const [tokenid, setTokenid] = useState('');
  const [comment, setComment] = useState('');
  const [privsep, setPrivsep] = useState(true);
  const [secret, setSecret] = useState<{ full: string; value: string } | null>(null);

  // Fresh form on every opening (and the previous secret is never shown again).
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) { setTokenid(''); setComment(''); setPrivsep(true); setSecret(null); }
  }

  const submit = () => {
    if (!tokenid.trim()) { toast.error(t('access.err_tokenid', 'Укажите ID токена')); return; }
    createToken.mutate(
      { userid, tokenid: tokenid.trim(), comment: comment.trim() || undefined, privsep },
      {
        onSuccess: (r) => {
          if (r.value && r.full_tokenid) setSecret({ full: r.full_tokenid, value: r.value });
          else { toast.success(t('access.token_created', 'Токен создан')); onClose(); }
        },
        onError: (e: Error) => toast.error(e.message),
      },
    );
  };

  const copy = (text: string) => { navigator.clipboard?.writeText(text); toast.success(t('common.copied', 'Скопировано')); };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('access.add_token', 'Создать API-токен')}</DialogTitle>
          <DialogDescription className="font-mono text-xs">{userid}</DialogDescription>
        </DialogHeader>

        {secret ? (
          <div className="space-y-3">
            <div className="rounded-md border border-warning/40 bg-warning/5 p-3 text-xs text-warning">
              {t('access.secret_warning', 'Секрет показывается только один раз — сохраните его сейчас.')}
            </div>
            <div className="space-y-1.5">
              <Label>Token ID</Label>
              <div className="flex gap-2">
                <Input readOnly value={secret.full} className="font-mono text-xs" />
                <Button size="icon" variant="outline" onClick={() => copy(secret.full)}><Copy className="h-4 w-4" /></Button>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>{t('access.secret', 'Секрет')}</Label>
              <div className="flex gap-2">
                <Input readOnly value={secret.value} className="font-mono text-xs" />
                <Button size="icon" variant="outline" onClick={() => copy(secret.value)}><Copy className="h-4 w-4" /></Button>
              </div>
            </div>
            <DialogFooter>
              <Button onClick={onClose}>{t('common.done', 'Готово')}</Button>
            </DialogFooter>
          </div>
        ) : (
          <>
            <div className="space-y-3">
              <div className="space-y-1.5"><Label>{t('access.token_id', 'ID токена')}</Label><Input value={tokenid} onChange={(e) => setTokenid(e.target.value)} className="font-mono" placeholder="automation" /></div>
              <div className="space-y-1.5"><Label>{t('common.comment', 'Комментарий')}</Label><Input value={comment} onChange={(e) => setComment(e.target.value)} /></div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={privsep} onChange={(e) => setPrivsep(e.target.checked)} />
                {t('access.privsep', 'Privilege separation (отдельные права токена)')}
              </label>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={onClose}>{t('common.cancel', 'Отмена')}</Button>
              <Button onClick={submit} disabled={createToken.isPending}>{t('common.create', 'Создать')}</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ==================== Tokens tab ====================

function TokensTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data: usersData } = useAccessUsers(serverId);
  const users = usersData?.users ?? [];
  const [useridPick, setUserid] = useState('');
  // The picked user, or the first one while none (or a removed one) is picked.
  const userid = users.some(u => u.userid === useridPick) ? useridPick : (users[0]?.userid ?? '');

  const { data: tokensData } = useUserTokens(serverId, userid);
  const tokens = tokensData?.tokens ?? [];
  const deleteToken = useDeleteToken(serverId, userid);
  const [dialog, setDialog] = useState(false);

  const remove = async (tid: string) => {
    if (!await confirm(`${t('access.delete_token', 'Удалить токен')} "${tid}"?`)) return;
    deleteToken.mutate(tid, { onSuccess: () => toast.success(t('access.token_deleted', 'Токен удалён')), onError: (e: Error) => toast.error(e.message) });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Select value={userid} onValueChange={(v) => { if (v) setUserid(v); }}>
          <SelectTrigger className="w-[240px]"><SelectValue placeholder={t('access.select_user', 'Пользователь')} /></SelectTrigger>
          <SelectContent>{users.map(u => <SelectItem key={u.userid} value={u.userid}>{u.userid}</SelectItem>)}</SelectContent>
        </Select>
        <Button size="sm" className="ml-auto" onClick={() => setDialog(true)} disabled={!userid}>
          <Plus className="mr-1 h-4 w-4" />{t('access.add_token', 'Создать токен')}
        </Button>
      </div>
      {tokens.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
      ) : (
        <Table>
          <TableHeader><TableRow>
            <TableHead>Token ID</TableHead><TableHead>{t('common.comment', 'Комментарий')}</TableHead>
            <TableHead>privsep</TableHead><TableHead>{t('access.expire', 'Истекает')}</TableHead>
            <TableHead className="text-right">{t('common.actions', 'Действия')}</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {tokens.map(tok => (
              <TableRow key={tok.tokenid}>
                <TableCell className="font-mono font-medium">{tok.tokenid}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{tok.comment || ''}</TableCell>
                <TableCell>{tok.privsep ? <Badge variant="outline">on</Badge> : <Badge variant="secondary">off</Badge>}</TableCell>
                <TableCell className="text-xs">{tok.expire ? new Date(tok.expire * 1000).toLocaleDateString() : '—'}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => remove(tok.tokenid)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <TokenDialog serverId={serverId} userid={userid} open={dialog} onClose={() => setDialog(false)} />
    </div>
  );
}

// ==================== Main ====================

export default function AccessManager({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const { data: realmsData, isLoading, refetch, isFetching } = useRealms(serverId);
  const deleteRealm = useDeleteRealm(serverId);
  const realms = realmsData?.realms ?? [];
  const [realmDialog, setRealmDialog] = useState(false);

  const removeRealm = async (r: string) => {
    if (!await confirm(`${t('access.delete_realm', 'Удалить realm')} "${r}"?`)) return;
    deleteRealm.mutate(r, { onSuccess: () => toast.success(t('access.realm_deleted', 'Realm удалён')), onError: (e: Error) => toast.error(e.message) });
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-lg">
          <KeyRound className="h-5 w-5" />{t('access.title', 'PVE Access')}
        </CardTitle>
        <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
        </Button>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="tokens">
          <TabsList>
            <TabsTrigger value="tokens">{t('access.tokens', 'API-токены')}</TabsTrigger>
            <TabsTrigger value="realms">{t('access.realms', 'Realms')} ({realms.length})</TabsTrigger>
          </TabsList>

          <TabsContent value="tokens" className="mt-4">
            <TokensTab serverId={serverId} />
          </TabsContent>

          <TabsContent value="realms" className="mt-4 space-y-3">
            <div className="flex justify-end">
              <Button size="sm" onClick={() => setRealmDialog(true)}>
                <Plus className="mr-1 h-4 w-4" />{t('access.add_realm', 'Добавить realm')}
              </Button>
            </div>
            {isLoading ? (
              <p className="py-6 text-center text-sm text-muted-foreground">{t('common.loading', 'Загрузка…')}</p>
            ) : realms.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
            ) : (
              <Table>
                <TableHeader><TableRow>
                  <TableHead>{t('access.realm_id', 'Realm')}</TableHead><TableHead>{t('access.type', 'Тип')}</TableHead>
                  <TableHead>TFA</TableHead><TableHead>{t('common.comment', 'Комментарий')}</TableHead>
                  <TableHead className="text-right">{t('common.actions', 'Действия')}</TableHead>
                </TableRow></TableHeader>
                <TableBody>
                  {realms.map(r => {
                    const builtin = r.type === 'pam' || r.type === 'pve';
                    return (
                      <TableRow key={r.realm}>
                        <TableCell className="font-mono font-medium">{r.realm}</TableCell>
                        <TableCell><Badge variant="outline">{r.type}</Badge></TableCell>
                        <TableCell className="text-xs">{r.tfa || '—'}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">{r.comment || ''}</TableCell>
                        <TableCell className="text-right">
                          {builtin ? (
                            <span className="text-xs text-muted-foreground flex items-center justify-end gap-1">
                              <ShieldQuestion className="h-3.5 w-3.5" />{t('access.builtin', 'встроенный')}
                            </span>
                          ) : (
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={() => removeRealm(r.realm)}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </TabsContent>
        </Tabs>
      </CardContent>

      <RealmDialog serverId={serverId} open={realmDialog} onClose={() => setRealmDialog(false)} />
    </Card>
  );
}
