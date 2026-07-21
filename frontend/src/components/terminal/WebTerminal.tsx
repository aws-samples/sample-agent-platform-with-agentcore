import { useEffect, useRef, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { useWebSocket } from '@/hooks/useWebSocket'

interface WebTerminalProps {
  websocketUrl: string | null
  sessionId?: string
  onReconnectUrl?: () => Promise<string | null>
}

// ttyd binary protocol — first byte is the message type
const TTYD_INPUT = 0x30 // '0' client→server input
const TTYD_RESIZE = 0x31 // '1' client→server resize
const TTYD_OUTPUT = 0x30 // '0' server→client output

function encodeInput(data: string): ArrayBuffer {
  const payload = new TextEncoder().encode(data)
  const buf = new Uint8Array(1 + payload.length)
  buf[0] = TTYD_INPUT
  buf.set(payload, 1)
  return buf.buffer
}

function encodeResize(cols: number, rows: number): ArrayBuffer {
  const payload = new TextEncoder().encode(JSON.stringify({ columns: cols, rows }))
  const buf = new Uint8Array(1 + payload.length)
  buf[0] = TTYD_RESIZE
  buf.set(payload, 1)
  return buf.buffer
}

/** ttyd expects a JSON handshake as the first client text frame. */
function encodeHandshake(cols: number, rows: number): string {
  return JSON.stringify({ AuthToken: '', columns: cols, rows })
}

const STATUS_STYLE: Record<string, string> = {
  disconnected: 'bg-slate-400',
  connecting: 'bg-amber-400 animate-pulse',
  connected: 'bg-emerald-400',
  reconnecting: 'bg-amber-400 animate-pulse',
  error: 'bg-red-400',
}

export default function WebTerminal({ websocketUrl, sessionId, onReconnectUrl }: WebTerminalProps) {
  const termRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<Terminal | null>(null)
  const handshakeSentRef = useRef(false)

  const handleMessage = useCallback((data: ArrayBuffer | string) => {
    const term = xtermRef.current
    if (!term) return

    if (typeof data === 'string') {
      try {
        const parsed = JSON.parse(data)
        if (parsed.columns && parsed.rows) term.resize(parsed.columns, parsed.rows)
      } catch {
        term.write(data)
      }
      return
    }

    const bytes = new Uint8Array(data)
    if (bytes.length < 1) return
    if (bytes[0] === TTYD_OUTPUT) {
      term.write(bytes.slice(1))
    }
    // 0x31 (title) / 0x32 (preferences) frames are ignored
  }, [])

  const { status, connect, disconnect, send } = useWebSocket({
    url: websocketUrl,
    onMessage: handleMessage,
    onOpen: useCallback(() => {
      handshakeSentRef.current = false
    }, []),
    onClose: useCallback(() => {
      handshakeSentRef.current = false
    }, []),
    onReconnectUrl,
  })

  useEffect(() => {
    if (status === 'reconnecting') {
      xtermRef.current?.write('\r\n*** Reconnecting… ***\r\n')
    } else if (status === 'error') {
      xtermRef.current?.write('\r\n*** Connection lost — reopen the session to retry ***\r\n')
    }
  }, [status])

  // First frame after connect must be the ttyd handshake
  useEffect(() => {
    if (status === 'connected' && xtermRef.current && !handshakeSentRef.current) {
      const term = xtermRef.current
      send(encodeHandshake(term.cols, term.rows))
      handshakeSentRef.current = true
    }
  }, [status, send])

  useEffect(() => {
    if (!termRef.current) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      theme: { background: '#0f172a', foreground: '#e2e8f0' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    term.open(termRef.current)
    fit.fit()

    term.onData((data) => send(encodeInput(data)))
    term.onResize(({ cols, rows }) => {
      if (handshakeSentRef.current) send(encodeResize(cols, rows))
    })

    xtermRef.current = term

    const resizeObs = new ResizeObserver(() => fit.fit())
    resizeObs.observe(termRef.current)

    return () => {
      resizeObs.disconnect()
      term.dispose()
      disconnect()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (websocketUrl) {
      handshakeSentRef.current = false
      disconnect()
      connect()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [websocketUrl])

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-700 bg-[#0f172a]">
      <div className="flex items-center gap-2 border-b border-slate-700 px-3 py-1.5">
        <span className={`h-2 w-2 rounded-full ${STATUS_STYLE[status]}`} />
        <span className="text-xs text-slate-300">{status}</span>
        {sessionId && (
          <span className="font-mono text-[11px] text-slate-500">{sessionId.slice(0, 12)}</span>
        )}
      </div>
      <div ref={termRef} className="min-h-0 flex-1 p-1" />
    </div>
  )
}
