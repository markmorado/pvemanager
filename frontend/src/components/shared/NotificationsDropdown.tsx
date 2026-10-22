import { useTranslation } from 'react-i18next';
import { Bell, Check, CheckCheck, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import type { Notification } from '@/types';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  useNotifications,
  useUnreadCount,
  useMarkRead,
  useMarkAllRead,
  useDeleteNotification,
} from '@/hooks/use-notifications';

export default function NotificationsDropdown() {
  const { t } = useTranslation();
  const { data: notifResponse } = useNotifications();
  const { data: countData } = useUnreadCount();
  const markRead = useMarkRead();
  const markAllRead = useMarkAllRead();
  const deleteNotification = useDeleteNotification();

  const notifications: Notification[] = notifResponse?.notifications ?? [];
  const unread = countData?.count ?? 0;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={
        <Button variant="ghost" size="icon" className="relative">
          <Bell className="h-4 w-4" />
          {unread > 0 && (
            <Badge
              variant="destructive"
              className="absolute -right-0.5 -top-0.5 h-4 min-w-4 px-1 text-2xs"
            >
              {unread > 99 ? '99+' : unread}
            </Badge>
          )}
        </Button>
      } />
      <DropdownMenuContent align="end" className="w-80 max-h-96 overflow-y-auto">
        <div className="flex items-center justify-between px-3 py-2">
          <span className="text-sm font-semibold">{t('notifications.title')}</span>
          {unread > 0 && (
            <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={() => markAllRead.mutate()}>
              <CheckCheck className="h-3 w-3 mr-1" />{t('notifications.mark_all_read')}
            </Button>
          )}
        </div>
        <DropdownMenuSeparator />
        {notifications.length === 0 ? (
          <div className="px-3 py-6 text-center text-sm text-muted-foreground">{t('notifications.empty')}</div>
        ) : (
          notifications.slice(0, 20).map(n => (
            // closeOnClick={false}: the mark-read / delete buttons live inside the
            // item, so a click on them must not close the whole menu (Base UI has
            // no Radix-style onSelect to cancel).
            <DropdownMenuItem key={n.id} className="flex flex-col items-start gap-1 p-3 cursor-default" closeOnClick={false}>
              <div className="flex w-full items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <p className={`text-sm leading-tight ${n.is_read ? 'text-muted-foreground' : 'font-medium'}`}>
                    {n.title || n.message}
                  </p>
                  {n.title && n.message && (
                    <p className="text-xs text-muted-foreground mt-0.5 line-clamp-2">{n.message}</p>
                  )}
                  <p className="text-2xs text-muted-foreground mt-1">
                    {n.created_at ? new Date(n.created_at).toLocaleString() : ''}
                  </p>
                </div>
                <div className="flex gap-0.5 shrink-0">
                  {!n.is_read && (
                    <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() => markRead.mutate(n.id)}>
                      <Check className="h-3 w-3" />
                    </Button>
                  )}
                  <Button variant="ghost" size="icon" className="h-6 w-6 text-destructive" onClick={() => deleteNotification.mutate(n.id)}>
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </div>
              </div>
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
