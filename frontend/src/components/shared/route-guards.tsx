import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { useAuthStore } from '@/stores/auth-store';
import { hasPermission } from '@/lib/permissions';
import { firstAccessiblePath } from '@/lib/nav-items';

function Spinner() {
  return (
    <div className="flex h-dvh items-center justify-center">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
    </div>
  );
}

/**
 * Blocks a route unless the user holds `permission` (admins bypass). Omitting
 * `permission` makes the route admin-only, matching the nav-item convention.
 *
 * Denied users are sent to the first page they can actually open; when nothing
 * is reachable (or that page is the one being denied) an explanatory screen is
 * rendered instead, so the redirect can never loop. The backend enforces the
 * same permission on every endpoint — this guard only keeps users out of pages
 * that would answer them with 403s.
 */
export function RequirePermission({
  permission,
  children,
}: {
  permission?: string;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const location = useLocation();

  // Authenticated but /me has not resolved yet: permissions are unknown, so
  // wait instead of bouncing the user off a page they are allowed to see.
  const pending = isAuthenticated && !user;
  const allowed = hasPermission(!!user?.is_admin, user?.permissions, permission);
  const fallback = firstAccessiblePath(!!user?.is_admin, user?.permissions);
  const redirectTo = fallback && fallback !== location.pathname ? fallback : null;

  useEffect(() => {
    if (pending || allowed) return;
    toast.error(t('common.access_denied'), { id: 'access-denied' });
  }, [pending, allowed, location.pathname, t]);

  if (pending) return <Spinner />;
  if (allowed) return <>{children}</>;
  if (redirectTo) return <Navigate to={redirectTo} replace />;

  return (
    <div className="flex flex-col items-center justify-center gap-3 py-24 text-center">
      <ShieldAlert className="h-10 w-10 text-muted-foreground" />
      <h1 className="text-lg font-semibold">{t('common.access_denied')}</h1>
      <p className="max-w-sm text-sm text-muted-foreground">{t('common.access_denied_desc')}</p>
    </div>
  );
}

/**
 * Landing page ("/", unknown URLs, after login or an identity switch): the
 * first page the user may open. Sending everybody to /dashboard made users
 * without dashboard:view bounce off RequirePermission with an "access denied"
 * toast on every sign-in.
 */
export function HomeRedirect() {
  const user = useAuthStore((s) => s.user);
  if (!user) return <Spinner />;
  return <Navigate to={firstAccessiblePath(!!user.is_admin, user.permissions) ?? '/profile'} replace />;
}

export function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuthStore();

  if (isLoading) {
    return <Spinner />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}

export function PublicRoute() {
  const { isAuthenticated } = useAuthStore();

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return <Outlet />;
}
