import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Save, Globe, Clock, FileText } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { toast } from 'sonner';
import {
  useNodeDns, useSetNodeDns, useNodeHosts, useSetNodeHosts, useNodeTime, useSetNodeTimezone,
} from '@/hooks/use-node-admin';

/** Системная конфигурация ноды: DNS, /etc/hosts, часовой пояс. */
export default function NodeSystemConfig({ serverId, node }: { serverId: number; node: string }) {
  const { t } = useTranslation();

  // --- DNS ---
  const { data: dnsData } = useNodeDns(serverId, node);
  const setDns = useSetNodeDns(serverId, node);
  const [dns, setDnsForm] = useState({ search: '', dns1: '', dns2: '', dns3: '' });
  // Each form below refills from every newly loaded response (also a cached one on mount).
  const [dnsFrom, setDnsFrom] = useState<typeof dnsData>(undefined);
  if (dnsData?.dns && dnsData !== dnsFrom) {
    setDnsFrom(dnsData);
    setDnsForm({
      search: dnsData.dns.search ?? '',
      dns1: dnsData.dns.dns1 ?? '',
      dns2: dnsData.dns.dns2 ?? '',
      dns3: dnsData.dns.dns3 ?? '',
    });
  }
  const saveDns = () => setDns.mutate(dns, {
    onSuccess: () => toast.success(t('nodeadm.dns_saved', 'DNS сохранён')),
    onError: (e: Error) => toast.error(e.message),
  });

  // --- Time ---
  const { data: timeData } = useNodeTime(serverId, node);
  const setTz = useSetNodeTimezone(serverId, node);
  const [tz, setTz2] = useState('');
  const [tzFrom, setTzFrom] = useState<typeof timeData>(undefined);
  if (timeData?.time?.timezone && timeData !== tzFrom) {
    setTzFrom(timeData);
    setTz2(timeData.time.timezone);
  }
  const saveTz = () => setTz.mutate(tz, {
    onSuccess: () => toast.success(t('nodeadm.tz_saved', 'Часовой пояс сохранён')),
    onError: (e: Error) => toast.error(e.message),
  });

  // --- Hosts ---
  const { data: hostsData } = useNodeHosts(serverId, node);
  const setHosts = useSetNodeHosts(serverId, node);
  const [hosts, setHostsForm] = useState('');
  const [hostsFrom, setHostsFrom] = useState<typeof hostsData>(undefined);
  if (hostsData?.hosts?.data != null && hostsData !== hostsFrom) {
    setHostsFrom(hostsData);
    setHostsForm(hostsData.hosts.data);
  }
  const saveHosts = () => setHosts.mutate({ data: hosts, digest: hostsData?.hosts?.digest }, {
    onSuccess: () => toast.success(t('nodeadm.hosts_saved', '/etc/hosts сохранён')),
    onError: (e: Error) => toast.error(e.message),
  });

  const nodeTime = timeData?.time?.localtime ? new Date(timeData.time.localtime * 1000).toLocaleString() : '—';

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      {/* DNS */}
      <div className="space-y-3 rounded-lg border p-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold"><Globe className="h-4 w-4" />DNS</h3>
        <div className="space-y-2">
          <Label className="text-xs">{t('nodeadm.dns_search', 'Search domain')}</Label>
          <Input value={dns.search} onChange={(e) => setDnsForm({ ...dns, search: e.target.value })} className="h-8" />
          <Label className="text-xs">DNS 1</Label>
          <Input value={dns.dns1} onChange={(e) => setDnsForm({ ...dns, dns1: e.target.value })} className="h-8 font-mono" />
          <Label className="text-xs">DNS 2</Label>
          <Input value={dns.dns2} onChange={(e) => setDnsForm({ ...dns, dns2: e.target.value })} className="h-8 font-mono" />
          <Label className="text-xs">DNS 3</Label>
          <Input value={dns.dns3} onChange={(e) => setDnsForm({ ...dns, dns3: e.target.value })} className="h-8 font-mono" />
        </div>
        <Button size="sm" onClick={saveDns} disabled={setDns.isPending}>
          <Save className="mr-1 h-4 w-4" />{t('common.save', 'Сохранить')}
        </Button>
      </div>

      {/* Time */}
      <div className="space-y-3 rounded-lg border p-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold"><Clock className="h-4 w-4" />{t('nodeadm.time', 'Время')}</h3>
        <p className="text-xs text-muted-foreground">
          {t('nodeadm.node_time', 'Локальное время ноды')}: <span className="font-mono">{nodeTime}</span>
        </p>
        <div className="space-y-2">
          <Label className="text-xs">{t('nodeadm.timezone', 'Часовой пояс')}</Label>
          <Input value={tz} onChange={(e) => setTz2(e.target.value)} className="h-8 font-mono" placeholder="Europe/Moscow" />
        </div>
        <Button size="sm" onClick={saveTz} disabled={setTz.isPending}>
          <Save className="mr-1 h-4 w-4" />{t('common.save', 'Сохранить')}
        </Button>
      </div>

      {/* Hosts */}
      <div className="space-y-3 rounded-lg border p-4 lg:col-span-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold"><FileText className="h-4 w-4" />/etc/hosts</h3>
        <textarea
          value={hosts}
          onChange={(e) => setHostsForm(e.target.value)}
          spellCheck={false}
          className="h-56 w-full rounded-md border bg-background p-3 font-mono text-xs leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button size="sm" onClick={saveHosts} disabled={setHosts.isPending}>
          <Save className="mr-1 h-4 w-4" />{t('common.save', 'Сохранить')}
        </Button>
      </div>
    </div>
  );
}
