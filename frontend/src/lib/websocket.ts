import { apiClient } from '@/lib/api-client';

type MessageHandler = (data: unknown) => void;

/**
 * Single-use, ~30 s ticket for one WebSocket handshake (?ticket=).
 *
 * Browsers cannot send headers on WebSocket requests; putting the session JWT
 * in the URL leaked it into proxy logs and browser history. Fetch a fresh
 * ticket right before every (re)connect — it cannot be reused.
 */
export async function fetchWsTicket(): Promise<string> {
  const { ticket } = await apiClient.post<{ ticket: string }>('/api/auth/ws-ticket');
  return ticket;
}

interface WSOptions {
  url: string;
  onMessage?: MessageHandler;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (event: Event) => void;
  reconnect?: boolean;
  /** Initial reconnect delay; doubles per failed attempt up to MAX_RECONNECT_DELAY. */
  reconnectInterval?: number;
  /** 0 / undefined = keep retrying (with backoff) until disconnect(). */
  maxReconnectAttempts?: number;
}

const MAX_RECONNECT_DELAY = 30_000;
// Close codes the backend uses for rejected credentials: retrying with the same
// token cannot succeed, so wait for an explicit connect() (e.g. after login).
const AUTH_CLOSE_CODES = new Set([4001, 4003]);

export class WebSocketManager {
  private ws: WebSocket | null = null;
  private options: WSOptions;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private subscribers = new Map<string, Set<MessageHandler>>();
  private typeListeners = new Map<string, Set<MessageHandler>>();
  private _isConnected = false;
  private manualClose = false;
  // Bumped by every connect()/disconnect(): a ticket fetch that finishes after
  // a newer call must not open a socket.
  private connectGeneration = 0;
  private ticketPending = false;

  constructor(options: WSOptions) {
    this.options = {
      reconnect: true,
      reconnectInterval: 1000,
      // Was 10 tries x 3 s: a backend restart longer than ~30 s (self-update)
      // silently ended real-time updates until the page was reloaded.
      maxReconnectAttempts: 0,
      ...options,
    };
  }

  get isConnected() {
    return this._isConnected;
  }

  connect() {
    // Don't create a new socket if one is already open or connecting
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    if (this.ticketPending) return;

    // An explicit connect() (e.g. after logging in again) re-enables reconnects
    // that disconnect() switched off.
    this.manualClose = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    // Logged out: nothing to authenticate with; an explicit connect() after
    // login (resetTasksWebSocket) opens the socket.
    if (!apiClient.getToken()) return;

    const generation = ++this.connectGeneration;
    this.ticketPending = true;
    fetchWsTicket().then(
      (ticket) => {
        if (generation !== this.connectGeneration) return;
        this.ticketPending = false;
        if (this.manualClose) return;
        const separator = this.options.url.includes('?') ? '&' : '?';
        this.open(`${this.options.url}${separator}ticket=${encodeURIComponent(ticket)}`);
      },
      (err: unknown) => {
        if (generation !== this.connectGeneration) return;
        this.ticketPending = false;
        // 401/403: the session is gone (api-client already redirects to
        // /login). Anything else (backend restarting) — back off and retry.
        const status = (err as { status?: number })?.status;
        if (status === 403 || (err instanceof Error && err.message === 'Unauthorized')) return;
        this.tryReconnect();
      },
    );
  }

  private open(url: string) {
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._isConnected = true;
      this.reconnectAttempts = 0;
      this.startPing();
      // Re-send subscribe messages for all active channels (handles initial
      // subscribe-before-open and reconnect cases).
      for (const channel of this.subscribers.keys()) {
        try {
          this.ws?.send(JSON.stringify({ type: 'subscribe', channel }));
        } catch { /* ignore */ }
      }
      this.options.onOpen?.();
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.options.onMessage?.(data);

        // Route to type listeners (for direct user messages without channel)
        if (data.type) {
          const typeHandlers = this.typeListeners.get(data.type);
          typeHandlers?.forEach((handler) => handler(data));
        }

        // Route to channel subscribers
        if (data.channel) {
          const handlers = this.subscribers.get(data.channel);
          handlers?.forEach((handler) => handler(data));
        }
      } catch {
        // Non-JSON message
        this.options.onMessage?.(event.data);
      }
    };

    const socket = this.ws;
    this.ws.onclose = (event) => {
      // A socket replaced by a newer connect() must not drive reconnects.
      if (this.ws !== socket) return;
      this._isConnected = false;
      this.stopPing();
      this.options.onClose?.();
      if (AUTH_CLOSE_CODES.has(event.code)) return;
      this.tryReconnect();
    };

    this.ws.onerror = (event) => {
      this.options.onError?.(event);
    };
  }

  disconnect() {
    this.manualClose = true;
    this.connectGeneration++;
    this.ticketPending = false;
    this.stopPing();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this._isConnected = false;
  }

  send(data: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }

  subscribe(channel: string, handler: MessageHandler) {
    if (!this.subscribers.has(channel)) {
      this.subscribers.set(channel, new Set());
      // Only send a subscribe message the first time someone subscribes to
      // this channel. If WS is not OPEN yet, onopen() will replay it.
      this.send({ type: 'subscribe', channel });
    }
    this.subscribers.get(channel)!.add(handler);

    // Return unsubscribe function
    return () => {
      const handlers = this.subscribers.get(channel);
      handlers?.delete(handler);
      if (handlers && handlers.size === 0) {
        this.subscribers.delete(channel);
        this.send({ type: 'unsubscribe', channel });
      }
    };
  }

  /** Listen to messages by their `type` field (for direct user messages without a channel). */
  onType(type: string, handler: MessageHandler) {
    if (!this.typeListeners.has(type)) {
      this.typeListeners.set(type, new Set());
    }
    this.typeListeners.get(type)!.add(handler);
    return () => {
      this.typeListeners.get(type)?.delete(handler);
    };
  }

  private tryReconnect() {
    const max = this.options.maxReconnectAttempts ?? 0;
    if (!this.options.reconnect || this.manualClose || (max > 0 && this.reconnectAttempts >= max)) {
      return;
    }

    // Exponential backoff: quick recovery from blips without hammering a
    // backend that is down for a while.
    const base = this.options.reconnectInterval ?? 1000;
    const delay = Math.min(base * 2 ** Math.min(this.reconnectAttempts, 10), MAX_RECONNECT_DELAY);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startPing() {
    this.stopPing();
    this.pingTimer = setInterval(() => {
      this.send({ type: 'ping' });
    }, 30000);
  }

  private stopPing() {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }
}

// Singleton for tasks WebSocket
let tasksWs: WebSocketManager | null = null;

/**
 * Re-open the tasks socket for a new identity (login, logout, impersonation).
 *
 * The backend authenticates a socket once, at connect time: without this the
 * tab kept streaming under the previous user's session after a token switch.
 * Channel subscriptions are replayed on open and re-authorized server-side.
 */
export function resetTasksWebSocket(reconnect: boolean): void {
  if (!tasksWs) return;
  tasksWs.disconnect();
  if (reconnect) tasksWs.connect();
}

export function getTasksWebSocket(): WebSocketManager {
  if (!tasksWs) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    tasksWs = new WebSocketManager({
      url: `${protocol}//${window.location.host}/ws/tasks`,
    });
  }
  return tasksWs;
}
