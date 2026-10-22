import { useEffect, useMemo, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useNavigate } from 'react-router';
import { Dialog as DialogPrimitive } from '@base-ui/react/dialog';
import { Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { DialogPortal, DialogOverlay } from '@/components/ui/dialog';
import { useAuthStore } from '@/stores/auth-store';
import { useCommandPaletteStore } from '@/stores/command-palette-store';
import { navGroups, canSeeNavItem, personalNavItems } from '@/lib/nav-items';
import { cn } from '@/lib/utils';

/**
 * Global Ctrl/Cmd+K palette for jumping between sections. Mounted once in
 * AppLayout; reuses the same navGroups/permission gating as the sidebar so
 * the two never drift out of sync.
 */
export default function CommandPalette() {
  const { open, setOpen, toggle } = useCommandPaletteStore();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { user } = useAuthStore();
  const isAdmin = !!user?.is_admin;
  const permissions = user?.permissions;

  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);

  useEffect(() => {
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        toggle();
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [toggle]);

  const flatItems = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = (item: { label: string; path: string }) => {
      if (!q) return true;
      const label = t(`nav.${item.label.toLowerCase()}`, item.label);
      return label.toLowerCase().includes(q) || item.path.toLowerCase().includes(q);
    };
    return [
      ...navGroups.flatMap((group) =>
        group.items
          .filter((item) => canSeeNavItem(item, isAdmin, permissions))
          .filter(matches)
          .map((item) => ({ ...item, group: group.title }))
      ),
      // Not permission-gated: reachable by anyone who is logged in.
      ...personalNavItems.filter(matches).map((item) => ({ ...item, group: 'Account' })),
    ];
  }, [query, isAdmin, permissions, t]);

  // Start every opening with an empty query. Done while rendering (not in an
  // effect, not in onOpenChange): Ctrl+K, the top-bar button and go() flip the
  // store directly and bypass onOpenChange, so the previous search used to
  // reappear on the next open.
  const [prevOpen, setPrevOpen] = useState(open);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (open) {
      setQuery('');
      setActiveIndex(0);
    }
  }

  const handleOpenChange = (next: boolean) => setOpen(next);

  const handleQueryChange = (value: string) => {
    setQuery(value);
    setActiveIndex(0);
  };

  const go = (path: string) => {
    setOpen(false);
    navigate(path);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, flatItems.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const item = flatItems[activeIndex];
      if (item) go(item.path);
    }
  };

  let groupCursor = '';

  return (
    <DialogPrimitive.Root open={open} onOpenChange={handleOpenChange}>
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Popup
          data-slot="command-palette"
          className="fixed top-24 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 overflow-hidden rounded-xl bg-popover text-popover-foreground shadow-2xl ring-1 ring-foreground/10 outline-none data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95"
        >
          <DialogPrimitive.Title className="sr-only">
            {t('nav.search', 'Search sections')}
          </DialogPrimitive.Title>

          <div className="flex items-center gap-2 border-b border-border px-3.5">
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
            <input
              autoFocus
              value={query}
              onChange={(e) => handleQueryChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={t('nav.searchPlaceholder', 'Jump to a section...')}
              className="h-11 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
            <kbd className="hidden shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-2xs text-muted-foreground sm:block">
              Esc
            </kbd>
          </div>

          <div className="max-h-80 overflow-y-auto p-1.5">
            {flatItems.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                {t('nav.searchEmpty', 'No results found')}
              </p>
            ) : (
              flatItems.map((item, i) => {
                const showGroupHeader = item.group !== groupCursor;
                groupCursor = item.group;
                const Icon = item.icon;
                return (
                  <div key={item.path}>
                    {showGroupHeader && (
                      <p className="mt-1.5 mb-1 px-2 text-2xs font-semibold uppercase tracking-wider text-muted-foreground first:mt-0">
                        {t(`nav.${item.group.toLowerCase()}`, item.group)}
                      </p>
                    )}
                    <button
                      type="button"
                      onMouseEnter={() => setActiveIndex(i)}
                      onClick={() => go(item.path)}
                      className={cn(
                        'flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-sm font-medium transition-colors',
                        i === activeIndex
                          ? 'bg-primary/10 text-primary'
                          : 'text-foreground hover:bg-accent'
                      )}
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                      {t(`nav.${item.label.toLowerCase()}`, item.label)}
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </DialogPrimitive.Popup>
      </DialogPortal>
    </DialogPrimitive.Root>
  );
}
