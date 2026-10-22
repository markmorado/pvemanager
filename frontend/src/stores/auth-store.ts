import { create } from 'zustand';
import { apiClient } from '@/lib/api-client';
import { queryClient } from '@/lib/query-client';
import { resetTasksWebSocket } from '@/lib/websocket';
import { useWorkspaceStore } from '@/stores/workspace-store';
import type { User, LoginRequest, AuthResponse } from '@/types';

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
  // Reset the active workspace: the previous identity's selection (sent via the
  // X-Active-Workspace header) may not belong to the new one.
  useWorkspaceStore.getState().setActiveWorkspace(null);
  set({ token, isAuthenticated: true });
  const user = await apiClient.get<User>('/api/auth/me');
  set({ user });
}

export const useAuthStore = create<AuthState>((set) => ({
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
    } catch {
      apiClient.setToken(null);
      set({ user: null, token: null, isAuthenticated: false, isLoading: false });
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
