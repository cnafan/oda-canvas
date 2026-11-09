import { t } from '@/i18n'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import axios from 'axios'
import { Alert, Button, Modal, Space, Spin, Tag } from 'antd'
import {
  callMcpTool,
  readMcpResource,
  type McpCallOutcome,
  type McpResource,
  type McpTool,
} from '@/api/aiGateways'
import { useAuth } from '@/hooks/useAuth'
import { colors } from '@/theme'
import McpArgumentsForm, { buildArguments, initialDraft } from './McpArgumentsForm'
import McpResultView from './McpResultView'

export type InspectedItem = { kind: 'tool'; tool: McpTool } | { kind: 'resource'; resource: McpResource }

type CallState =
  | { status: 'idle' }
  | { status: 'running' }
  | { status: 'done'; outcome: McpCallOutcome }
  | { status: 'refused'; message: string }

function refusalMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail: unknown = err.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (err.response) return `Request failed: HTTP ${err.response.status}`
  }
  return 'The request failed.'
}

/** Runs one call at a time. Closing the dialog stops waiting, but not a call already running upstream. */
function useMcpCall() {
  const [state, setState] = useState<CallState>({ status: 'idle' })
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => controller.current?.abort(), [])

  const run = useCallback((call: (signal: AbortSignal) => Promise<McpCallOutcome>) => {
    controller.current?.abort()
    const current = new AbortController()
    controller.current = current
    setState({ status: 'running' })
    call(current.signal)
      .then((outcome) => {
        if (!current.signal.aborted) setState({ status: 'done', outcome })
      })
      .catch((err: unknown) => {
        if (!current.signal.aborted) setState({ status: 'refused', message: refusalMessage(err) })
      })
  }, [])

  return { state, run }
}

function CallResult({ state, callMethod, runningText }: { state: CallState; callMethod: string; runningText: string }) {
  if (state.status === 'running') {
    return (
      <Space size={8} aria-live="polite">
        <Spin size="small" />
        <span style={{ fontSize: 12, color: colors.textMuted }}>{runningText}</span>
      </Space>
    )
  }
  if (state.status === 'refused') return <Alert type="error" showIcon message={state.message} />
  if (state.status === 'done') return <McpResultView outcome={state.outcome} callMethod={callMethod} />
  return null
}

const ANNOTATION_LABELS: [keyof McpTool['annotations'], string][] = [
  ['readOnlyHint', 'Read only'],
  ['destructiveHint', 'Destructive'],
  ['idempotentHint', 'Idempotent'],
  ['openWorldHint', 'Open world'],
]

function ToolInspector({ tool }: { tool: McpTool }) {
  const { isAdmin, isLoading: authLoading } = useAuth()
  const { state, run } = useMcpCall()
  const [draft, setDraft] = useState(() => initialDraft(tool.inputSchema))
  const built = buildArguments(tool.inputSchema, draft)
  const running = state.status === 'running'
  const hints = ANNOTATION_LABELS.filter(([hint]) => tool.annotations[hint] === true)

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {tool.description && <div style={{ fontSize: 13, color: colors.textSecondary }}>{tool.description}</div>}
      {hints.length > 0 && (
        <Space size={4} wrap>
          {hints.map(([hint, label]) => (
            <Tag key={hint} color={hint === 'destructiveHint' ? 'red' : undefined}>
              {label}
            </Tag>
          ))}
        </Space>
      )}
      <McpArgumentsForm schema={tool.inputSchema} draft={draft} onChange={setDraft} disabled={running} />
      {!authLoading && !isAdmin && (
        <Alert type="info" showIcon message="Only administrators can call tools." />
      )}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 12 }}>
        {built.problem && isAdmin && <span style={{ fontSize: 12, color: colors.textMuted }}>{built.problem}</span>}
        <Button
          type="primary"
          disabled={!isAdmin || !built.args}
          loading={running}
          onClick={() => {
            const args = built.args
            if (args) run((signal) => callMcpTool(tool.name, args, signal))
          }}
        >

          {t("Call tool")}
        </Button>
      </div>
      <CallResult state={state} callMethod="tools/call" runningText="Calling the tool" />
    </Space>
  )
}

function ResourceInspector({ resource }: { resource: McpResource }) {
  const { isAdmin, isLoading: authLoading } = useAuth()
  const { state, run } = useMcpCall()

  useEffect(() => {
    if (isAdmin) run((signal) => readMcpResource(resource.uri, signal))
  }, [isAdmin, resource.uri, run])

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {resource.description && <div style={{ fontSize: 13, color: colors.textSecondary }}>{resource.description}</div>}
      <Space size={8} wrap>
        <code style={{ overflowWrap: 'anywhere' }}>{resource.uri}</code>
        {resource.mimeType && <Tag>{resource.mimeType}</Tag>}
      </Space>
      {!authLoading && !isAdmin && (
        <Alert type="info" showIcon message="Only administrators can read resources." />
      )}
      <CallResult state={state} callMethod="resources/read" runningText="Reading the resource" />
    </Space>
  )
}

export default function McpInspectorModal({ item, onClose }: { item: InspectedItem; onClose: () => void }) {
  return (
    <Modal
      open
      title={item.kind === 'tool' ? `Tool: ${item.tool.annotations.title || item.tool.title || item.tool.name}` : `Resource: ${item.resource.name || item.resource.uri}`}
      width={720}
      maskClosable={false}
      onCancel={onClose}
      footer={<Button onClick={onClose}>{t("Close")}</Button>}
    >
      {item.kind === 'tool' ? <ToolInspector tool={item.tool} /> : <ResourceInspector resource={item.resource} />}
    </Modal>
  )
}
