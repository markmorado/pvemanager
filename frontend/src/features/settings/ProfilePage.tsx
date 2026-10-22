import { useState, useEffect } from 'react';
import i18n from '@/lib/i18n';
import { useTranslation } from 'react-i18next';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { toast } from 'sonner';
import {
  useProfile, useUpdateProfile, useChangePassword,
  useMyQuota,
} from '@/hooks/use-settings';
import {
  useNotificationPreferences, useUpdateNotificationPreferences,
  useChannelsStatus, useSendTestNotification, useVerifyTelegramChat,
} from '@/hooks/use-notifications';
import { SSHKeysManager } from './SSHKeysManager';
import { apiClient } from '@/lib/api-client';
import { useAuthStore } from '@/stores/auth-store';

/**
 * Every user's own account: profile, password, 2FA, SSH keys, quota and
 * personal notification preferences. All of it is self-scoped — the endpoints
 * behind it only require a session, never a permission — so this page is
 * reachable by anyone who is logged in. Panel-wide configuration lives in
 * SettingsPage behind `setting:view`.
 */
export default function ProfilePage() {
  const { t } = useTranslation();

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('nav.profile')}</h1>
      <Tabs defaultValue="profile">
        <TabsList>
          <TabsTrigger value="profile">{t('settings.profile')}</TabsTrigger>
          <TabsTrigger value="ssh-keys">{t('ssh_keys.title')}</TabsTrigger>
          <TabsTrigger value="notifications">{t('settings.notifications')}</TabsTrigger>
        </TabsList>
        <TabsContent value="profile"><ProfileTab /></TabsContent>
        <TabsContent value="ssh-keys">
          <Card>
            <CardHeader><CardTitle className="text-sm">{t('ssh_keys.title')}</CardTitle></CardHeader>
            <CardContent><SSHKeysManager /></CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="notifications"><NotificationPreferencesCard /></TabsContent>
      </Tabs>
    </div>
  );
}

