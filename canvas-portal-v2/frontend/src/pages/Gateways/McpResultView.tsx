import { t } from '@/i18n'
import React from 'react'
import { Alert, Collapse, Space, Tabs, Tag } from 'antd'
import type { McpCallOutcome, McpExchange } from '@/api/aiGateways'
import { colors } from '@/theme'

const preStyle: React.CSSProperties = {
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
  fontFamily: 'monospace',
  fontSize: 13,
  margin: 0,
  color: colors.textPrimary,
}
const labelStyle: React.CSSProperties = { fontSize: 12, fontWeight: 600, color: colors.textSecondary, marginBottom: 4 }
const panelStyle: React.CSSProperties = {
  maxHeight: 360,
  overflowY: 'auto',
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
  padding: 12,
  background: colors.surfaceSubtle,
}

// SVG is left out because it can carry scripts, and the CSP blocks audio and video.
const RENDERED_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/

type Item = Record<string, unknown>

const asItems = (value: unknown): Item[] =>
  Array.isArray(value) ? value.filter((item): item is Item => typeof item === 'object' && item !== null) : []

const asText = (value: unknown): string => (typeof value === 'string' ? value : '')

function Binary({ mimeType, data }: { mimeType: string; data: string }) {
  if (RENDERED_IMAGE_TYPES.has(mimeType) && BASE64.test(data)) {
    return <img alt="Returned by the MCP server" src={`data:${mimeType};base64,${data}`} style={{ maxWidth: '100%' }} />
  }
  // About three quarters of the base64 length.
  const size = Math.floor((data.length * 3) / 4)
  return (
    <span style={{ fontSize: 12, color: colors.textMuted }}>
      {mimeType || 'Binary content'}, about {size.toLocaleString()} bytes. This can't be previewed here.
    </span>
  )
}

/** A resource's contents, on their own or embedded in a tool result. Text is never rendered as HTML. */
function ResourceContents({ item }: { item: Item }) {
  const uri = asText(item.uri)
  return (
    <div>
      {uri && <div style={labelStyle}>{uri}</div>}
      {typeof item.blob === 'string' ? (
        <Binary mimeType={asText(item.mimeType)} data={item.blob} />
      ) : (
        <pre style={preStyle}>{asText(item.text)}</pre>
      )}
    </div>
  )
}

function ContentItem({ item }: { item: Item }) {
  switch (item.type) {
    case 'text':
      return <pre style={preStyle}>{asText(item.text)}</pre>
    case 'image':
    case 'audio':
      return <Binary mimeType={asText(item.mimeType)} data={asText(item.data)} />
    case 'resource':
      return <ResourceContents item={(item.resource ?? {}) as Item} />
    case 'resource_link':
      return <pre style={preStyle}>{`${asText(item.name) || 'Resource'}: ${asText(item.uri)}`}</pre>
    default:
      return (
        <span style={{ fontSize: 12, color: colors.textMuted }}>
          Unknown content type {String(item.type)}. See the Protocol tab.
        </span>
      )
  }
}

type McpOutcomeKind = 'success' | 'toolError' | 'protocolError' | 'failed'

function outcomeOf(outcome: McpCallOutcome, callMethod: string): { kind: McpOutcomeKind; message: string } {
  if (outcome.error) return { kind: 'failed', message: outcome.error }
  const call = outcome.exchanges[outcome.exchanges.length - 1]
  if (!call || call.method !== callMethod || !call.response || Object.keys(call.response).length === 0) {
    return { kind: 'failed', message: 'The MCP endpoint sent no response to this call.' }
  }
  if (call.response.error) {
    const { code, message } = call.response.error
    return { kind: 'protocolError', message: `${message ?? 'Unknown error'}${code === undefined ? '' : ` (code ${code})`}` }
  }
  return (call.response.result as Item | undefined)?.isError === true
    ? { kind: 'toolError', message: '' }
    : { kind: 'success', message: '' }
}

const OUTCOME_TAGS: Record<McpOutcomeKind, { color: string; label: string }> = {
  success: { color: 'green', label: t("Success") },
  toolError: { color: 'red', label: 'Tool returned an error' },
  protocolError: { color: 'red', label: 'JSON-RPC error' },
  failed: { color: 'red', label: 'Call failed' },
}

function ResultPane({ outcome, callMethod, kind, message }: {
  outcome: McpCallOutcome
  callMethod: string
  kind: McpOutcomeKind
  message: string
}) {
  if (kind === 'failed' || kind === 'protocolError') return <Alert type="error" showIcon message={message} />
  const result = (outcome.exchanges[outcome.exchanges.length - 1].response?.result ?? {}) as Item
  const items = asItems(callMethod === 'tools/call' ? result.content : result.contents)
  const structured = result.structuredContent
  return (
    <div style={panelStyle}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {items.length === 0 && structured === undefined && (
          <span style={{ fontSize: 12, color: colors.textMuted }}>The server returned no content.</span>
        )}
        {items.map((item, index) =>
          callMethod === 'tools/call' ? (
            <ContentItem key={index} item={item} />
          ) : (
            <ResourceContents key={index} item={item} />
          ),
        )}
        {structured !== undefined && (
          <div data-testid="structured-output">
            <div style={labelStyle}>Structured content</div>
            <pre style={preStyle}>{JSON.stringify(structured, null, 2)}</pre>
          </div>
        )}
      </Space>
    </div>
  )
}

function ProtocolPane({ exchanges }: { exchanges: McpExchange[] }) {
  if (exchanges.length === 0) {
    return <span style={{ fontSize: 12, color: colors.textMuted }}>No messages were exchanged.</span>
  }
  return (
    <div style={{ maxHeight: 360, overflowY: 'auto' }}>
      <Collapse
        size="small"
        items={exchanges.map((exchange, index) => ({
          key: index,
          label: (
            <Space size={8}>
              <code>{exchange.method}</code>
              {exchange.status !== null && (
                <span style={{ fontSize: 12, color: colors.textMuted }}>HTTP {exchange.status}</span>
              )}
            </Space>
          ),
          children: (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <div>
                <div style={labelStyle}>{t("Request")}</div>
                <pre style={preStyle}>{JSON.stringify(exchange.request, null, 2)}</pre>
              </div>
              <div>
                <div style={labelStyle}>{t("Response")}</div>
                <pre style={preStyle}>
                  {exchange.response ? JSON.stringify(exchange.response, null, 2) : 'No JSON-RPC response.'}
                </pre>
              </div>
            </Space>
          ),
        }))}
      />
    </div>
  )
}

export default function McpResultView({ outcome, callMethod }: { outcome: McpCallOutcome; callMethod: string }) {
  const { kind, message } = outcomeOf(outcome, callMethod)
  const tag = OUTCOME_TAGS[kind]
  return (
    <div>
      <div aria-live="polite" style={{ marginBottom: 8 }}>
        <Tag color={tag.color}>{tag.label}</Tag>
      </div>
      <Tabs
        size="small"
        items={[
          {
            key: 'result',
            label: t("Result"),
            children: <ResultPane outcome={outcome} callMethod={callMethod} kind={kind} message={message} />,
          },
          { key: 'protocol', label: t("Protocol"), children: <ProtocolPane exchanges={outcome.exchanges} /> },
        ]}
      />
    </div>
  )
}
