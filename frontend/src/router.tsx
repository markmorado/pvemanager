import { lazy, Suspense } from 'react';
import { createBrowserRouter, Navigate } from 'react-router';
import AppLayout from '@/layouts/AppLayout';
import AuthLayout from '@/layouts/AuthLayout';
import ConsoleLayout from '@/layouts/ConsoleLayout';
import { ProtectedRoute, PublicRoute, RequirePermission } from '@/components/shared/route-guards';
import LoginPage from '@/features/auth/LoginPage';
const ForgotPasswordPage = lazy(() => import('./features/auth/ForgotPasswordPage.tsx'));
const ResetPasswordPage = lazy(() => import('./features/auth/ResetPasswordPage.tsx'));
// Dashboard and instance detail carry recharts (~0.4 MB): out of the entry
// chunk, which tenants (no dashboard:view) load on every visit.
const DashboardPage = lazy(() => import('./features/dashboard/DashboardPage.tsx'));
import InstancesPage from '@/features/instances/InstancesPage';
const InstanceDetailPage = lazy(() => import('./features/instances/InstanceDetailPage.tsx'));
const SnapshotArchivesPage = lazy(() => import('./features/instances/SnapshotArchivesPage.tsx'));
// Lazy like NodeShellPage: it pulls in xterm (and its global CSS) for every user otherwise.
const ConsolePage = lazy(() => import('./features/console/ConsolePage.tsx'));
const NodeShellPage = lazy(() => import('./features/console/NodeShellPage.tsx'));

// Lazy-loaded pages
// NOTE: явные относительные пути с расширением .tsx — обход бага rolldown-vite
// (vite 8.0.5), который не добавляет расширение для dynamic import() с alias '@/'.
const CreateInstanceWizard = lazy(() => import('./features/instances/CreateInstanceWizard.tsx'));
const NodesPage = lazy(() => import('./features/nodes/NodesPage.tsx'));
const NodeDetailPage = lazy(() => import('./features/nodes/NodeDetailPage.tsx'));
const ClusterPage = lazy(() => import('./features/cluster/ClusterPage.tsx'));
const TemplatesPage = lazy(() => import('./features/templates/TemplatesPage.tsx'));
const BackupsPage = lazy(() => import('./features/backups/BackupsPage.tsx'));
const TasksPage = lazy(() => import('./features/tasks/TasksPage.tsx'));
const TopologyPage = lazy(() => import('./features/topology/TopologyPage.tsx'));
const IPAMDashboardPage = lazy(() => import('./features/ipam/IPAMDashboardPage.tsx'));
const IPAMNetworksPage = lazy(() => import('./features/ipam/IPAMNetworksPage.tsx'));
const IPAMNetworkDetailPage = lazy(() => import('./features/ipam/IPAMNetworkDetailPage.tsx'));
const IPAMAllocationsPage = lazy(() => import('./features/ipam/IPAMAllocationsPage.tsx'));
const IPAMHistoryPage = lazy(() => import('./features/ipam/IPAMHistoryPage.tsx'));
const IPAMToolsPage = lazy(() => import('./features/ipam/IPAMToolsPage.tsx'));
const UsersPage = lazy(() => import('./features/users/UsersPage.tsx'));
const WorkspacesPage = lazy(() => import('./features/workspaces/WorkspacesPage.tsx'));
const LogsPage = lazy(() => import('./features/logs/LogsPage.tsx'));
const SettingsPage = lazy(() => import('./features/settings/SettingsPage.tsx'));
const ProfilePage = lazy(() => import('./features/settings/ProfilePage.tsx'));
const AppStorePage = lazy(() => import('./features/appstore/AppStorePage.tsx'));
const AppDetailPage = lazy(() => import('./features/appstore/AppDetailPage.tsx'));
const MyAppsPage = lazy(() => import('./features/appstore/MyAppsPage.tsx'));
const ScriptsPage = lazy(() => import('./features/scripts/ScriptsPage.tsx'));

function LazyFallback() {
  return <div className="flex items-center justify-center py-20 text-muted-foreground text-sm">Loading…</div>;
}

function SuspenseWrap({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<LazyFallback />}>{children}</Suspense>;
}

/**
 * A guarded page. `perm` is the permission the page's own API requires — the
 * same code the matching nav item is gated on, so a hidden menu entry can no
 * longer be reached by typing its URL. Omit `perm` for admin-only pages.
 */
