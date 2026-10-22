import { useEffect, useRef, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useSearchParams } from 'react-router';
import { Maximize, Minimize, Loader2, PackageCheck, TerminalSquare } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { fetchWsTicket } from '@/lib/websocket';
import '@xterm/xterm/css/xterm.css';

/**
 * Root-терминал на уровне ноды: установка обновлений (cmd=upgrade → apt dist-upgrade)
 * или обычный shell (cmd=login). Работает через node-termproxy Proxmox,
 * аналогично штатной кнопке «Upgrade» в интерфейсе Proxmox.
 */
export default function NodeShellPage() {
  const { serverId, node } = useParams<{ serverId: string; node: string }>();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation();
  const cmd = searchParams.get('cmd') === 'login' ? 'login' : 'upgrade';

  const [status, setStatus] = useState<'connecting' | 'connected' | 'error'>('connecting');
  const [errorMsg, setErrorMsg] = useState('');
  const [isFullscreen, setIsFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const keepaliveRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const termRef = useRef<{ terminal: { dispose: () => void }; cleanup: () => void } | null>(null);
  // Флаг отмены: connect() асинхронен, а React StrictMode в dev монтирует эффект
  // дважды — без него второй xterm ложится в контейнер поверх первого и видимой
  // остаётся «осиротевшая» пустая сессия.
  const guardRef = useRef<{ cancelled: boolean } | null>(null);

  const sid = Number(serverId);

  const cleanupSession = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (keepaliveRef.current) {
      clearInterval(keepaliveRef.current);
      keepaliveRef.current = null;
    }
    if (termRef.current) {
      termRef.current.cleanup();
      termRef.current.terminal.dispose();
      termRef.current = null;
    }
  }, []);

  const connect = useCallback(async (guard: { cancelled: boolean }) => {
    try {
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
      const params = new URLSearchParams({ cmd, ticket });
      const wsUrl = `${wsProto}//${window.location.host}/proxmox/ws/node-shell/${sid}/${node}?${params}`;
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
        if (event.data instanceof ArrayBuffer) {
          terminal.write(new Uint8Array(event.data));
        } else {
          terminal.write(event.data);
        }
      };

      ws.onclose = () => {
        if (guard.cancelled) return;
        setStatus('error');
        setErrorMsg(t('console.terminal_closed', 'Соединение с терминалом закрыто'));
      };
      ws.onerror = () => {
        if (guard.cancelled) return;
        setStatus('error');
        setErrorMsg(t('console.terminal_error', 'Ошибка соединения с терминалом'));
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

      // Сессию могли отменить, пока грузились чанки xterm. Убираем за собой сами
      // и не трогаем refs — они уже могут указывать на актуальную сессию.
      if (guard.cancelled) {
        window.removeEventListener('resize', handleResize);
        terminal.dispose();
        ws.close();
        return;
      }

      wsRef.current = ws;
      termRef.current = { terminal, cleanup: () => window.removeEventListener('resize', handleResize) };
    } catch (err) {
      if (guard.cancelled) return;
      setStatus('error');
      setErrorMsg(err instanceof Error ? err.message : 'Failed to connect');
    }
  }, [sid, node, cmd, t]);

  const startSession = useCallback(() => {
    if (guardRef.current) guardRef.current.cancelled = true;
    cleanupSession();

    const guard = { cancelled: false };
    guardRef.current = guard;
    setStatus('connecting');
    setErrorMsg('');
    connect(guard);
  }, [connect, cleanupSession]);

  useEffect(() => {
    startSession();
    return () => {
      if (guardRef.current) guardRef.current.cancelled = true;
      cleanupSession();
    };
  }, [startSession, cleanupSession]);

  const toggleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  };

  return (
    <div className="flex h-screen flex-col bg-[#09090B]">
      <div className="flex items-center justify-between border-b border-border/30 bg-[#111113] px-3 py-1.5">
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="text-xs border-border/50">
            {cmd === 'upgrade' ? (
              <><PackageCheck className="mr-1 h-3 w-3" />{t('nodeadm.upgrade', 'Upgrade')}</>
            ) : (
              <><TerminalSquare className="mr-1 h-3 w-3" />Shell</>
            )}
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
        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground" onClick={toggleFullscreen}>
          {isFullscreen ? <Minimize className="h-3.5 w-3.5" /> : <Maximize className="h-3.5 w-3.5" />}
        </Button>
      </div>

      <div className="relative flex-1">
        {status === 'connecting' && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#09090B]">
            <div className="flex flex-col items-center gap-3 text-muted-foreground">
              <Loader2 className="h-8 w-8 animate-spin" />
              <p className="text-sm">{t('console.connecting', 'Подключение…')}</p>
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
                {t('console.reconnect', 'Переподключиться')}
              </Button>
            </div>
          </div>
        )}

        <div ref={containerRef} className="h-full w-full" style={{ minHeight: 'calc(100vh - 40px)' }} />
      </div>
    </div>
  );
}
