import { t } from '@/i18n'
import React, { useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal, Space, Spin, Tag } from 'antd'
import { streamModelTest } from '@/api/aiGateways'
import { colors } from '@/theme'

interface TestOutput {
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  finishReason: string | null
  error: string
}

const INITIAL_OUTPUT: TestOutput = { reasoning: '', content: '', status: 'streaming', finishReason: null, error: '' }

const outputStyle: React.CSSProperties = {
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
  fontFamily: 'monospace',
  fontSize: 13,
  margin: 0,
}
const labelStyle: React.CSSProperties = { fontSize: 12, fontWeight: 600, color: colors.textSecondary, marginBottom: 4 }

function useModelTest(model: string): TestOutput {
  const [output, setOutput] = useState(INITIAL_OUTPUT)

  useEffect(() => {
    const controller = new AbortController()
    const fail = (error: string) => setOutput((current) => ({ ...current, status: 'error', error }))
    streamModelTest(
      model,
      (event) => {
        if (event.type === 'error') {
          fail(event.message)
        } else if (event.type === 'done') {
          setOutput((current) => ({ ...current, status: 'done', finishReason: event.finishReason }))
        } else {
          setOutput((current) => ({ ...current, [event.type]: current[event.type] + event.text }))
        }
      },
      controller.signal,
    )
      .then(() => {
        // The backend always ends with done or error, unless the connection was cut.
        setOutput((current) =>
          current.status === 'streaming' ? { ...current, status: 'error', error: 'The connection closed early.' } : current,
        )
      })
      .catch((err: Error) => {
        if (err.name !== 'AbortError') fail(err.message)
      })
    // Closing the dialog stops the request, and with it the generation.
    return () => controller.abort()
  }, [model])

  return output
}

function Status({ output }: { output: TestOutput }) {
  if (output.status === 'streaming') {
    return (
      <Space size={8}>
        <Spin size="small" />
        <span style={{ fontSize: 12, color: colors.textMuted }}>
          {output.reasoning || output.content ? 'Streaming the reply' : 'Waiting for the model'}
        </span>
      </Space>
    )
  }
  if (output.status === 'done') return <Tag color="green">Connection works</Tag>
  return <Tag color="red">Test failed</Tag>
}

export default function ModelTestModal({ model, onClose }: { model: string; onClose: () => void }) {
  const output = useModelTest(model)
  const scrollArea = useRef<HTMLDivElement>(null)
  const reasoning = output.reasoning.trimStart()
  const content = output.content.trimStart()

  useEffect(() => {
    if (scrollArea.current) scrollArea.current.scrollTop = scrollArea.current.scrollHeight
  }, [reasoning, content])

  return (
    <Modal
      open
      title={`Test ${model}`}
      width={640}
      maskClosable={false}
      onCancel={onClose}
      footer={<Button onClick={onClose}>{t("Close")}</Button>}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 12, color: colors.textSecondary }}>
            Sends <code>PING</code> to the chat completions endpoint, with streaming on.
          </span>
          <div aria-live="polite">
            <Status output={output} />
          </div>
        </div>
        <div
          ref={scrollArea}
          style={{
            minHeight: 120,
            maxHeight: 360,
            overflowY: 'auto',
            border: `1px solid ${colors.border}`,
            borderRadius: 8,
            padding: 12,
            background: colors.surfaceSubtle,
          }}
        >
          {reasoning && (
            <div style={{ marginBottom: content ? 12 : 0 }}>
              <div style={labelStyle}>Reasoning</div>
              <pre style={{ ...outputStyle, color: colors.textMuted }}>{reasoning}</pre>
            </div>
          )}
          {content && (
            <div>
              <div style={labelStyle}>Reply</div>
              <pre style={{ ...outputStyle, color: colors.textPrimary }}>{content}</pre>
            </div>
          )}
        </div>
        {output.status === 'done' && output.finishReason === 'length' && (
          <Alert type="info" showIcon message="The reply stopped at the test's token limit." />
        )}
        {output.status === 'done' && !content && output.finishReason !== 'length' && (
          <Alert type="warning" showIcon message="The model finished without sending any reply text." />
        )}
        {output.status === 'error' && <Alert type="error" showIcon message={output.error} />}
      </Space>
    </Modal>
  )
}
