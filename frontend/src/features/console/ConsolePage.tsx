import { useEffect, useRef, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useSearchParams } from 'react-router';
import {
  Maximize,
  Minimize,
  Keyboard,
  Loader2,
  Power,
  ChevronDown,
  RotateCw,
  PowerOff,
  Zap,
  Square,
  ClipboardPaste,
  Camera,
  Scan,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { apiClient } from '@/lib/api-client';
import { fetchWsTicket } from '@/lib/websocket';
import { vmTypeLabel } from '@/lib/format';
import { usePowerAction } from '@/hooks/use-instances';
import '@xterm/xterm/css/xterm.css';

// X11 keysyms используемые доп. клавишами консоли (см. noVNC core/input/keysym.js)
const KEYSYM = {
  BackSpace: 0xff08,
  Tab: 0xff09,
  Escape: 0xff1b,
  Delete: 0xffff,
  Control_L: 0xffe3,
  Alt_L: 0xffe9,
  F1: 0xffbe,
} as const;
const FKEY_CODES = Array.from({ length: 12 }, (_, i) => ({
  label: `F${i + 1}`,
  keysym: KEYSYM.F1 + i,
  code: `F${i + 1}`,
}));


// noVNC and xterm.js will be loaded dynamically
// We use dynamic imports for code splitting

interface VNCData {
  port: number;
  ticket: string;
  password?: string;
  host: string;
  node: string;
  vmid: number;
  type: string;
  auth_ticket?: string;
}

// Сессия консоли переживает один проход эффекта: connect* асинхронны, а React
// StrictMode в dev монтирует эффект дважды. Без флага отмены вторая пара
// xterm/RFB ложится в тот же контейнер поверх первой — «осиротевший» терминал
// остаётся в DOM и занимает видимую область, из-за чего экран выглядит пустым.
interface SessionGuard {
  cancelled: boolean;
}

interface RFBHandle {
  disconnect: () => void;
  sendCtrlAltDel: () => void;
  sendKey: (keysym: number, code: string, down?: boolean) => void;
  clipboardPasteFrom: (text: string) => void;
  scaleViewport: boolean;
}

export default function ConsolePage() {
  const { serverId, vmid } = useParams<{ serverId: string; vmid: string }>();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation();
  const node = searchParams.get('node') || '';
  const type = searchParams.get('type') || 'qemu';
  const isSerial = type === 'qemu' && searchParams.get('mode') === 'serial';
  // Панель управления питанием/консолью доступна для VNC (qemu, не serial); для
  // serial и LXC-терминала клавиатура и буфер обмена уже идут через сам xterm.
  const isVnc = type === 'qemu' && !isSerial;

  const [status, setStatus] = useState<'connecting' | 'connected' | 'error'>('connecting');
  const [errorMsg, setErrorMsg] = useState('');
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [scaleToFit, setScaleToFit] = useState(true);
  const [clipboardOpen, setClipboardOpen] = useState(false);
  const [clipboardText, setClipboardText] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<RFBHandle | null>(null);
  const termRef = useRef<unknown>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const keepaliveRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const sid = Number(serverId);
  const vid = Number(vmid);
  const power = usePowerAction(sid, vid, type, node);

  const guardRef = useRef<SessionGuard | null>(null);

  // Освобождает ресурсы текущей сессии (RFB / xterm / WebSocket / keepalive).
  const cleanupSession = useCallback(() => {
    if (rfbRef.current) {
      try { rfbRef.current.disconnect(); } catch { /* */ }
      rfbRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (keepaliveRef.current) {
      clearInterval(keepaliveRef.current);
      keepaliveRef.current = null;
    }
    if (termRef.current) {
      const t = termRef.current as { terminal: { dispose: () => void }; cleanup: () => void };
      t.cleanup();
      t.terminal.dispose();
      termRef.current = null;
    }
  }, []);

  // ==================== VNC Console (QEMU) ====================
  const connectVNC = useCallback(async (guard: SessionGuard) => {
    try {
      const data = await apiClient.get<VNCData>(
        `/proxmox/api/${sid}/vm/${vid}/vnc?node=${node}`
      );
      if (guard.cancelled) return;

      // Dynamic import noVNC. The package ships CJS, so depending on bundler
      // interop the result can be: { default: RFB }, { default: { default: RFB, __esModule: true } },
      // or the raw CJS namespace. Normalise to the actual constructor.
      // @ts-expect-error noVNC doesn't have TS declarations
      const rfbModule = await import('@novnc/novnc/lib/rfb');
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mod: any = rfbModule;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const RFB: any =
        typeof mod === 'function'
          ? mod
          : typeof mod.default === 'function'
            ? mod.default
            : typeof mod.default?.default === 'function'
              ? mod.default.default
              : typeof mod.RFB === 'function'
                ? mod.RFB
                : null;
      if (typeof RFB !== 'function') {
        throw new Error('noVNC RFB constructor not found in module exports');
      }

      const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const params = new URLSearchParams({
        port: String(data.port),
        vncticket: data.ticket,
      });
      if (data.password) params.set('vnc_password', data.password);
      if (data.auth_ticket) params.set('auth_ticket', data.auth_ticket);
      params.set('ticket', await fetchWsTicket());

      const wsUrl = `${wsProto}//${window.location.host}/proxmox/ws/vnc/${sid}/${data.node}/qemu/${vid}?${params}`;

      if (guard.cancelled || !containerRef.current) return;

      // Pass URL string to RFB, NOT an open WebSocket
      const rfb = new RFB(containerRef.current, wsUrl, {
        credentials: { password: data.password || '' },
      });

      rfb.scaleViewport = true;
      rfb.resizeSession = false;

      rfb.addEventListener('connect', () => {
        if (guard.cancelled) return;
        setStatus('connected');
      });

      rfb.addEventListener('disconnect', (e: CustomEvent) => {
        if (guard.cancelled) return;
        if (e.detail?.clean) {
          setStatus('error');
          setErrorMsg('Connection closed');
        } else {
          setStatus('error');
          setErrorMsg('Connection lost');
        }
      });

      rfbRef.current = rfb as RFBHandle;
      setScaleToFit(true);
    } catch (err) {
      if (guard.cancelled) return;
      setStatus('error');
      setErrorMsg(err instanceof Error ? err.message : 'Failed to connect');
    }
  }, [sid, vid, node]);

  // ==================== xterm.js session (LXC terminal / VM serial) ====================
  // Общая обвязка xterm + WebSocket. wsPath — путь до нужного ws-эндпоинта Proxmox-прокси.
  const setupXtermSession = useCallback(async (wsPath: string, guard: SessionGuard) => {
    // Dynamic import xterm.js
    const [{ Terminal }, { FitAddon }, { WebLinksAddon }] = await Promise.all([
      import('@xterm/xterm'),
      import('@xterm/addon-fit'),
      import('@xterm/addon-web-links'),
    ]);
    if (guard.cancelled) return;
    const ticket = await fetchWsTicket();

    if (guard.cancelled || !containerRef.current) return;

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();

    const terminal = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: 'JetBrains Mono, monospace',
      theme: {
        background: '#09090B',
        foreground: '#F0F0F3',
        cursor: '#F0F0F3',
        selectionBackground: 'rgba(255,255,255,0.2)',
      },
    });

    terminal.loadAddon(fitAddon);
    terminal.loadAddon(webLinksAddon);
    terminal.open(containerRef.current);
    requestAnimationFrame(() => fitAddon.fit());

    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProto}//${window.location.host}${wsPath}?ticket=${encodeURIComponent(ticket)}`;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      if (guard.cancelled) {
        ws.close();
        return;
      }
      setStatus('connected');
      const { cols, rows } = terminal;
      ws.send(`1:${cols}:${rows}:`);
      keepaliveRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('2');
      }, 120000);
    };

    ws.onmessage = (event) => {
      if (guard.cancelled) return;
      if (event.data instanceof ArrayBuffer) terminal.write(new Uint8Array(event.data));
      else terminal.write(event.data);
    };
    ws.onclose = () => {
      if (guard.cancelled) return;
      setStatus('error');
      setErrorMsg('Terminal connection closed');
    };
    ws.onerror = () => {
      if (guard.cancelled) return;
      setStatus('error');
      setErrorMsg('Terminal connection error');
    };

    terminal.onData((data: string) => {
      if (ws.readyState === WebSocket.OPEN) {
        const byteLength = new TextEncoder().encode(data).byteLength;
        ws.send(`0:${byteLength}:${data}`);
      }
    });
    terminal.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(`1:${cols}:${rows}:`);
    });

    const handleResize = () => fitAddon.fit();
    window.addEventListener('resize', handleResize);

    // Сессию могли отменить, пока грузились чанки xterm. Убираем за собой сами и
    // НЕ трогаем refs: они уже могут указывать на актуальную сессию. Иначе в
    // контейнере остаётся невидимый терминал поверх живого — чёрный экран.
    if (guard.cancelled) {
      window.removeEventListener('resize', handleResize);
      terminal.dispose();
      ws.close();
      return;
    }

    wsRef.current = ws;
    termRef.current = { terminal, fitAddon, cleanup: () => window.removeEventListener('resize', handleResize) };
  }, []);

  // ==================== Terminal Console (LXC) ====================
  const connectTerminal = useCallback(async (guard: SessionGuard) => {
    try {
      // Termproxy-сессию создаёт сам ws-эндпоинт. Отдельный REST-вызов
      // (GET .../container/{vmid}/terminal) поднимал бы на ноде вторую
      // termproxy-сессию, к которой никто не подключается: Proxmox ждёт клиента
      // ~10 секунд и завершает задачу с «failed waiting for client: timed out».
      await setupXtermSession(`/proxmox/ws/terminal/${sid}/${node}/${vid}`, guard);
    } catch (err) {
      if (guard.cancelled) return;
      setStatus('error');
      setErrorMsg(err instanceof Error ? err.message : 'Failed to connect');
    }
  }, [sid, vid, node, setupXtermSession]);

  // ==================== Serial Console (QEMU) ====================
  const connectSerial = useCallback(async (guard: SessionGuard) => {
    try {
      // Убедиться, что у VM есть serial0 (добавит при отсутствии)
      await apiClient.post(`/proxmox/api/${sid}/vm/${vid}/serial/enable?node=${node}`);
      if (guard.cancelled) return;
      await setupXtermSession(`/proxmox/ws/serial/${sid}/${node}/${vid}`, guard);
    } catch (err) {
      if (guard.cancelled) return;
      setStatus('error');
      setErrorMsg(err instanceof Error ? err.message : 'Failed to connect');
    }
  }, [sid, vid, node, setupXtermSession]);

  // ==================== Connect on mount ====================
  // Старт новой сессии: старая помечается отменённой и полностью убирается,
  // чтобы в контейнере всегда был ровно один живой терминал/RFB.
  const startSession = useCallback(() => {
    if (guardRef.current) guardRef.current.cancelled = true;
    cleanupSession();

    const guard: SessionGuard = { cancelled: false };
    guardRef.current = guard;
    setStatus('connecting');
    setErrorMsg('');

    if (isSerial) connectSerial(guard);
    else if (type === 'qemu') connectVNC(guard);
    else connectTerminal(guard);
  }, [isSerial, type, connectSerial, connectVNC, connectTerminal, cleanupSession]);

  useEffect(() => {
    startSession();

    return () => {
      if (guardRef.current) guardRef.current.cancelled = true;
      cleanupSession();
    };
  }, [startSession, cleanupSession]);

  // ==================== Fullscreen ====================
  const toggleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  };

  // ==================== Send Ctrl+Alt+Del (VNC) ====================
  const sendCtrlAltDel = () => {
    rfbRef.current?.sendCtrlAltDel();
  };

  // ==================== Extra keys (VNC) ====================
  const sendKeyCombo = (keysym: number, code: string) => {
    rfbRef.current?.sendKey(keysym, code);
  };
  const sendCtrlAltBackspace = () => {
    const rfb = rfbRef.current;
    if (!rfb) return;
    rfb.sendKey(KEYSYM.Control_L, 'ControlLeft', true);
    rfb.sendKey(KEYSYM.Alt_L, 'AltLeft', true);
    rfb.sendKey(KEYSYM.BackSpace, 'Backspace', true);
    rfb.sendKey(KEYSYM.BackSpace, 'Backspace', false);
    rfb.sendKey(KEYSYM.Alt_L, 'AltLeft', false);
    rfb.sendKey(KEYSYM.Control_L, 'ControlLeft', false);
  };
  const sendCtrlAltF = (keysym: number, code: string) => {
    const rfb = rfbRef.current;
    if (!rfb) return;
    rfb.sendKey(KEYSYM.Control_L, 'ControlLeft', true);
    rfb.sendKey(KEYSYM.Alt_L, 'AltLeft', true);
    rfb.sendKey(keysym, code, true);
    rfb.sendKey(keysym, code, false);
    rfb.sendKey(KEYSYM.Alt_L, 'AltLeft', false);
    rfb.sendKey(KEYSYM.Control_L, 'ControlLeft', false);
  };

  // ==================== Scale toggle (VNC) ====================
  const toggleScale = () => {
    const rfb = rfbRef.current;
    if (!rfb) return;
    const next = !scaleToFit;
    rfb.scaleViewport = next;
    setScaleToFit(next);
  };

  // ==================== Clipboard → VM (VNC) ====================
  const sendClipboard = () => {
    if (!clipboardText) return;
    rfbRef.current?.clipboardPasteFrom(clipboardText);
    setClipboardOpen(false);
    setClipboardText('');
  };

  // ==================== Screenshot ====================
  const takeScreenshot = () => {
    // querySelectorAll + last(): в dev-режиме React StrictMode временно
    // монтирует RFB дважды, оставляя осиротевший canvas первым в DOM —
    // актуальный (живой) canvas всегда последний.
    const canvases = containerRef.current?.querySelectorAll('canvas');
    const canvas = canvases && canvases.length > 0 ? canvases[canvases.length - 1] : null;
    if (!canvas) {
      toast.error(t('console.screenshot_failed'));
      return;
    }
    const link = document.createElement('a');
    link.download = `console-${type}-${vmid}-${Date.now()}.png`;
    link.href = canvas.toDataURL('image/png');
    link.click();
  };

  // ==================== Power actions ====================
  const runPowerAction = (action: 'restart' | 'shutdown' | 'reset' | 'stop', force?: boolean) => {
    if (
      (action === 'reset' || (action === 'stop' && force)) &&
      !window.confirm(action === 'reset' ? t('console.confirm_hard_reset') : t('console.confirm_hard_stop'))
    ) {
      return;
    }
    power.mutate(
      { action, force },
      {
        onSuccess: () => toast.success(t('console.action_sent')),
        onError: (err) => toast.error(err.message || t('console.action_failed')),
      }
    );
  };

  return (
    <div className="flex h-screen min-h-0 flex-col bg-[#09090B]">
      {/* Toolbar */}
      <div className="flex items-center justify-between border-b border-border/30 bg-[#111113] px-3 py-1.5">
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="text-xs border-border/50">
            {vmTypeLabel(type)} #{vmid}
          </Badge>
          <span className="text-xs text-muted-foreground">Node: {node}</span>
          <Badge
            variant="secondary"
            className={`text-2xs ${
              status === 'connected'
                ? 'bg-green-500/10 text-green-500'
                : status === 'connecting'
                  ? 'bg-amber-500/10 text-amber-500'
                  : 'bg-red-500/10 text-red-500'
            }`}
          >
            {status}
          </Badge>
        </div>
        <div className="flex items-center gap-1">
          {isSerial && (
            <Badge variant="secondary" className="text-2xs bg-blue-500/10 text-blue-500">serial</Badge>
          )}

          {/* Power menu */}
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button variant="ghost" size="sm" className="h-7 text-xs text-muted-foreground" />}>
              <Power className="mr-1 h-3 w-3" />
              {t('console.power')}
              <ChevronDown className="ml-1 h-3 w-3" />
            </DropdownMenuTrigger>
            <DropdownMenuContent className="w-56">
              <DropdownMenuItem onClick={() => runPowerAction('restart')}>
                <RotateCw className="h-3.5 w-3.5" />
                {t('console.reboot')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => runPowerAction('shutdown')}>
                <PowerOff className="h-3.5 w-3.5" />
                {t('console.shutdown')}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              {type === 'qemu' && (
                <DropdownMenuItem variant="destructive" onClick={() => runPowerAction('reset')}>
                  <Zap className="h-3.5 w-3.5" />
                  {t('console.hard_reset')}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem variant="destructive" onClick={() => runPowerAction('stop', true)}>
                <Square className="h-3.5 w-3.5" />
                {t('console.hard_stop')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>

          {isVnc && (
            <>
              {/* Extra keys menu */}
              <DropdownMenu>
                <DropdownMenuTrigger render={<Button variant="ghost" size="sm" className="h-7 text-xs text-muted-foreground" />}>
                  <Keyboard className="mr-1 h-3 w-3" />
                  {t('console.extra_keys')}
                  <ChevronDown className="ml-1 h-3 w-3" />
                </DropdownMenuTrigger>
                <DropdownMenuContent className="w-56">
                  <DropdownMenuItem onClick={sendCtrlAltDel}>{t('console.ctrl_alt_del')}</DropdownMenuItem>
                  <DropdownMenuItem onClick={sendCtrlAltBackspace}>{t('console.ctrl_alt_backspace')}</DropdownMenuItem>
                  <DropdownMenuItem onClick={() => sendKeyCombo(KEYSYM.Tab, 'Tab')}>Tab</DropdownMenuItem>
                  <DropdownMenuItem onClick={() => sendKeyCombo(KEYSYM.Escape, 'Escape')}>Esc</DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>Ctrl+Alt+F1…F12</DropdownMenuSubTrigger>
                    <DropdownMenuSubContent>
                      {FKEY_CODES.map((k) => (
                        <DropdownMenuItem key={k.label} onClick={() => sendCtrlAltF(k.keysym, k.code)}>
                          {k.label}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                </DropdownMenuContent>
              </DropdownMenu>

              {/* Scale toggle */}
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground"
                title={scaleToFit ? t('console.actual_size') : t('console.scale_to_fit')}
                onClick={toggleScale}
              >
                <Scan className="h-3.5 w-3.5" />
              </Button>

              {/* Clipboard */}
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground"
                title={t('console.clipboard')}
                onClick={() => setClipboardOpen(true)}
              >
                <ClipboardPaste className="h-3.5 w-3.5" />
              </Button>

              {/* Screenshot */}
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground"
                title={t('console.screenshot')}
                onClick={takeScreenshot}
              >
                <Camera className="h-3.5 w-3.5" />
              </Button>
            </>
          )}

          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground"
            onClick={toggleFullscreen}
          >
            {isFullscreen ? <Minimize className="h-3.5 w-3.5" /> : <Maximize className="h-3.5 w-3.5" />}
          </Button>
        </div>
      </div>

      <Dialog open={clipboardOpen} onOpenChange={setClipboardOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ClipboardPaste className="h-4 w-4" /> {t('console.clipboard_title')}
            </DialogTitle>
            <DialogDescription>{t('console.clipboard_desc')}</DialogDescription>
          </DialogHeader>
          <textarea
            value={clipboardText}
            onChange={(e) => setClipboardText(e.target.value)}
            placeholder={t('console.clipboard_placeholder')}
            rows={5}
            className="w-full min-w-0 resize-none rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            autoFocus
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setClipboardOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button onClick={sendClipboard} disabled={!clipboardText}>
              {t('console.send')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Console viewport */}
      <div className="relative min-h-0 flex-1 overflow-hidden">
        {status === 'connecting' && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#09090B]">
            <div className="flex flex-col items-center gap-3 text-muted-foreground">
              <Loader2 className="h-8 w-8 animate-spin" />
              <p className="text-sm">{t('console.connecting')}</p>
            </div>
          </div>
        )}

        {status === 'error' && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#09090B]">
            <div className="flex flex-col items-center gap-3 text-center">
              <p className="text-sm text-red-500">{errorMsg}</p>
              <Button
                variant="outline"
                size="sm"
                onClick={startSession}
              >
                {t('console.reconnect')}
              </Button>
            </div>
          </div>
        )}

        <div
          ref={containerRef}
          className="h-full w-full overflow-hidden"
        />
      </div>
    </div>
  );
}
