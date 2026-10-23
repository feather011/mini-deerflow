/**
 * SettingsDialog — 设置对话框（TOOL-05：MCP 标签页替换为真实管理）
 */
import { useState } from 'react'
import type { KeyboardEvent, JSX } from 'react'
import { MCPSettings } from './MCPSettings'
import styles from './SettingsDialog.module.css'

type SettingsTab = '模型' | '上下文与记忆' | '工具与MCP' | '权限与日志'

const TABS: readonly SettingsTab[] = [
  '模型',
  '上下文与记忆',
  '工具与MCP',
  '权限与日志',
]

interface SettingsState {
  mainModel: string
  workerModel: string
  endpoint: string
  historyCompaction: boolean
  projectMemory: boolean
  showBudget: boolean
  workspaceOnly: boolean
  showOpLog: boolean
  autoCommit: boolean
  logLevel: string
}

const INITIAL_STATE: SettingsState = {
  mainModel: '默认模型',
  workerModel: '继承主模型配置',
  endpoint: '',
  historyCompaction: true,
  projectMemory: true,
  showBudget: true,
  workspaceOnly: true,
  showOpLog: true,
  autoCommit: false,
  logLevel: 'info',
}

interface SwitchRowProps {
  id: string
  label: string
  desc: string
  checked: boolean
  onChange: (checked: boolean) => void
}

function SwitchRow({ id, label, desc, checked, onChange }: SwitchRowProps) {
  return (
    <div className={styles.row}>
      <label className={styles.rowText} htmlFor={id}>
        <span className={styles.rowLabel}>{label}</span>
        <span className={styles.rowDesc}>{desc}</span>
      </label>
      <input
        id={id}
        type="checkbox"
        className={styles.switch}
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
    </div>
  )
}

export function SettingsDialog(props: {
  open: boolean
  onClose: () => void
}): JSX.Element {
  const { open, onClose } = props
  const [tab, setTab] = useState<SettingsTab>('模型')
  const [state, setState] = useState<SettingsState>(INITIAL_STATE)
  const [toast, setToast] = useState<string | null>(null)

  if (!open) return <></>

  function update<K extends keyof SettingsState>(
    key: K,
    value: SettingsState[K],
  ) {
    setState((prev) => ({ ...prev, [key]: value }))
  }

  function handleSave() {
    // 纯本地提示：不发请求、不写任何存储
    setToast('演示设置已确认 · 未保存到后端')
  }

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') onClose()
  }

  function selectTab(next: SettingsTab) {
    setTab(next)
    setToast(null)
  }

  function renderGroup(): JSX.Element {
    switch (tab) {
      case '模型':
        return (
          <>
            <div className={styles.field}>
              <label className={styles.label} htmlFor="settings-main-model">
                主 Agent 模型
              </label>
              <select
                id="settings-main-model"
                className={styles.select}
                value={state.mainModel}
                onChange={(e) => update('mainModel', e.target.value)}
              >
                <option value="默认模型">默认模型</option>
                <option value="高质量模型（示例）">高质量模型（示例）</option>
              </select>
            </div>

            <div className={styles.field}>
              <label className={styles.label} htmlFor="settings-worker-model">
                执行 Agent 模型
              </label>
              <select
                id="settings-worker-model"
                className={styles.select}
                value={state.workerModel}
                onChange={(e) => update('workerModel', e.target.value)}
              >
                <option value="继承主模型配置">继承主模型配置</option>
                <option value="独立编码模型（示例）">
                  独立编码模型（示例）
                </option>
              </select>
            </div>

            <div className={styles.field}>
              <label className={styles.label} htmlFor="settings-endpoint">
                模型服务地址
              </label>
              <input
                id="settings-endpoint"
                type="text"
                className={styles.input}
                value={state.endpoint}
                placeholder="http://127.0.0.1:8000/v1（演示）"
                onChange={(e) => update('endpoint', e.target.value)}
              />
              <p className={styles.hint}>
                演示输入，不校验地址、Key 或连接。
              </p>
            </div>
          </>
        )
      case '上下文与记忆':
        return (
          <>
            <SwitchRow
              id="settings-history-compaction"
              label="启用历史压缩"
              desc="主 Agent 与执行 Agent 保留各自的上下文机制"
              checked={state.historyCompaction}
              onChange={(v) => update('historyCompaction', v)}
            />
            <SwitchRow
              id="settings-project-memory"
              label="项目记忆隔离"
              desc="按项目与会话管理记忆范围"
              checked={state.projectMemory}
              onChange={(v) => update('projectMemory', v)}
            />
            <SwitchRow
              id="settings-show-budget"
              label="显示输入预算"
              desc="统计注明引擎来源与估算口径"
              checked={state.showBudget}
              onChange={(v) => update('showBudget', v)}
            />
          </>
        )
      case '工具与MCP':
        return <MCPSettings />
      case '权限与日志':
        return (
          <>
            <SwitchRow
              id="settings-workspace-only"
              label="限制在绑定工作区"
              desc="目录由项目事实决定，不接受模型覆盖"
              checked={state.workspaceOnly}
              onChange={(v) => update('workspaceOnly', v)}
            />
            <SwitchRow
              id="settings-show-op-log"
              label="展示操作记录"
              desc="保留任务与工具事件"
              checked={state.showOpLog}
              onChange={(v) => update('showOpLog', v)}
            />
            <SwitchRow
              id="settings-auto-commit"
              label="自动提交与推送"
              desc="初版不支持，由用户自行处理"
              checked={state.autoCommit}
              onChange={(v) => update('autoCommit', v)}
            />
            <div className={styles.field}>
              <label className={styles.label} htmlFor="settings-log-level">
                日志级别
              </label>
              <select
                id="settings-log-level"
                className={styles.select}
                value={state.logLevel}
                onChange={(e) => update('logLevel', e.target.value)}
              >
                <option value="info">info</option>
                <option value="debug">debug</option>
                <option value="warn">warn</option>
                <option value="error">error</option>
              </select>
            </div>
          </>
        )
    }
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-label="设置"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div className={styles.header}>
          <h2 className={styles.title}>设置</h2>
        </div>

        <div className={styles.body}>
          <div className={styles.tabs} role="tablist" aria-label="设置分组">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                className={`${styles.tab} ${tab === t ? styles.tabActive : ''}`}
                onClick={() => selectTab(t)}
              >
                {t}
              </button>
            ))}
          </div>

          <div
            className={styles.group}
            role="tabpanel"
            aria-label={tab}
          >
            {renderGroup()}
          </div>

          {toast !== null && (
            <p className={styles.toast} role="status">
              {toast}
            </p>
          )}
        </div>

        <div className={styles.footer}>
          <p className={styles.notice}>
            原型设置：仅影响本页演示状态，不保存、不验证连接
          </p>
          <div className={styles.actions}>
            <button type="button" className={styles.btn} onClick={onClose}>
              关闭
            </button>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnPrimary}`}
              onClick={handleSave}
            >
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
