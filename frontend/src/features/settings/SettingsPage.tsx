import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { toast } from 'sonner';
import {
  usePanelSettings, useUpdatePanelSettings,
  useSecuritySettings, useUpdateSecuritySettings,
  useNotificationChannels, useUpdateSMTP, useUpdateTelegram,
  useAppVersion, useCheckUpdates,
  useUpdateRepository, useSetUpdateRepository, useUpdateStatus, usePerformUpdate, useResetUpdate,
  useTestSMTP,
} from '@/hooks/use-settings';
import { useTelegramBotInfo } from '@/hooks/use-notifications';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { useHasPermission } from '@/lib/permissions';

/**
 * Panel-wide configuration, reachable with `setting:view`. Each tab is gated on
 * the permission its own API requires, so a role that may only read the panel
 * settings does not get security or notification-channel forms it cannot use.
 * A user's own account settings live in ProfilePage, which needs no permission.
 */
export default function SettingsPage() {
  const { t } = useTranslation();
  const canEditPanel = useHasPermission('setting:update');
  const canManageSecurity = useHasPermission('setting:manage');
  // The SMTP/Telegram endpoints check `is_admin` literally, not a permission.
  const isAdmin = useHasPermission();

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('nav.settings')}</h1>
      <Tabs defaultValue="panel">
        <TabsList>
          <TabsTrigger value="panel">{t('settings.panel')}</TabsTrigger>
          {canManageSecurity && <TabsTrigger value="security">{t('settings.security')}</TabsTrigger>}
          {isAdmin && <TabsTrigger value="notifications">{t('settings.notification_channels')}</TabsTrigger>}
          <TabsTrigger value="about">{t('settings.about')}</TabsTrigger>
        </TabsList>
        <TabsContent value="panel"><PanelTab canEdit={canEditPanel} /></TabsContent>
        {canManageSecurity && <TabsContent value="security"><SecurityTab /></TabsContent>}
        {isAdmin && <TabsContent value="notifications"><NotificationChannelsCards /></TabsContent>}
        <TabsContent value="about"><AboutTab /></TabsContent>
      </Tabs>
    </div>
  );
}

/** `setting:view` shows the panel settings; saving them needs `setting:update`. */
function PanelTab({ canEdit }: { canEdit: boolean }) {
  const { t } = useTranslation();
  const { data: settings } = usePanelSettings();
  const updatePanel = useUpdatePanelSettings();
  const [logRetention, setLogRetention] = useState('30');

  // Refill from each newly loaded response (also a cached one on mount).
  const [syncedFrom, setSyncedFrom] = useState<typeof settings>(undefined);
  if (settings && settings !== syncedFrom) {
    setSyncedFrom(settings);
    setLogRetention(String(settings.log_retention_days || 30));
  }

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">{t('settings.panel_settings')}</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div><Label>{t('settings.panel_name')}</Label><Input value={settings?.panel_name || ''} disabled className="mt-1" /></div>
        <div>
          <Label>{t('settings.log_retention_days')}</Label>
          <Input type="number" value={logRetention} onChange={e => setLogRetention(e.target.value)} className="mt-1" min={1} max={365} disabled={!canEdit} />
          <p className="text-xs text-muted-foreground mt-1">{t('settings.days')}</p>
        </div>
        {canEdit && (
          <Button size="sm" onClick={() => updatePanel.mutate({ log_retention_days: Number(logRetention) }, {
            onSuccess: (r) => toast.success(r.message),
            onError: (e: Error) => toast.error(e.message),
          })}>{t('common.save')}</Button>
        )}
      </CardContent>
    </Card>
  );
}

function SecurityTab() {
  const { t } = useTranslation();
  const { data: security } = useSecuritySettings();
  const updateSecurity = useUpdateSecuritySettings();
  const [maxAttempts, setMaxAttempts] = useState('');
  const [lockoutDuration, setLockoutDuration] = useState('');
  const [sessionTimeout, setSessionTimeout] = useState('');

  // Refill from each newly loaded response (also a cached one on mount).
  const [syncedFrom, setSyncedFrom] = useState<typeof security>(undefined);
  if (security && security !== syncedFrom) {
    setSyncedFrom(security);
    setMaxAttempts(String(security.max_login_attempts ?? 5));
    setLockoutDuration(String(security.lockout_duration_minutes ?? 30));
    setSessionTimeout(String(security.session_timeout_minutes ?? 60));
  }

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">{t('settings.security_settings')}</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div><Label>{t('settings.max_attempts')}</Label><Input type="number" value={maxAttempts} onChange={e => setMaxAttempts(e.target.value)} className="mt-1" /></div>
        <div><Label>{t('settings.lockout_duration')}</Label><Input type="number" value={lockoutDuration} onChange={e => setLockoutDuration(e.target.value)} className="mt-1" /><p className="text-xs text-muted-foreground mt-1">{t('settings.minutes')}</p></div>
        <div><Label>{t('settings.session_timeout')}</Label><Input type="number" value={sessionTimeout} onChange={e => setSessionTimeout(e.target.value)} className="mt-1" /><p className="text-xs text-muted-foreground mt-1">{t('settings.minutes')}</p></div>
        <Button size="sm" onClick={() => updateSecurity.mutate({
          max_login_attempts: Number(maxAttempts),
          lockout_duration_minutes: Number(lockoutDuration),
          session_timeout_minutes: Number(sessionTimeout),
        }, {
          onSuccess: () => toast.success(t('settings.security_saved')),
          onError: (e: Error) => toast.error(e.message),
        })}>{t('common.save')}</Button>
      </CardContent>
    </Card>
  );
}

