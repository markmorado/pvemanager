import { Link, useLocation, Outlet, useNavigate } from 'react-router';
import {
  Sun,
  Moon,
  Menu,
  LogOut,
  Languages,
  ChevronDown,
  Building2,
  Check,
  Search,
  FolderKanban,
  UserCog,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetTrigger } from '@/components/ui/sheet';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Separator } from '@/components/ui/separator';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useAuthStore } from '@/stores/auth-store';
import { useThemeStore } from '@/stores/theme-store';
import { useCommandPaletteStore } from '@/stores/command-palette-store';
import { useWorkspaceStore } from '@/stores/workspace-store';
import { useWorkspaces } from '@/hooks/use-workspaces';
import { useGlobalRealtimeSync } from '@/hooks/use-realtime-sync';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import NotificationsDropdown from '@/components/shared/NotificationsDropdown';
import ActiveTasksIndicator from '@/components/shared/ActiveTasksIndicator';
import CommandPalette from '@/components/shared/CommandPalette';
import MinimizedOpsTray from '@/components/shared/MinimizedOpsTray';
import { navGroups, canSeeNavItem } from '@/lib/nav-items';

function SidebarContent() {
  const location = useLocation();
  const { t } = useTranslation();
  const { user } = useAuthStore();
  const isAdmin = !!user?.is_admin;
  const permissions = user?.permissions;

  const visibleGroups = navGroups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => canSeeNavItem(item, isAdmin, permissions)),
    }))
    .filter((group) => group.items.length > 0);

  return (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="flex h-20 items-center gap-2 px-4">
        <img src="/logo.png" alt="PVEmanager" className="h-16 w-auto" />
      </div>

      <Separator />

      {/* Navigation */}
      <nav className="flex-1 space-y-4 overflow-y-auto px-3 py-4">
        {visibleGroups.map((group) => (
          <div key={group.title}>
            <p className="mb-1.5 px-2 text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
              {t(`nav.${group.title.toLowerCase()}`, group.title)}
            </p>
            <div className="space-y-0.5">
              {group.items.map((item) => {
                const isActive = location.pathname === item.path ||
                  (item.path !== '/dashboard' && location.pathname.startsWith(item.path));
                const Icon = item.icon;
                return (
                  <Link
                    key={item.path}
                    to={item.path}
                    className={`flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm font-medium transition-colors ${
                      isActive
                        ? 'bg-primary/10 text-primary'
                        : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
                    }`}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    {t(`nav.${item.label.toLowerCase()}`, item.label)}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <Separator />

      {/* User section */}
      <UserSection />
    </div>
  );
}

function UserSection() {
  const { user, logout } = useAuthStore();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="p-3">
      <DropdownMenu>
        <DropdownMenuTrigger render={<button className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left hover:bg-accent transition-colors" />}>
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/20 text-xs font-semibold text-primary">
              {user?.username?.[0]?.toUpperCase() ?? '?'}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{user?.username ?? 'User'}</p>
              <p className="truncate text-2xs text-muted-foreground">
                {user?.is_admin ? 'Admin' : 'User'}
              </p>
            </div>
            <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-48">
          <DropdownMenuItem onClick={() => navigate('/profile')}>
            <UserCog className="mr-2 h-4 w-4" />
            {t('nav.profile')}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={handleLogout} className="text-destructive">
            <LogOut className="mr-2 h-4 w-4" />
            {t('login.logout', 'Logout')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

function WorkspaceSwitcher() {
  const { t } = useTranslation();
  const { data: workspaces = [] } = useWorkspaces();
  const { activeWorkspaceId, setActiveWorkspace } = useWorkspaceStore();
  const qc = useQueryClient();

  const active = workspaces.find((w) => w.id === activeWorkspaceId);

  const handleSwitch = (id: number | null) => {
    setActiveWorkspace(id);
    // Invalidate all queries to refetch with new workspace
    qc.invalidateQueries();
  };

  if (workspaces.length === 0) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="outline" size="sm" className="gap-1.5 max-w-[200px]" />}>
        <Building2 className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate text-xs">{active?.name || t('workspaces.all', 'All workspaces')}</span>
        <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[200px]">
        <DropdownMenuItem onClick={() => handleSwitch(null)}>
          <Building2 className="mr-2 h-3.5 w-3.5" />
          {t('workspaces.all', 'All workspaces')}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {workspaces.map((w) => (
          <DropdownMenuItem key={w.id} onClick={() => handleSwitch(w.id)}>
            <FolderKanban className="mr-2 h-3.5 w-3.5" />
            <span className="truncate">{w.name}</span>
            {w.id === activeWorkspaceId && (
              <Check className="ml-auto h-3.5 w-3.5 text-primary" />
            )}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ImpersonationBanner() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const stopImpersonate = useAuthStore((s) => s.stopImpersonate);

  if (!user?.impersonator) return null;

  const handleReturn = async () => {
    try {
      await stopImpersonate();
      toast.success(t('users.impersonate_stopped'));
      navigate('/');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="flex items-center justify-between gap-3 border-b border-amber-500/40 bg-amber-500/15 px-4 py-2 text-sm text-amber-700 dark:text-amber-300 lg:px-6">
      <div className="flex min-w-0 items-center gap-2">
        <UserCog className="h-4 w-4 shrink-0" />
        <span className="truncate">
          {t('users.impersonating_as', { name: user.username })}
          <span className="text-muted-foreground"> · {t('users.impersonated_by', { name: user.impersonator })}</span>
        </span>
      </div>
      <Button size="sm" variant="outline" onClick={handleReturn} className="shrink-0">
        <LogOut className="mr-1.5 h-3.5 w-3.5" />
        {t('users.return_to_admin')}
      </Button>
    </div>
  );
}

function TopBar() {
  const { theme, toggleTheme } = useThemeStore();
  const { i18n, t } = useTranslation();
  const setPaletteOpen = useCommandPaletteStore((s) => s.setOpen);
  const isMac = typeof navigator !== 'undefined' && /mac/i.test(navigator.platform);

  const toggleLanguage = () => {
    const next = i18n.language === 'ru' ? 'en' : 'ru';
    i18n.changeLanguage(next);
  };

  return (
    <header className="flex h-14 items-center justify-between gap-3 border-b px-4 lg:px-6">
      {/* Mobile menu */}
      <Sheet>
        <SheetTrigger render={<Button variant="ghost" size="icon" className="lg:hidden" />}>
          <Menu className="h-5 w-5" />
        </SheetTrigger>
        <SheetContent side="left" className="w-[var(--sidebar-width)] p-0">
          <SidebarContent />
        </SheetContent>
      </Sheet>

      <div className="hidden items-center gap-3 lg:flex">
        <WorkspaceSwitcher />
      </div>

      {/* Search / command palette trigger */}
      <button
        type="button"
        onClick={() => setPaletteOpen(true)}
        className="flex h-8 flex-1 items-center gap-2 rounded-lg border border-input bg-transparent px-2.5 text-sm text-muted-foreground transition-colors outline-none hover:bg-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 sm:max-w-72"
      >
        <Search className="h-4 w-4 shrink-0" />
        <span className="hidden truncate sm:inline">{t('nav.searchPlaceholder', 'Jump to a section...')}</span>
        <kbd className="ml-auto hidden shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-2xs sm:block">
          {isMac ? '⌘K' : 'Ctrl+K'}
        </kbd>
      </button>

      {/* Right side */}
      <div className="flex items-center gap-1">
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon" onClick={toggleLanguage} />}>
            <Languages className="h-4 w-4" />
          </TooltipTrigger>
          <TooltipContent>{i18n.language === 'ru' ? 'English' : 'Русский'}</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon" onClick={toggleTheme} />}>
            {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </TooltipTrigger>
          <TooltipContent>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</TooltipContent>
        </Tooltip>

        <ActiveTasksIndicator />
        <NotificationsDropdown />
      </div>
    </header>
  );
}

export default function AppLayout() {
  // Глобальная подписка на real-time события WebSocket: server_added/updated/deleted,
  // vm_status_update/created/deleted. Кэши React Query инвалидируются мгновенно,
  // поэтому пользователю не нужно обновлять страницу.
  useGlobalRealtimeSync();

  return (
    <div className="flex h-dvh overflow-hidden">
      <CommandPalette />
      <MinimizedOpsTray />

      {/* Desktop sidebar */}
      <aside className="hidden w-[var(--sidebar-width)] shrink-0 border-r bg-card lg:block">
        <SidebarContent />
      </aside>

      {/* Main area */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <ImpersonationBanner />
        <TopBar />
        <main className="flex-1 overflow-y-auto p-4 lg:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
