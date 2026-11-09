import React, { useState } from 'react'
import { Alert } from 'antd'
import { CloudServerOutlined } from '@ant-design/icons'
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

const ICON_SIZE = 46

/** `<protocol>//<host>:<port>/favicon.ico` of a registry page URL, or null when the URL is unusable. */
export function faviconUrl(pageUrl?: string | null): string | null {
  if (!pageUrl) return null
  try {
    const { protocol, origin } = new URL(pageUrl)
    return protocol === 'http:' || protocol === 'https:' ? `${origin}/favicon.ico` : null
  } catch {
    return null
  }
}

/** The registry's favicon, or a generic registry glyph when there is no page URL or the favicon fails to load. */
export function RegistryIcon({ pageUrl }: { pageUrl?: string | null }) {
  const src = faviconUrl(pageUrl)
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const box: React.CSSProperties = {
    width: ICON_SIZE,
    height: ICON_SIZE,
    flex: `0 0 ${ICON_SIZE}px`,
    borderRadius: 10,
    border: `1px solid ${colors.border}`,
    background: colors.hoverSurface,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  }
  if (src && failedSrc !== src) {
    return (
      <div style={box}>
        <img
          src={src}
          alt=""
          referrerPolicy="no-referrer"
          onError={() => setFailedSrc(src)}
          style={{ width: ICON_SIZE, height: ICON_SIZE, objectFit: 'contain' }}
        />
      </div>
    )
  }
  return (
    <div style={box} aria-hidden>
      <CloudServerOutlined style={{ fontSize: 22, color: colors.textSecondary }} />
    </div>
  )
}