function NotificationChannelsCards() {
  const { t } = useTranslation();
  const { data: channels } = useNotificationChannels();
  const updateSmtp = useUpdateSMTP();
  const updateTg = useUpdateTelegram();
  const [smtpHost, setSmtpHost] = useState('');
  const [smtpPort, setSmtpPort] = useState('');
  const [smtpUser, setSmtpUser] = useState('');
  const [smtpPass, setSmtpPass] = useState('');
  const [smtpFrom, setSmtpFrom] = useState('');
  const [tgToken, setTgToken] = useState('');

  // Each block refills from every newly loaded channel config (also a cached one on mount).
  const smtp = channels?.smtp;
  const [smtpSyncedFrom, setSmtpSyncedFrom] = useState<typeof smtp>(undefined);
  if (smtp && smtp !== smtpSyncedFrom) {
    setSmtpSyncedFrom(smtp);
    setSmtpHost(String(smtp.host ?? ''));
    setSmtpPort(String(smtp.port ?? 587));
    setSmtpUser(String(smtp.username ?? ''));
    setSmtpFrom(String(smtp.from_email ?? ''));
  }

  const telegram = channels?.telegram;
  const [tgSyncedFrom, setTgSyncedFrom] = useState<typeof telegram>(undefined);
  if (telegram && telegram !== tgSyncedFrom) {
    setTgSyncedFrom(telegram);
    setTgToken(String(telegram.telegram_bot_token ?? telegram.bot_token ?? ''));
  }

  const testSmtp = useTestSMTP();
  const botInfo = useTelegramBotInfo();
  const [testEmail, setTestEmail] = useState('');

  const handleTestSmtp = () => {
    if (!testEmail) { toast.error(t('settings.test_email_required')); return; }
    testSmtp.mutate(testEmail, {
      onSuccess: (r) => toast.success(r.message),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const bot = botInfo.data?.bot_info as { username?: string; first_name?: string } | null | undefined;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader><CardTitle className="text-sm">SMTP</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="grid sm:grid-cols-2 gap-3">
            <div><Label>Host</Label><Input value={smtpHost} onChange={e => setSmtpHost(e.target.value)} className="mt-1" /></div>
            <div><Label>Port</Label><Input type="number" value={smtpPort} onChange={e => setSmtpPort(e.target.value)} className="mt-1" /></div>
            <div><Label>Username</Label><Input value={smtpUser} onChange={e => setSmtpUser(e.target.value)} className="mt-1" /></div>
            <div><Label>Password</Label><Input type="password" value={smtpPass} onChange={e => setSmtpPass(e.target.value)} className="mt-1" /></div>
            <div><Label>From Email</Label><Input value={smtpFrom} onChange={e => setSmtpFrom(e.target.value)} className="mt-1" /></div>
          </div>
          <Button size="sm" onClick={() => updateSmtp.mutate({ host: smtpHost, port: Number(smtpPort), username: smtpUser, password: smtpPass || undefined, from_email: smtpFrom }, {
            onSuccess: (r) => toast.success(r.message),
            onError: (e: Error) => toast.error(e.message),
          })}>{t('common.save')}</Button>
          <div className="flex items-center gap-2 border-t pt-3">
            <Input value={testEmail} onChange={e => setTestEmail(e.target.value)} className="max-w-xs" placeholder={t('settings.test_email_placeholder')} />
            <Button size="sm" variant="outline" onClick={handleTestSmtp} disabled={testSmtp.isPending}>{t('settings.send_test')}</Button>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center gap-2">
            Telegram
            {bot?.username && <Badge variant="outline">@{bot.username}</Badge>}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div><Label>Bot Token</Label><Input value={tgToken} onChange={e => setTgToken(e.target.value)} className="mt-1" /></div>
          <Button size="sm" onClick={() => updateTg.mutate({ telegram_bot_token: tgToken }, {
            onSuccess: (r) => toast.success(r.message),
            onError: (e: Error) => toast.error(e.message),
          })}>{t('common.save')}</Button>
        </CardContent>
      </Card>
    </div>
  );
}


function AboutTab() {
  const { t } = useTranslation();
  const { data: version } = useAppVersion();
  // Everyone may see which version they are on; the update machinery behind it
  // is admin territory (`setting:manage` on the backend).
  const canUpdate = useHasPermission('setting:manage');

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">{t('settings.about')}</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">{t('settings.version')}:</span>
          <Badge variant="outline">{version?.version || '—'}</Badge>
        </div>
        {canUpdate && <UpdateSection />}
      </CardContent>
    </Card>
  );
}

function UpdateSection() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const checkUpdates = useCheckUpdates();
  const { data: repo } = useUpdateRepository();
  const setRepo = useSetUpdateRepository();
  const performUpdate = usePerformUpdate();
  const resetUpdate = useResetUpdate();

  const check = checkUpdates.data;
  const updating = check?.update_available ?? false;
  // Poll the update status only while an update is running.
  const { data: status } = useUpdateStatus(performUpdate.isSuccess);

  const [repoUrl, setRepoUrl] = useState('');
  const [repoEdit, setRepoEdit] = useState(false);

  const storedRepoUrl = repo?.repository_url;
  const [repoSyncedFrom, setRepoSyncedFrom] = useState<string | undefined>(undefined);
  if (storedRepoUrl && storedRepoUrl !== repoSyncedFrom) {
    setRepoSyncedFrom(storedRepoUrl);
    setRepoUrl(storedRepoUrl);
  }

  const handleSaveRepo = () => {
    setRepo.mutate(repoUrl, {
      onSuccess: () => { toast.success(t('settings.repo_saved')); setRepoEdit(false); },
      onError: (e: Error) => toast.error(e.message),
    });
  };

  const handlePerform = async () => {
    if (!await confirm(t('settings.update_confirm'))) return;
    performUpdate.mutate(undefined, {
      onSuccess: (r) => r.success ? toast.success(r.message || t('settings.update_started')) : toast.error(r.error || 'Error'),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <>
    {/* Repository */}
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{t('settings.update_repository')}</Label>
      {repoEdit ? (
        <div className="flex items-center gap-2">
          <Input value={repoUrl} onChange={e => setRepoUrl(e.target.value)} placeholder="https://git.example.com/owner/repo" />
          <Button size="sm" onClick={handleSaveRepo} disabled={setRepo.isPending || !repoUrl}>{t('common.save')}</Button>
          <Button size="sm" variant="ghost" onClick={() => { setRepoEdit(false); setRepoUrl(repo?.repository_url || ''); }}>{t('common.cancel')}</Button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <code className="rounded bg-muted px-2 py-1 text-xs">{repo?.repository_url || '—'}</code>
          <Button size="sm" variant="ghost" onClick={() => setRepoEdit(true)}>{t('common.edit', 'Edit')}</Button>
        </div>
      )}
    </div>

    {/* Check */}
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" variant="outline" onClick={() => checkUpdates.mutate()} disabled={checkUpdates.isPending}>
        {t('settings.check_updates')}
      </Button>
      {updating && (
        <Button size="sm" onClick={handlePerform} disabled={performUpdate.isPending || status?.is_updating}>
          {t('settings.perform_update')}
        </Button>
      )}
    </div>

    {check && (
      <div className="space-y-1 text-sm">
        {check.error ? (
          <p className="text-destructive">{check.error}</p>
        ) : check.update_available ? (
          <p className="font-medium text-warning">
            {t('settings.update_available')}: {check.latest_version}
          </p>
        ) : (
          <p className="text-muted-foreground">{t('settings.up_to_date')}</p>
        )}
        {check.changelog && (
          <pre className="mt-2 max-h-48 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap">{check.changelog}</pre>
        )}
      </div>
    )}

    {/* Update in progress / failed banner */}
    {status && (status.is_updating || status.stage === 'failed' || status.error) && (
      <div className={`rounded-md border p-3 text-sm ${
        status.stage === 'failed' || status.error
          ? 'border-destructive/40 bg-destructive/10'
          : 'border-warning/40 bg-warning/10'
      }`}>
        {status.stage === 'failed' || status.error ? (
          <>
            <p className="font-medium text-destructive">{t('settings.update_failed')}</p>
            {status.error && <p className="mt-1 text-xs text-muted-foreground">{status.error}</p>}
          </>
        ) : (
          <>
            <p className="font-medium">{t('settings.update_in_progress')}</p>
            <p className="text-xs text-muted-foreground">{status.stage} — {status.progress}%</p>
          </>
        )}
        <Button size="sm" variant="ghost" className="mt-2" onClick={() => resetUpdate.mutate()} disabled={resetUpdate.isPending}>
          {t('settings.update_reset')}
        </Button>
      </div>
    )}
    </>
  );
}
