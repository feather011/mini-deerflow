/**
 * MCPSettings — TOOL-05 round 2
 *
 * 真实管理页面（替换原"工具与MCP"占位）：
 * - 服务器列表（GET /api/mcp/servers）
 * - 工具表（GET /api/mcp/tools）
 * - 启用 / 停用（POST .../enable）
 * - 三主体授权（POST .../grant / .../revoke）
 *
 * 设计：
 * - 后端 200 才视为成功；失败显示错误且不更新本地状态
 * - inFlight：按 tool 序列化，同工具所有操作（enable/grant/revoke）互斥；
 *   结果覆盖不会乱序（旧响应不会覆盖最新权限显示）
 * - 错误码：500/409 不允许乐观更新；操作失败不掩盖（不把 online 恢复当成成功）
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  grantMcpTool,
  listMcpServers,
  listMcpTools,
  MCP_PRINCIPALS,
  revokeMcpTool,
  setMcpToolEnabled,
  type McpPrincipal,
  type ServerInfo,
  type ToolAuthInfo,
} from '../../shared/api/mcp'
import styles from './MCPSettings.module.css'

export function MCPSettings() {
  const [servers, setServers] = useState<ServerInfo[]>([])
  const [tools, setTools] = useState<ToolAuthInfo[]>([])
  const [inFlight, setInFlight] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (preserveError = false) => {
    try {
      const [sv, tl] = await Promise.all([listMcpServers(), listMcpTools()])
      setServers(sv.servers)
      setTools(tl.tools)
      // preserveError：操作失败后同步刷新时不得清掉已展示的错误
      if (!preserveError) setError(null)
    } catch (e) {
      if (!preserveError) setError(`加载失败: ${(e as Error).message}`)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // R1/R4 同一守卫模式：先写 ref（同 tick 可见）再 setState，防双击竞态
  const inFlightRef = useRef<Set<string>>(new Set())
  const tryAddFlight = (key: string): boolean => {
    if (inFlightRef.current.has(key)) return false
    inFlightRef.current.add(key)
    setInFlight(new Set(inFlightRef.current))
    return true
  }
  const removeFlight = (key: string) => {
    inFlightRef.current.delete(key)
    setInFlight(new Set(inFlightRef.current))
  }

  /** 后端 200 才更新本地；失败保留旧值并显示错误（不把连通恢复当成功） */
  const applyOrError = useCallback(
    async (op: () => Promise<ToolAuthInfo>, opKey: string) => {
      if (!tryAddFlight(opKey)) return
      try {
        const updated = await op()
        setTools((prev) =>
          prev.map((t) =>
            t.qualified_name === updated.qualified_name ? updated : t,
          ),
        )
        setError(null)
      } catch (e) {
        setError(`操作失败: ${(e as Error).message}`)
        // 失败时再拉一次以同步（preserveError：不清掉上面的错误）
        try {
          await refresh(true)
        } catch {
          /* ignore */
        }
      } finally {
        removeFlight(opKey)
      }
    },
    [refresh],
  )

  return (
    <div className={styles.mcp} data-testid="mcp-settings">
      {error !== null ? (
        <div className={styles.error} role="alert">
          {error}
        </div>
      ) : null}

      <div className={styles.section}>
        <div className={styles.sectionTitle}>服务器</div>
        {servers.length === 0 ? (
          <div className={styles.empty}>无服务器</div>
        ) : (
          servers.map((s) => (
            <div className={styles.serverRow} key={s.name} data-testid={`server-${s.name}`}>
              <span
                className={`${styles.dot} ${
                  s.running
                    ? styles.dotRunning
                    : s.status === 'failed'
                      ? styles.dotFailed
                      : styles.dotStopped
                }`}
              />
              <span className={styles.serverName}>{s.name}</span>
              <span className={styles.serverMeta}>
                {s.status} · {s.tool_count} 工具
              </span>
              {s.failure_message ? (
                <span className={styles.serverMeta} title={s.failure_kind ?? ''}>
                  {s.failure_message}
                </span>
              ) : null}
            </div>
          ))
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>工具策略</div>
        {tools.length === 0 ? (
          <div className={styles.empty}>无工具</div>
        ) : (
          tools.map((t) => {
            const granted = new Set(t.allowed_principals)
            return (
              <div className={styles.toolRow} key={t.qualified_name}>
                <span
                  className={styles.toolName}
                  title={t.server_name}
                  data-testid={`tool-name-${t.qualified_name}`}
                >
                  {t.qualified_name}
                </span>
                <span className={styles.toggle}>
                  <button
                    className={styles.btn}
                    data-testid={`toggle-${t.qualified_name}`}
                    onClick={() =>
                      void applyOrError(
                        () => setMcpToolEnabled(t.qualified_name, !t.enabled),
                        t.qualified_name,
                      )
                    }
                    disabled={inFlight.has(t.qualified_name)}
                  >
                    {t.enabled ? '✓ 启用' : '✗ 停用'}
                  </button>
                </span>
                <span className={styles.principals}>
                  {MCP_PRINCIPALS.map((p) => {
                    const isGranted = granted.has(p)
                    // R1：per-tool key，同工具所有操作序列化，防止旧响应覆盖最新权限
                    const flightKey = t.qualified_name
                    return (
                      <span
                        key={p}
                        className={`${styles.principalPill} ${
                          isGranted ? styles.principalPillGranted : ''
                        } ${inFlight.has(flightKey) ? styles.principalPillPending : ''}`}
                        data-testid={`principal-${t.qualified_name}-${p}`}
                      >
                        {p}
                        <button
                          className={`${styles.btn} ${isGranted ? styles.btnRevoke : ''}`}
                          data-testid={`principal-btn-${t.qualified_name}-${p}`}
                          onClick={() => {
                            const principal = p as McpPrincipal
                            void applyOrError(
                              () =>
                                isGranted
                                  ? revokeMcpTool(t.qualified_name, principal)
                                  : grantMcpTool(t.qualified_name, principal),
                              flightKey,
                            )
                          }}
                          disabled={inFlight.has(flightKey)}
                        >
                          {isGranted ? '撤销' : '授权'}
                        </button>
                      </span>
                    )
                  })}
                </span>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
