import { Button } from "@/components/ui/button";
import api from "@/lib/api/client";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { ArrowLeft, Maximize2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";

type ConnState = "connecting" | "connected" | "disconnected" | "error";

function buildWsUrl(stackId: string, serviceName: string, token: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  return `${proto}//${host}/api/stacks/${stackId}/terminal/${serviceName}?token=${encodeURIComponent(token)}`;
}

export function TerminalPage() {
  const { stackId, serviceName } = useParams<{ stackId: string; serviceName: string }>();
  const navigate = useNavigate();
  const termRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [connState, setConnState] = useState<ConnState>("connecting");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (!stackId || !serviceName) return;

    setConnState("connecting");
    setErrorMsg(null);

    const term = xtermRef.current;
    if (term) term.clear();

    // Fetch a short-lived terminal ticket so the full access JWT never appears
    // in server logs or browser history via the WebSocket query string.
    void (async () => {
      let ticket: string;
      try {
        const { data, error } = await (api as any).GET(
          `/api/stacks/${stackId}/terminal/${serviceName}/ticket`
        );
        if (error || !data?.ticket) {
          setConnState("error");
          setErrorMsg("Not authenticated — please refresh the page.");
          return;
        }
        ticket = data.ticket as string;
      } catch {
        setConnState("error");
        setErrorMsg("Failed to obtain terminal ticket — please refresh the page.");
        return;
      }

      const url = buildWsUrl(stackId, serviceName, ticket);
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        setConnState("connected");
        sendResize();
      };

      ws.onmessage = (evt) => {
        if (!xtermRef.current) return;
        if (evt.data instanceof ArrayBuffer) {
          xtermRef.current.write(new Uint8Array(evt.data));
        } else if (typeof evt.data === "string") {
          try {
            const msg = JSON.parse(evt.data) as { type: string; message?: string };
            if (msg.type === "error" && msg.message) {
              setConnState("error");
              setErrorMsg(msg.message);
              xtermRef.current.writeln(`\r\n\x1b[31mError: ${msg.message}\x1b[0m`);
            }
          } catch {
            xtermRef.current.write(evt.data);
          }
        }
      };

      ws.onerror = () => {
        setConnState("error");
        setErrorMsg("Connection failed — check that the container is running.");
      };

      ws.onclose = () => {
        if (connState !== "error") setConnState("disconnected");
      };
    })();
  }, [stackId, serviceName]); // eslint-disable-line react-hooks/exhaustive-deps

  const sendResize = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!xtermRef.current) return;
    const { cols, rows } = xtermRef.current;
    wsRef.current.send(JSON.stringify({ type: "resize", cols, rows }));
  }, []);

  // Mount xterm once
  useEffect(() => {
    if (!termRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: '"Cascadia Code", "Fira Code", Menlo, monospace',
      theme: {
        background: "#0d0d0d",
        foreground: "#e4e4e4",
        cursor: "#e4e4e4",
        selectionBackground: "#3c3c3c",
        black: "#1e1e1e",
        red: "#f44747",
        green: "#4ec9b0",
        yellow: "#dcdcaa",
        blue: "#569cd6",
        magenta: "#c586c0",
        cyan: "#9cdcfe",
        white: "#d4d4d4",
        brightBlack: "#555555",
        brightRed: "#f44747",
        brightGreen: "#6a9955",
        brightYellow: "#dcdcaa",
        brightBlue: "#569cd6",
        brightMagenta: "#c586c0",
        brightCyan: "#9cdcfe",
        brightWhite: "#ffffff",
      },
      allowProposedApi: true,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(termRef.current);
    fitAddon.fit();

    xtermRef.current = term;
    fitRef.current = fitAddon;

    // Send keyboard input to WebSocket
    term.onData((data) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(new TextEncoder().encode(data));
      }
    });

    // Resize handler
    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit();
      sendResize();
    });
    resizeObserver.observe(termRef.current);

    return () => {
      resizeObserver.disconnect();
      term.dispose();
      xtermRef.current = null;
      fitRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Connect on mount and when reconnect is triggered
  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
    };
  }, [connect]);

  // Fullscreen toggle
  const toggleFullscreen = useCallback(() => {
    const el = termRef.current?.parentElement;
    if (!el) return;
    if (!document.fullscreenElement) {
      void el.requestFullscreen();
    } else {
      void document.exitFullscreen();
    }
  }, []);

  const stateColor: Record<ConnState, string> = {
    connecting: "text-yellow-500",
    connected: "text-green-500",
    disconnected: "text-muted-foreground",
    error: "text-destructive",
  };

  const stateLabel: Record<ConnState, string> = {
    connecting: "Connecting…",
    connected: "Connected",
    disconnected: "Disconnected",
    error: "Error",
  };

  return (
    <div className="flex h-screen flex-col bg-background">
      {/* Top bar */}
      <div className="flex items-center justify-between border-b px-4 py-2 shrink-0">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => navigate(`/stacks/${stackId}`)}
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <span className="text-sm font-medium font-mono">
              {stackId} / {serviceName}
            </span>
            <span className={`ml-2 text-xs ${stateColor[connState]}`}>
              ● {stateLabel[connState]}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {(connState === "disconnected" || connState === "error") && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 gap-1 text-xs"
              onClick={() => connect()}
            >
              <RefreshCw className="h-3 w-3" /> Reconnect
            </Button>
          )}
          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={toggleFullscreen}>
            <Maximize2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Error banner */}
      {connState === "error" && errorMsg && (
        <div className="shrink-0 border-b border-destructive/30 bg-destructive/10 px-4 py-2 text-sm text-destructive">
          {errorMsg}
        </div>
      )}

      {/* Terminal */}
      <div className="relative flex-1 overflow-hidden bg-[#0d0d0d]">
        <div ref={termRef} className="h-full w-full p-1" />
        {connState === "connecting" && (
          <div className="absolute inset-0 flex items-center justify-center bg-[#0d0d0d]/80">
            <p className="text-sm text-muted-foreground animate-pulse">Connecting to container…</p>
          </div>
        )}
      </div>
    </div>
  );
}
