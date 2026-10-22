import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ShieldCheck, Upload, Trash2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  useNodeCertificates, useUploadNodeCertificate, useDeleteNodeCertificate,
  type NodeCertificate,
} from '@/hooks/use-node-admin';
import { useConfirm } from '@/components/shared/ConfirmDialog';
import { useHasPermission } from '@/lib/permissions';

/** Форматирование unix-времени (сек) в локальную дату; пустое — прочерк. */
function fmtDate(ts?: number): string {
  if (!ts) return '—';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return '—';
  }
}

/** Дней до истечения (может быть отрицательным — уже просрочен). */
function daysLeft(notafter?: number): number | null {
  if (!notafter) return null;
  return Math.floor((notafter * 1000 - Date.now()) / 86_400_000);
}

function ExpiryBadge({ notafter }: { notafter?: number }) {
  const { t } = useTranslation();
  const d = daysLeft(notafter);
  if (d === null) return null;
  if (d < 0) return <Badge variant="destructive">{t('certs.expired', 'Просрочен')}</Badge>;
  if (d < 30) return <Badge variant="destructive">{t('certs.days_left', '{{d}} дн.', { d })}</Badge>;
  return <Badge variant="secondary">{t('certs.days_left', '{{d}} дн.', { d })}</Badge>;
}

export default function NodeCertificates({ serverId, node }: { serverId: number; node: string }) {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const canManage = useHasPermission('node:manage');

  const { data, isFetching, refetch } = useNodeCertificates(serverId, node);
  const upload = useUploadNodeCertificate(serverId, node);
  const remove = useDeleteNodeCertificate(serverId, node);

  const [showUpload, setShowUpload] = useState(false);
  const [cert, setCert] = useState('');
  const [key, setKey] = useState('');

  const certs = data?.certificates ?? [];

  const doUpload = () => {
    if (!cert.trim()) {
      toast.error(t('certs.cert_required', 'Вставьте сертификат в формате PEM'));
      return;
    }
    upload.mutate(
      { certificates: cert, key: key.trim() || undefined, force: true, restart: true },
      {
        onSuccess: () => {
          toast.success(t('certs.uploaded', 'Сертификат загружен, pveproxy перезапущен'));
          setShowUpload(false);
          setCert('');
          setKey('');
        },
        onError: (e: Error) => toast.error(e.message),
      },
    );
  };

  const doDelete = async () => {
    const ok = await confirm(
      t('certs.confirm_delete', 'Удалить пользовательский сертификат ноды «{{node}}»? Будет восстановлен самоподписанный, pveproxy перезапустится.', { node }),
      { title: t('certs.delete', 'Удалить сертификат'), variant: 'destructive' },
    );
    if (!ok) return;
    remove.mutate(true, {
      onSuccess: () => toast.success(t('certs.deleted', 'Сертификат удалён, восстановлен самоподписанный')),
      onError: (e: Error) => toast.error(e.message),
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <ShieldCheck className="h-4 w-4" />{t('certs.title', 'TLS-сертификаты')}
        </h3>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </Button>
          {canManage && (
            <Button size="sm" onClick={() => setShowUpload((v) => !v)}>
              <Upload className="mr-1 h-4 w-4" />{t('certs.upload', 'Загрузить сертификат')}
            </Button>
          )}
        </div>
      </div>

      {showUpload && canManage && (
        <div className="space-y-3 rounded-lg border p-4">
          <div className="space-y-1.5">
            <label className="text-xs font-medium">{t('certs.pem_cert', 'Сертификат (PEM, с цепочкой)')}</label>
            <textarea
              value={cert}
              onChange={(e) => setCert(e.target.value)}
              spellCheck={false}
              placeholder="-----BEGIN CERTIFICATE-----"
              className="h-40 w-full rounded-md border bg-background p-3 font-mono text-xs leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium">{t('certs.pem_key', 'Приватный ключ (PEM, опционально)')}</label>
            <textarea
              value={key}
              onChange={(e) => setKey(e.target.value)}
              spellCheck={false}
              placeholder="-----BEGIN PRIVATE KEY-----"
              className="h-32 w-full rounded-md border bg-background p-3 font-mono text-xs leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={doUpload} disabled={upload.isPending}>
              <Upload className="mr-1 h-4 w-4" />{t('certs.apply', 'Применить')}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setShowUpload(false)}>
              {t('common.cancel', 'Отмена')}
            </Button>
          </div>
        </div>
      )}

      {certs.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">{t('common.no_data', 'Нет данных')}</p>
      ) : (
        <div className="space-y-3">
          {certs.map((c: NodeCertificate) => {
            const isCustom = c.filename === 'pveproxy-ssl.pem';
            return (
              <div key={c.filename || c.fingerprint} className="space-y-2 rounded-lg border p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-medium">{c.filename || '—'}</span>
                  <Badge variant={isCustom ? 'default' : 'secondary'}>
                    {isCustom ? t('certs.custom', 'Пользовательский') : t('certs.builtin', 'Самоподписанный')}
                  </Badge>
                  <ExpiryBadge notafter={c.notafter} />
                  {canManage && isCustom && (
                    <Button
                      size="sm" variant="outline"
                      className="ml-auto h-7 text-destructive"
                      onClick={doDelete} disabled={remove.isPending}
                    >
                      <Trash2 className="mr-1 h-3.5 w-3.5" />{t('certs.delete', 'Удалить')}
                    </Button>
                  )}
                </div>
                <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
                  <Row label={t('certs.subject', 'Subject')} value={c.subject} mono />
                  <Row label={t('certs.issuer', 'Issuer')} value={c.issuer} mono />
                  <Row label={t('certs.notbefore', 'Действителен с')} value={fmtDate(c.notbefore)} />
                  <Row label={t('certs.notafter', 'Действителен до')} value={fmtDate(c.notafter)} />
                  <Row label={t('certs.pubkey', 'Ключ')} value={c['public-key-type'] ? `${c['public-key-type']} ${c['public-key-bits'] || ''}` : undefined} />
                  <Row label={t('certs.fingerprint', 'Отпечаток')} value={c.fingerprint} mono />
                  {c.san && c.san.length > 0 && (
                    <Row label="SAN" value={c.san.join(', ')} mono />
                  )}
                </dl>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value?: string; mono?: boolean }) {
  if (!value) return null;
  return (
    <div className="flex gap-2">
      <dt className="shrink-0 text-muted-foreground">{label}:</dt>
      <dd className={`min-w-0 break-all ${mono ? 'font-mono' : ''}`}>{value}</dd>
    </div>
  );
}
