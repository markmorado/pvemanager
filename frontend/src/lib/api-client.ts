import type { ApiError } from '@/types';

const API_BASE = '';

function getActiveWorkspaceId(): number | null {
  const stored = localStorage.getItem('pve-active-workspace');
  return stored ? Number(stored) : null;
}

// FastAPI returns `detail` as a string for HTTPException, but as an array of
// objects ({loc, msg, type}) for 422 validation errors. Coercing the latter
// straight into `new Error()` yields the useless "[object Object]" message.
function formatApiError(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const msg = detail
      .map((e) => (e && typeof e === 'object' && 'msg' in e ? String((e as { msg: unknown }).msg) : String(e)))
      .filter(Boolean)
      .join('; ');
    if (msg) return msg;
  }
  if (detail && typeof detail === 'object') {
    const obj = detail as { msg?: unknown; detail?: unknown };
    if (typeof obj.msg === 'string') return obj.msg;
    if (typeof obj.detail === 'string') return obj.detail;
  }
  return `HTTP ${status}`;
}

// JWT segments are base64url (RFC 7515): '-'/'_' instead of '+'/'/', no padding.
// atob() rejects '-'/'_', so decoding the payload with it failed for most
// tokens — the refresh was never scheduled and sessions simply expired.
function decodeJwtPayload(token: string): { exp?: number } | null {
  const part = token.split('.')[1];
  if (!part) return null;
  const b64 = part.replace(/-/g, '+').replace(/_/g, '/');
  const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
  try {
    return JSON.parse(atob(padded));
  } catch {
    return null;
  }
}

const REFRESH_RETRY_MS = 30_000;

class ApiClient {
  private token: string | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.token = localStorage.getItem('access_token');
    if (this.token) {
      this.scheduleRefresh();
    }
  }

  setToken(token: string | null) {
    this.token = token;
    if (token) {
      localStorage.setItem('access_token', token);
      this.scheduleRefresh();
    } else {
      localStorage.removeItem('access_token');
      if (this.refreshTimer) {
        clearTimeout(this.refreshTimer);
        this.refreshTimer = null;
      }
    }
  }

  getToken(): string | null {
    return this.token;
  }

  private getTokenExpiry(): number | null {
    if (!this.token) return null;
    const payload = decodeJwtPayload(this.token);
    return payload?.exp ? payload.exp * 1000 : null;
  }

  private scheduleRefresh() {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    const expiry = this.getTokenExpiry();
    if (!expiry) return;

    const refreshIn = expiry - Date.now() - 2 * 60 * 1000; // 2 min before expiry
    if (refreshIn <= 0) {
      this.refreshToken();
      return;
    }

    this.refreshTimer = setTimeout(() => this.refreshToken(), refreshIn);
  }

  private async refreshToken() {
    try {
      const res = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        },
      });
      if (res.ok) {
        const data = await res.json();
        this.setToken(data.access_token);
      } else if (res.status === 401 || res.status === 403) {
        this.setToken(null);
        window.location.href = '/login';
      } else {
        // 502/503 while the panel restarts (e.g. self-update) is not a reason
        // to log everybody out — retry while the token is still valid.
        this.retryRefreshLater();
      }
    } catch {
      // Network error, try again later
      this.retryRefreshLater();
    }
  }

  private retryRefreshLater() {
    const expiry = this.getTokenExpiry();
    if (!this.token || (expiry && expiry <= Date.now())) return;
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refreshToken(), REFRESH_RETRY_MS);
  }

  async request<T>(
    path: string,
    options: RequestInit = {}
  ): Promise<T> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string> || {}),
    };

    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }

    // Явно переданный заголовок (например, '0' — «показать все области») не
    // должен перезатираться значением активной рабочей области по умолчанию.
    if (!('X-Active-Workspace' in headers)) {
      const wsId = getActiveWorkspaceId();
      if (wsId) {
        headers['X-Active-Workspace'] = String(wsId);
      }
    }

    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers,
    });

    return this.handleResponse<T>(res, path);
  }

  // multipart/form-data: без Content-Type (браузер сам проставит boundary) и
  // без JSON.stringify тела — для загрузки файлов (напр. локальный ISO).
  async postForm<T>(path: string, formData: FormData): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }
    const wsId = getActiveWorkspaceId();
    if (wsId) {
      headers['X-Active-Workspace'] = String(wsId);
    }

    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers,
      body: formData,
    });

    return this.handleResponse<T>(res, path);
  }

  private async handleResponse<T>(res: Response, path: string): Promise<T> {
    if (res.status === 401) {
      // 401 от самого логина — это «неверный логин/пароль»: отдаём ошибку в
      // форму, а не перезагружаем страницу (иначе сообщение теряется).
      const isLoginRequest = path.startsWith('/api/auth/login');
      if (!isLoginRequest) {
        this.setToken(null);
        // Не дёргаем навигацию, если уже на /login — присваивание location.href
        // на тот же URL вызывает полную перезагрузку и стирает состояние формы.
        if (window.location.pathname !== '/login') {
          window.location.href = '/login';
        }
        throw new Error('Unauthorized');
      }
    }

    if (!res.ok) {
      const error: ApiError = await res.json().catch(() => ({
        detail: `HTTP ${res.status}: ${res.statusText}`,
      }));
      throw Object.assign(new Error(formatApiError(error.detail, res.status)), { status: res.status });
    }

    if (res.status === 204) return undefined as T;
    return res.json();
  }

  get<T>(path: string, options?: { headers?: Record<string, string> }) {
    return this.request<T>(path, options?.headers ? { headers: options.headers } : undefined);
  }

  post<T>(path: string, body?: unknown) {
    return this.request<T>(path, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  put<T>(path: string, body?: unknown) {
    return this.request<T>(path, {
      method: 'PUT',
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  patch<T>(path: string, body?: unknown) {
    return this.request<T>(path, {
      method: 'PATCH',
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  delete<T>(path: string, body?: unknown) {
    return this.request<T>(path, {
      method: 'DELETE',
      body: body ? JSON.stringify(body) : undefined,
    });
  }
}

export const apiClient = new ApiClient();
