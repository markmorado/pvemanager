import { create } from 'zustand';
import { apiClient } from '@/lib/api-client';
import { queryClient } from '@/lib/query-client';
import { resetTasksWebSocket } from '@/lib/websocket';
import { useWorkspaceStore } from '@/stores/workspace-store';
import { useDeployTasksStore } from '@/stores/deploy-tasks-store';
import { useMinimizedOpsStore } from '@/stores/minimized-ops-store';
import { useBulkTasksStore } from '@/stores/bulk-tasks-store';
import type { User, LoginRequest, AuthResponse } from '@/types';

const LOAD_USER_RETRY_MS = 3_000;

// Per-identity client state that outlives the React Query cache: the active
// workspace (sent as X-Active-Workspace) and the task trays, two of which are
// persisted in localStorage. Left in place, the next identity in this browser
// saw the previous one's task names and sent its workspace id.
function resetIdentityState(): void {
  useWorkspaceStore.getState().setActiveWorkspace(null);
  useDeployTasksStore.setState({ tasks: [] });
  useMinimizedOpsStore.setState({ items: [] });
  useBulkTasksStore.setState({ tasks: [] });
}

interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  login: (credentials: LoginRequest) => Promise<{ twoFactorRequired: boolean }>;
  logout: () => Promise<void>;
  loadUser: () => Promise<void>;
  impersonate: (userId: number) => Promise<void>;
  stopImpersonate: () => Promise<void>;
  clearError: () => void;
}

// Swap the active token for a freshly-issued one (impersonation start/stop),
// resetting the React Query cache so the previous identity's data cannot leak,
// then reload the current-user info under the new token.
async function switchToken(
  set: (partial: Partial<AuthState>) => void,
  token: string,
): Promise<void> {
  apiClient.setToken(token);
  queryClient.clear();
  resetTasksWebSocket(true);
  // The previous identity's workspace selection may not belong to the new one.
  resetIdentityState();
  set({ token, isAuthenticated: true });
  const user = await apiClient.get<User>('/api/auth/me');
  set({ user });
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: localStorage.getItem('access_token'),
  isAuthenticated: !!localStorage.getItem('access_token'),
  isLoading: false,
  error: null,

  login: async (credentials: LoginRequest) => {
    set({ isLoading: true, error: null });
    try {
      const data = await apiClient.post<AuthResponse>('/api/auth/login', credentials);

      // Password accepted but a second factor is still required.
      if (data.two_factor_required || !data.access_token) {
        set({ isLoading: false });
        return { twoFactorRequired: true };
      }

      apiClient.setToken(data.access_token);
      // Сбрасываем кэш React Query: иначе новый пользователь увидит
      // закэшированные данные предыдущего (список инстансов и т.п.).
      queryClient.clear();
      resetTasksWebSocket(true);
      set({ token: data.access_token, isAuthenticated: true, isLoading: false });

      // Load user info
      const user = await apiClient.get<User>('/api/auth/me');
      set({ user });
      return { twoFactorRequired: false };
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Login failed';
      set({ error: message, isLoading: false, isAuthenticated: false });
      throw err;
    }
  },

  logout: async () => {
    try {
      await apiClient.post('/api/auth/logout');
    } catch {
      // Ignore logout errors
    }
    apiClient.setToken(null);
    // Полностью очищаем кэш запросов, чтобы данные не утекли следующему
    // пользователю, который войдёт в этой же вкладке.
    queryClient.clear();
    resetTasksWebSocket(false);
    resetIdentityState();
    set({ user: null, token: null, isAuthenticated: false });
  },

  loadUser: async () => {
    const token = localStorage.getItem('access_token');
    if (!token) {
      set({ isAuthenticated: false, isLoading: false });
      return;
    }
    set({ isLoading: true });
    try {
      apiClient.setToken(token);
      const user = await apiClient.get<User>('/api/auth/me');
      set({ user, isAuthenticated: true, isLoading: false });
    } catch (err) {
      const status = (err as { status?: number }).status;
      const rejected = status === 401 || status === 403
        || (err instanceof Error && err.message === 'Unauthorized');
      if (rejected) {
        apiClient.setToken(null);
        set({ user: null, token: null, isAuthenticated: false, isLoading: false });
        return;
      }
      // Network error / 5xx (panel restarting, e.g. self-update): the session is
      // still valid — dropping the token here logged everybody out on a reload
      // during a restart. Keep the spinner up and try again.
      setTimeout(() => {
        if (localStorage.getItem('access_token')) get().loadUser();
      }, LOAD_USER_RETRY_MS);
    }
  },

  impersonate: async (userId: number) => {
    const data = await apiClient.post<AuthResponse>(`/api/auth/impersonate/${userId}`);
    if (!data.access_token) throw new Error('Impersonation failed');
    await switchToken(set, data.access_token);
  },

  stopImpersonate: async () => {
    const data = await apiClient.post<AuthResponse>('/api/auth/stop-impersonate');
    if (!data.access_token) throw new Error('Failed to return to admin');
    await switchToken(set, data.access_token);
  },

  clearError: () => set({ error: null }),
}));
