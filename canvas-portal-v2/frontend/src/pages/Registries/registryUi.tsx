import React from 'react'
import { Alert } from 'antd'
import type { ConnectionTestResult } from '@/api/registries'
import { colors } from '@/theme'

export const subtleButton = {
  background: 'transparent',
  border: `1px solid ${colors.border}`,
  color: colors.textSecondary,
}

/** Outcome of a registry connection test, with any warnings listed below the message. */
export function TestResultAlert({ result, onClose }: { result: ConnectionTestResult; onClose?: () => void }) {
  const levelNote = result.level === 'admin' ? ' These credentials can read every review state.' : ''
  return (
    <Alert
      type={result.ok ? (result.warnings.length ? 'warning' : 'success') : 'error'}
      showIcon
      closable={!!onClose}
      onClose={onClose}
      message={`${result.message}${levelNote}`}
      description={
        result.warnings.length ? (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {result.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        ) : undefined
      }
      style={{ marginBottom: 12 }}
    />
  )
}