function Page({ perm, children }: { perm?: string; children: React.ReactNode }) {
  return (
    <RequirePermission permission={perm}>
      <SuspenseWrap>{children}</SuspenseWrap>
    </RequirePermission>
  );
}

export const router = createBrowserRouter([
  {
    element: <PublicRoute />,
    children: [
      {
        element: <AuthLayout />,
        children: [
          { path: '/login', element: <LoginPage /> },
          { path: '/forgot-password', element: <SuspenseWrap><ForgotPasswordPage /></SuspenseWrap> },
          { path: '/reset-password', element: <SuspenseWrap><ResetPasswordPage /></SuspenseWrap> },
        ],
      },
    ],
  },
  {
    element: <ProtectedRoute />,
    children: [
      // Console — separate layout (no sidebar)
      {
        path: '/console/node/:serverId/:node',
        element: <ConsoleLayout />,
        children: [{ index: true, element: <Page perm="node:upgrade"><NodeShellPage /></Page> }],
      },
      {
        path: '/console/:serverId/:vmid',
        element: <ConsoleLayout />,
        children: [{ index: true, element: <Page perm="vm:console"><ConsolePage /></Page> }],
      },
      // Main app layout
      {
        element: <AppLayout />,
        children: [
          { path: '/', element: <Navigate to="/dashboard" replace /> },
          { path: '/dashboard', element: <Page perm="dashboard:view"><DashboardPage /></Page> },
          { path: '/instances', element: <Page perm="vm:view"><InstancesPage /></Page> },
          { path: '/instances/create', element: <Page perm="vm:create"><CreateInstanceWizard /></Page> },
          { path: '/instances/snapshot-archives', element: <Page perm="log:view"><SnapshotArchivesPage /></Page> },
          { path: '/instances/:serverId/:vmid', element: <Page perm="vm:view"><InstanceDetailPage /></Page> },
          { path: '/nodes', element: <Page perm="server:view"><NodesPage /></Page> },
          { path: '/nodes/:serverId', element: <Page perm="server:view"><NodeDetailPage /></Page> },
          { path: '/cluster', element: <Page perm="cluster:manage"><ClusterPage /></Page> },
          { path: '/templates', element: <Page perm="template:view"><TemplatesPage /></Page> },
          { path: '/appstore', element: <Page perm="app:view"><AppStorePage /></Page> },
          { path: '/appstore/:appId', element: <Page perm="app:view"><AppDetailPage /></Page> },
          { path: '/my-apps', element: <Page perm="app:view"><MyAppsPage /></Page> },
          { path: '/scripts', element: <Page perm="script:view"><ScriptsPage /></Page> },
          { path: '/images', element: <Navigate to="/templates" replace /> },
          { path: '/backups', element: <Page perm="backup:view"><BackupsPage /></Page> },
          { path: '/tasks', element: <Page perm="vm:view"><TasksPage /></Page> },
          { path: '/topology', element: <Page perm="network:view"><TopologyPage /></Page> },
          { path: '/ipam', element: <Page perm="ipam:view"><IPAMDashboardPage /></Page> },
          { path: '/ipam/networks', element: <Page perm="ipam:view"><IPAMNetworksPage /></Page> },
          { path: '/ipam/network/:id', element: <Page perm="ipam:view"><IPAMNetworkDetailPage /></Page> },
          { path: '/ipam/allocations', element: <Page perm="ipam:view"><IPAMAllocationsPage /></Page> },
          { path: '/ipam/history', element: <Page perm="ipam:view"><IPAMHistoryPage /></Page> },
          { path: '/ipam/tools', element: <Page perm="ipam:view"><IPAMToolsPage /></Page> },
          { path: '/networks', element: <Page perm="server:manage"><IPAMNetworksPage /></Page> },
          { path: '/users', element: <Page perm="user:view"><UsersPage /></Page> },
          { path: '/workspaces', element: <Page><WorkspacesPage /></Page> },
          { path: '/logs', element: <Page perm="log:view"><LogsPage /></Page> },
          { path: '/settings', element: <Page perm="setting:view"><SettingsPage /></Page> },
          // Own account — self-scoped endpoints, so no permission gates it.
          { path: '/profile', element: <SuspenseWrap><ProfilePage /></SuspenseWrap> },
        ],
      },
    ],
  },
  { path: '*', element: <Navigate to="/dashboard" replace /> },
]);
