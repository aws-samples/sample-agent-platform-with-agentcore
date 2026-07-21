import { useEffect, useRef, useCallback, useState } from 'react'

type WsStatus = 'disconnected' | 'connecting' | 'connected' | 'error' | 'reconnecting'

interface UseWebSocketOptions {
  url: string | null
  onMessage?: (data: ArrayBuffer | string) => void
  onOpen?: () => void
  onClose?: () => void
  /** Pre-signed WSS URLs expire (~5 min); ask the backend for a fresh one. */
  onReconnectUrl?: () => Promise<string | null>
  maxReconnectAttempts?: number
}

const KEEPALIVE_INTERVAL = 20_000
const RECONNECT_DELAY_BASE = 2_000

export function useWebSocket({
  url,
  onMessage,
  onOpen,
  onClose,
  onReconnectUrl,
  maxReconnectAttempts = 3,
}: UseWebSocketOptions) {
  const wsRef = useRef<WebSocket | null>(null)
  const keepaliveRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const urlRef = useRef(url)
  const [status, setStatus] = useState<WsStatus>('disconnected')
  const reconnectAttemptRef = useRef(0)
  const intentionalCloseRef = useRef(false)

  urlRef.current = url

  const clearKeepalive = useCallback(() => {
    if (keepaliveRef.current) {
      clearInterval(keepaliveRef.current)
      keepaliveRef.current = null
    }
  }, [])

  // Neutralize a socket before closing it. Without this, the superseded
  // socket's async `onclose` fires after a new connect() has already reset
  // intentionalCloseRef, gets treated as an unexpected drop, and spawns a
  // second live connection feeding the same terminal.
  const retire = useCallback(
    (ws: WebSocket | null) => {
      if (!ws) return
      ws.onopen = null
      ws.onmessage = null
      ws.onclose = null
      ws.onerror = null
      clearKeepalive()
      try {
        ws.close()
      } catch {
        /* already closed */
      }
    },
    [clearKeepalive],
  )

  const connectWithUrl = useCallback(
    (wsUrl: string) => {
      if (wsRef.current) {
        retire(wsRef.current)
        wsRef.current = null
      }
      setStatus('connecting')

      const ws = new WebSocket(wsUrl)
      ws.binaryType = 'arraybuffer'

      ws.onopen = () => {
        setStatus('connected')
        reconnectAttemptRef.current = 0
        // Whitespace keepalive: refreshes AgentCore's activity clock without
        // reaching the terminal (contract-server filters it).
        keepaliveRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(' ')
        }, KEEPALIVE_INTERVAL)
        onOpen?.()
      }

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return
        onMessage?.(event.data)
      }

      ws.onclose = () => {
        if (wsRef.current !== ws) return
        clearKeepalive()
        wsRef.current = null

        if (intentionalCloseRef.current) {
          setStatus('disconnected')
          onClose?.()
          return
        }

        if (onReconnectUrl && reconnectAttemptRef.current < maxReconnectAttempts) {
          setStatus('reconnecting')
          reconnectAttemptRef.current++
          const delay = RECONNECT_DELAY_BASE * Math.pow(2, reconnectAttemptRef.current - 1)
          setTimeout(async () => {
            try {
              const newUrl = await onReconnectUrl()
              if (newUrl) {
                urlRef.current = newUrl
                // Give a cold container time to boot after the warmup invoke
                await new Promise((r) => setTimeout(r, 5000))
                if (!intentionalCloseRef.current) connectWithUrl(newUrl)
              } else {
                setStatus('error')
                onClose?.()
              }
            } catch {
              setStatus('error')
              onClose?.()
            }
          }, delay)
        } else {
          setStatus('error')
          onClose?.()
        }
      }

      ws.onerror = () => {
        if (wsRef.current !== ws) return
        setStatus('error')
      }

      wsRef.current = ws
    },
    [onMessage, onOpen, onClose, onReconnectUrl, maxReconnectAttempts, clearKeepalive, retire],
  )

  const connect = useCallback(() => {
    if (!urlRef.current) return
    intentionalCloseRef.current = false
    reconnectAttemptRef.current = 0
    connectWithUrl(urlRef.current)
  }, [connectWithUrl])

  const disconnect = useCallback(() => {
    intentionalCloseRef.current = true
    retire(wsRef.current)
    wsRef.current = null
    setStatus('disconnected')
  }, [retire])

  const send = useCallback((data: string | ArrayBuffer) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(data)
    }
  }, [])

  useEffect(() => {
    return () => {
      intentionalCloseRef.current = true
      retire(wsRef.current)
      wsRef.current = null
    }
  }, [retire])

  return { status, connect, disconnect, send }
}
