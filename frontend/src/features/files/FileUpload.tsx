/**
 * FileUpload — 文件选择 + 上传按钮（20E）
 *
 * 职责：
 * - 选择本地文件
 * - 触发上传
 * - 上传 pending 时禁止重复提交
 * - 失败时保留选择以便重试，成功后清空选择
 * - 显示错误信息和 Request ID
 */
import { useRef, useState } from 'react'
import { useUploadFile } from './hooks'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import type { ApiError } from '../../shared/api'
import styles from './FileUpload.module.css'

export function FileUpload() {
  const [file, setFile] = useState<File | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const upload = useUploadFile()

  const isPending = upload.isPending
  const error = upload.error as ApiError | null

  function handlePick(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = e.target.files?.[0] ?? null
    setFile(picked)
    upload.reset()
  }

  async function handleSubmit() {
    if (!file) return
    try {
      await upload.mutateAsync({ file })
      // 成功后清空选择
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
    } catch {
      // 失败时保留选择以便重试
    }
  }

  return (
    <div className={styles.upload}>
      <div className={styles.row}>
        <input
          ref={inputRef}
          type="file"
          className={styles.fileInput}
          onChange={handlePick}
          disabled={isPending}
          aria-label="选择要上传的文件"
        />
        <button
          type="button"
          className={styles.uploadBtn}
          onClick={handleSubmit}
          disabled={!file || isPending}
        >
          {isPending ? '上传中…' : '上传'}
        </button>
      </div>

      {file && (
        <div className={styles.picked}>
          已选择：{file.name}（{(file.size / 1024).toFixed(1)} KB）
        </div>
      )}

      {error && (
        <ErrorNotice
          error={error}
          context="file"
          ariaLabel="上传错误"
        />
      )}
    </div>
  )
}