function ProfileTab() {
  const { t } = useTranslation();
  const { data: profile } = useProfile();
  const updateProfile = useUpdateProfile();
  const changePassword = useChangePassword();
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [lang, setLang] = useState('ru');
  const [currentPwd, setCurrentPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');

  // Refill from each newly loaded profile (also the one already cached on mount).
  const [syncedProfile, setSyncedProfile] = useState<typeof profile>(undefined);
  if (profile && profile !== syncedProfile) {
    setSyncedProfile(profile);
    setDisplayName(profile.full_name || '');
    setEmail(profile.email || '');
    setLang(profile.language || 'ru');
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader><CardTitle className="text-sm">{t('settings.profile_info')}</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div><Label>{t('settings.username')}</Label><Input value={profile?.username || ''} disabled className="mt-1" /></div>
          <div><Label>{t('settings.display_name')}</Label><Input value={displayName} onChange={e => setDisplayName(e.target.value)} className="mt-1" /></div>
          <div><Label>Email</Label><Input value={email} onChange={e => setEmail(e.target.value)} className="mt-1" /></div>
          <div>
            <Label>{t('settings.language')}</Label>
            <select value={lang} onChange={e => setLang(e.target.value)} className="mt-1 block w-full rounded-md border bg-background px-3 py-2 text-sm">
              <option value="ru">Русский</option>
              <option value="en">English</option>
            </select>
          </div>
          <Button size="sm" onClick={() => updateProfile.mutate({ full_name: displayName, email, language: lang }, {
            onSuccess: (r) => { i18n.changeLanguage(lang); toast.success(r.message); },
            onError: (e: Error) => toast.error(e.message),
          })}>{t('common.save')}</Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle className="text-sm">{t('settings.change_password')}</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div><Label>{t('settings.current_password')}</Label><Input type="password" value={currentPwd} onChange={e => setCurrentPwd(e.target.value)} className="mt-1" /></div>
          <div><Label>{t('settings.new_password')}</Label><Input type="password" value={newPwd} onChange={e => setNewPwd(e.target.value)} className="mt-1" /></div>
          <div><Label>{t('settings.confirm_password')}</Label><Input type="password" value={confirmPwd} onChange={e => setConfirmPwd(e.target.value)} className="mt-1" /></div>
          <Button size="sm" onClick={() => {
            if (newPwd !== confirmPwd) { toast.error(t('settings.password_mismatch')); return; }
            changePassword.mutate({ current_password: currentPwd, new_password: newPwd, confirm_password: confirmPwd }, {
              onSuccess: (r) => { toast.success(r.message); setCurrentPwd(''); setNewPwd(''); setConfirmPwd(''); },
              onError: (e: Error) => toast.error(e.message),
            });
          }}>{t('settings.change_password')}</Button>
        </CardContent>
      </Card>
      <TwoFactorCard />
      <QuotaCard />
    </div>
  );
}

interface TwoFactorStatus { enabled: boolean; backup_codes_remaining: number }
interface TwoFactorSetup { secret: string; otpauth_uri: string; qr_svg: string }

function TwoFactorCard() {
  const { t } = useTranslation();
  const loadUser = useAuthStore((s) => s.loadUser);
  const [status, setStatus] = useState<TwoFactorStatus | null>(null);
  const [setup, setSetup] = useState<TwoFactorSetup | null>(null);
  const [code, setCode] = useState('');
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  // Disable flow
  const [showDisable, setShowDisable] = useState(false);
  const [disablePwd, setDisablePwd] = useState('');
  const [disableCode, setDisableCode] = useState('');

  const refresh = () =>
    apiClient.get<TwoFactorStatus>('/api/auth/2fa/status').then(setStatus).catch(() => {});

  useEffect(() => { refresh(); }, []);

  const startSetup = async () => {
    setBusy(true);
    try {
      const data = await apiClient.post<TwoFactorSetup>('/api/auth/2fa/setup');
      setSetup(data);
      setBackupCodes(null);
      setCode('');
    } catch (e) { toast.error((e as Error).message); }
    finally { setBusy(false); }
  };

  const confirmEnable = async () => {
    setBusy(true);
    try {
      const res = await apiClient.post<{ backup_codes: string[] }>('/api/auth/2fa/enable', { code });
      setBackupCodes(res.backup_codes);
      setSetup(null);
      setCode('');
      await refresh();
      loadUser();
      toast.success(t('settings.tfa.enabled_ok'));
    } catch (e) { toast.error((e as Error).message); }
    finally { setBusy(false); }
  };

  const confirmDisable = async () => {
    setBusy(true);
    try {
      await apiClient.post('/api/auth/2fa/disable', { password: disablePwd, code: disableCode });
      setShowDisable(false);
      setDisablePwd('');
      setDisableCode('');
      setBackupCodes(null);
      await refresh();
      loadUser();
      toast.success(t('settings.tfa.disabled_ok'));
    } catch (e) { toast.error((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm flex items-center gap-2">
          {t('settings.tfa.title')}
          {status?.enabled
            ? <Badge variant="default">{t('settings.tfa.on')}</Badge>
            : <Badge variant="secondary">{t('settings.tfa.off')}</Badge>}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">{t('settings.tfa.description')}</p>

        {/* Freshly generated backup codes (shown once, right after enabling) */}
        {backupCodes && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 space-y-2">
            <p className="text-sm font-medium">{t('settings.tfa.backup_codes_title')}</p>
            <p className="text-xs text-muted-foreground">{t('settings.tfa.backup_codes_hint')}</p>
            <div className="grid grid-cols-2 gap-1.5 font-mono text-sm">
              {backupCodes.map((c) => <span key={c} className="select-all">{c}</span>)}
            </div>
            <Button size="sm" variant="outline" onClick={() => navigator.clipboard?.writeText(backupCodes.join('\n'))}>
              {t('settings.tfa.copy_codes')}
            </Button>
          </div>
        )}

        {/* Not enabled — offer setup */}
        {!status?.enabled && !setup && !backupCodes && (
          <Button size="sm" onClick={startSetup} disabled={busy}>{t('settings.tfa.enable')}</Button>
        )}

        {/* Setup in progress — QR + verify */}
        {setup && (
          <div className="space-y-3">
            <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center">
              <div
                className="h-40 w-40 rounded-md bg-white p-2 [&>svg]:h-full [&>svg]:w-full"
                dangerouslySetInnerHTML={{ __html: setup.qr_svg }}
              />
              <div className="space-y-1 text-xs">
                <p className="text-muted-foreground">{t('settings.tfa.scan_hint')}</p>
                <p className="text-muted-foreground">{t('settings.tfa.manual_key')}</p>
                <code className="select-all break-all font-mono text-foreground">{setup.secret}</code>
              </div>
            </div>
            <div className="max-w-xs space-y-1">
              <Label>{t('settings.tfa.enter_code')}</Label>
              <Input value={code} inputMode="numeric" onChange={(e) => setCode(e.target.value)} placeholder="123456" />
            </div>
            <div className="flex gap-2">
              <Button size="sm" onClick={confirmEnable} disabled={busy || code.length < 6}>{t('settings.tfa.verify_enable')}</Button>
              <Button size="sm" variant="ghost" onClick={() => { setSetup(null); setCode(''); }}>{t('common.cancel')}</Button>
            </div>
          </div>
        )}

        {/* Enabled — show remaining backup codes + disable */}
        {status?.enabled && !backupCodes && (
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {t('settings.tfa.backup_remaining', { count: status.backup_codes_remaining })}
            </p>
            {!showDisable ? (
              <Button size="sm" variant="destructive" onClick={() => setShowDisable(true)}>{t('settings.tfa.disable')}</Button>
            ) : (
              <div className="space-y-2 rounded-md border p-3">
                <div className="max-w-xs space-y-1">
                  <Label>{t('settings.current_password')}</Label>
                  <Input type="password" value={disablePwd} onChange={(e) => setDisablePwd(e.target.value)} />
                </div>
                <div className="max-w-xs space-y-1">
                  <Label>{t('settings.tfa.current_code')}</Label>
                  <Input value={disableCode} inputMode="numeric" onChange={(e) => setDisableCode(e.target.value)} placeholder="123456" />
                </div>
                <div className="flex gap-2">
                  <Button size="sm" variant="destructive" onClick={confirmDisable} disabled={busy || !disablePwd}>{t('settings.tfa.confirm_disable')}</Button>
                  <Button size="sm" variant="ghost" onClick={() => { setShowDisable(false); setDisablePwd(''); setDisableCode(''); }}>{t('common.cancel')}</Button>
                </div>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function QuotaCard() {
  const { t } = useTranslation();
  const { data: quota, isLoading } = useMyQuota();

  // Hide the card entirely if the user has no limits set (all unlimited)
  const hasAnyLimit = quota && (
    quota.max_instances !== null || quota.max_cores !== null ||
    quota.max_memory_mb !== null || quota.max_disk_gb !== null
  );
  if (isLoading || !quota || !hasAnyLimit) return null;

  const rows: Array<{ label: string; used: number; limit: number | null }> = [
    { label: t('users.quota.instances'), used: quota.used_instances, limit: quota.max_instances },
    { label: t('users.quota.cores'), used: quota.used_cores, limit: quota.max_cores },
    { label: t('users.quota.memory_mb'), used: quota.used_memory_mb, limit: quota.max_memory_mb },
    { label: t('users.quota.disk_gb'), used: quota.used_disk_gb, limit: quota.max_disk_gb },
  ];

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">{t('users.quota.manage')}</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        {rows.map(r => {
          const pct = r.limit && r.limit > 0 ? Math.min(100, (r.used / r.limit) * 100) : 0;
          const over = r.limit !== null && r.used > r.limit;
          return (
            <div key={r.label} className="space-y-1.5">
              <div className="flex items-center justify-between text-sm">
                <span>{r.label}</span>
                <span className={over ? 'text-destructive' : 'text-muted-foreground'}>
                  {r.used}{r.limit !== null ? ` / ${r.limit}` : ` / ${t('users.quota.unlimited')}`}
                </span>
              </div>
              {r.limit !== null && (
                <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                  <div className={`h-full ${over ? 'bg-destructive' : 'bg-primary'}`} style={{ width: `${pct}%` }} />
                </div>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

function Toggle({ id, checked, onChange, label }: { id: string; checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <Checkbox id={id} checked={checked} onChange={e => onChange(e.target.checked)} />
      <Label htmlFor={id} className="cursor-pointer">{label}</Label>
    </div>
  );
}

function NotificationPreferencesCard() {
  const { t } = useTranslation();
  const { data: prefs } = useNotificationPreferences();
  const update = useUpdateNotificationPreferences();
  const sendTest = useSendTestNotification();
  const verifyTg = useVerifyTelegramChat();
  const { data: channelsStatus } = useChannelsStatus();

  const [enabled, setEnabled] = useState(true);
  const [emailEnabled, setEmailEnabled] = useState(false);
  const [emailCritical, setEmailCritical] = useState(false);
  const [tgEnabled, setTgEnabled] = useState(false);
  const [tgChatId, setTgChatId] = useState('');
  const [webhook, setWebhook] = useState('');
  const [quietStart, setQuietStart] = useState('');
  const [quietEnd, setQuietEnd] = useState('');
  const [testChannel, setTestChannel] = useState('all');

  // Refill from each newly loaded preferences object (also a cached one on mount).
  const [syncedPrefs, setSyncedPrefs] = useState<typeof prefs>(undefined);
  if (prefs && prefs !== syncedPrefs) {
    setSyncedPrefs(prefs);
    setEnabled(prefs.enabled);
    setEmailEnabled(prefs.email_enabled);
    setEmailCritical(prefs.email_critical_only);
    setTgEnabled(prefs.telegram_enabled);
    setTgChatId(prefs.telegram_chat_id ?? '');
    setWebhook(prefs.webhook_url ?? '');
    setQuietStart(prefs.quiet_hours_start ?? '');
    setQuietEnd(prefs.quiet_hours_end ?? '');
  }

  const handleSave = () => {
    update.mutate({
      enabled, email_enabled: emailEnabled, email_critical_only: emailCritical,
      telegram_enabled: tgEnabled, telegram_chat_id: tgChatId || undefined,
      webhook_url: webhook || undefined,
      quiet_hours_start: quietStart || undefined, quiet_hours_end: quietEnd || undefined,
    }, {
      onSuccess: () => toast.success(t('settings.prefs_saved')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const handleVerify = () => {
    if (!tgChatId) { toast.error(t('settings.chat_id_required')); return; }
    verifyTg.mutate(tgChatId, {
      onSuccess: (r) => toast.success(r.message),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const handleTest = () => {
    sendTest.mutate(testChannel, {
      onSuccess: () => toast.success(t('settings.test_sent')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">{t('settings.my_notifications')}</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <Toggle id="np_enabled" checked={enabled} onChange={setEnabled} label={t('settings.notifications_enabled')} />
        <Toggle id="np_email" checked={emailEnabled} onChange={setEmailEnabled} label={t('settings.email_enabled')} />
        <Toggle id="np_email_crit" checked={emailCritical} onChange={setEmailCritical} label={t('settings.email_critical_only')} />
        <Toggle id="np_tg" checked={tgEnabled} onChange={setTgEnabled} label={t('settings.telegram_enabled')} />
        <div className="flex items-end gap-2">
          <div className="flex-1"><Label>Telegram Chat ID</Label><Input value={tgChatId} onChange={e => setTgChatId(e.target.value)} className="mt-1" /></div>
          <Button size="sm" variant="outline" onClick={handleVerify} disabled={verifyTg.isPending}>{t('settings.verify')}</Button>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div><Label>{t('settings.quiet_start')}</Label><Input value={quietStart} onChange={e => setQuietStart(e.target.value)} className="mt-1" placeholder="22:00" /></div>
          <div><Label>{t('settings.quiet_end')}</Label><Input value={quietEnd} onChange={e => setQuietEnd(e.target.value)} className="mt-1" placeholder="08:00" /></div>
        </div>
        <Button size="sm" onClick={handleSave} disabled={update.isPending}>{t('common.save')}</Button>

        <div className="flex items-center gap-2 border-t pt-3">
          <Select value={testChannel} onValueChange={v => { if (v) setTestChannel(v); }}>
            <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('common.all')}</SelectItem>
              <SelectItem value="email">Email</SelectItem>
              <SelectItem value="telegram">Telegram</SelectItem>
              <SelectItem value="inapp">In-app</SelectItem>
            </SelectContent>
          </Select>
          <Button size="sm" variant="outline" onClick={handleTest} disabled={sendTest.isPending}>{t('settings.send_test_notification')}</Button>
          {channelsStatus && (
            <span className="ml-auto text-xs text-muted-foreground">
              {Object.entries(channelsStatus).map(([k, v]) => {
                const conf = (v as { configured?: boolean })?.configured;
                return typeof conf === 'boolean' ? `${k}: ${conf ? '✓' : '✗'}` : null;
              }).filter(Boolean).join('  ')}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
