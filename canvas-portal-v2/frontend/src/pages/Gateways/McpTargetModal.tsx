import { t } from '@/i18n'
import React, { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Collapse, Form, Input, InputNumber, message, Modal, Select, Spin } from 'antd'
import { addMcpTarget, updateMcpTarget, type McpTarget, type McpTargetInput } from '@/api/aiGateways'
import { colors } from '@/theme'
import { errorDetail } from './errorDetail'

const TRANSPORT_OPTIONS = [
  { value: 'mcp', label: 'Streamable HTTP (mcp)' },
  { value: 'sse', label: 'Server-Sent Events (sse)' },
  { value: 'openapi', label: 'OpenAPI schema as MCP tools (openapi)' },
]

interface FormValues {
  name: string
  transport: McpTarget['transport']
  host?: string
  port?: number | null
  path?: string
  schema?: string
  condition?: string
  authToken?: string
  removeAuthToken?: boolean
  policies?: string
}

const help = (text: React.ReactNode) => <span style={{ fontSize: 12, color: colors.textMuted }}>{text}</span>

function initialValues(target: McpTarget | null): Partial<FormValues> {
  if (!target) return { transport: 'mcp' }
  return {
    name: target.name,
    transport: target.transport,
    host: target.host ?? undefined,
    port: target.port ?? undefined,
    path: target.path ?? undefined,
    schema: target.schema ?? undefined,
    condition: target.condition ?? undefined,
    policies: Object.keys(target.policies).length > 0 ? JSON.stringify(target.policies, null, 2) : undefined,
  }
}

function parsePolicies(text: string | undefined): Record<string, unknown> {
  if (!text?.trim()) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('Policies must be valid JSON.')
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('Policies must be a JSON object.')
  }
  return parsed as Record<string, unknown>
}

function toInput(values: FormValues): McpTargetInput {
  return {
    name: values.name.trim(),
    transport: values.transport,
    host: values.host?.trim() || null,
    port: values.port ?? null,
    path: values.path?.trim() || null,
    schema: values.schema?.trim() ? values.schema : null,
    condition: values.condition?.trim() || null,
    authToken: values.removeAuthToken ? null : values.authToken?.trim() || null,
    removeAuthToken: Boolean(values.removeAuthToken),
    policies: parsePolicies(values.policies),
  }
}

export default function McpTargetModal({
  target,
  onSaved,
  onClose,
}: {
  target: McpTarget | null
  onSaved: () => void
  onClose: () => void
}) {
  const [form] = Form.useForm<FormValues>()
  const [formError, setFormError] = useState('')
  const transport = Form.useWatch('transport', form)
  const removeAuthToken = Form.useWatch('removeAuthToken', form)
  const isStdio = target?.transport === 'stdio'

  const save = useMutation({
    mutationFn: (input: McpTargetInput) =>
      target ? updateMcpTarget(input, target.name, target.revision) : addMcpTarget(input),
    onSuccess: (result) => {
      if (result.warning) message.warning(result.warning)
      else message.success(result.message)
      onSaved()
    },
    onError: (error) => setFormError(errorDetail(error)),
  })

  const submit = (values: FormValues) => {
    setFormError('')
    try {
      save.mutate(toInput(values))
    } catch (error) {
      setFormError((error as Error).message)
    }
  }

  return (
    <Modal
      open
      title={target ? `Edit ${target.name}` : 'Add an MCP server'}
      width={640}
      maskClosable={false}
      closable={!save.isPending}
      keyboard={!save.isPending}
      onCancel={onClose}
      footer={[
        <Button key="cancel" disabled={save.isPending} onClick={onClose}>

          {t("Cancel")}
        </Button>,
        <Button key="save" type="primary" loading={save.isPending} onClick={() => form.submit()}>
          {target ? 'Save' : 'Add MCP server'}
        </Button>,
      ]}
    >
      <Spin spinning={save.isPending} tip="Applying to the gateway">
        <Form form={form} layout="vertical" size="small" initialValues={initialValues(target)} onFinish={submit}>
          <Form.Item
            name="name"
            label={t("Name")}
            extra={help('With more than one server, the gateway adds this name to every tool name, such as myserver_echo. Adding, removing or renaming a server can change the names clients use.')}
            rules={[{ required: true, whitespace: true, message: 'Enter a name' }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          {isStdio ? (
            <>
              <Form.Item name="transport" hidden>
                <Input />
              </Form.Item>
              <Alert
                style={{ marginBottom: 12 }}
                type="info"
                showIcon
                message={`This server runs the command ${target.command ?? ''} inside the gateway pod.`}
                description="The portal can't add stdio servers or change their command, arguments or environment."
              />
            </>
          ) : (
            <>
              <Form.Item name="transport" label={t("Transport")} rules={[{ required: true, message: 'Choose a transport' }]}>
                <Select options={TRANSPORT_OPTIONS} />
              </Form.Item>
              <Form.Item
                name="host"
                label={t("Host")}
                extra={help(
                  target?.backend
                    ? `Connects through the backend ${target.backend}. Enter a host to use that instead.`
                    : 'A URL such as https://example.com/mcp, or a host and port such as example.com:443.',
                )}
                rules={[{ required: !target?.backend, whitespace: true, message: 'Enter a host' }]}
              >
                <Input autoComplete="off" />
              </Form.Item>
              <div style={{ display: 'flex', gap: 12 }}>
                <Form.Item name="port" label={t("Port")} style={{ flex: 1 }} extra={help('Only with a plain host name, and with a path.')}>
                  <InputNumber min={0} max={65535} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="path" label={t("Path")} style={{ flex: 2 }} extra={help('For example /mcp')}>
                  <Input autoComplete="off" />
                </Form.Item>
              </div>
              {transport === 'openapi' && (
                <Form.Item
                  name="schema"
                  label="OpenAPI schema"
                  extra={help(
                    target?.schemaKept
                      ? 'This server loads its schema from a file or URL, which stays if you leave this empty.'
                      : 'The schema itself, as JSON or YAML. The portal can\'t add schemas from a file or URL.',
                  )}
                  rules={[{ required: !target?.schemaKept, whitespace: true, message: 'Enter the schema' }]}
                >
                  <Input.TextArea rows={6} spellCheck={false} style={{ fontFamily: 'monospace' }} />
                </Form.Item>
              )}
              <Form.Item
                name="authToken"
                label="Auth token"
                extra={help(
                  target?.tokenSet
                    ? 'A token is set, and is kept if you leave this empty. Changing where requests go needs the token again.'
                    : 'Sent to the server as a bearer token. Stored in a Kubernetes Secret, and not shown again.',
                )}
              >
                <Input.Password autoComplete="new-password" disabled={removeAuthToken} />
              </Form.Item>
              {target?.tokenSet && (
                <Form.Item name="removeAuthToken" valuePropName="checked">
                  <Checkbox>Remove the current token</Checkbox>
                </Form.Item>
              )}
            </>
          )}
          <Collapse
            size="small"
            ghost
            defaultActiveKey={target && (target.condition || Object.keys(target.policies).length > 0) ? ['advanced'] : []}
            items={[
              {
                key: 'advanced',
                label: 'Advanced',
                forceRender: true,
                children: (
                  <>
                    <Form.Item
                      name="condition"
                      label="Condition"
                      extra={help('A CEL expression. The server is used only for requests where it is true.')}
                    >
                      <Input autoComplete="off" />
                    </Form.Item>
                    {!isStdio && (
                      <Form.Item
                        name="policies"
                        label="Policies (JSON)"
                        extra={help(
                          <>
                            Transport policies from the agentgateway config reference (<code>mcp.targets[].policies</code>),
                            such as <code>backendTLS</code>, <code>requestHeaderModifier</code> or <code>http</code>. These
                            are stored in a ConfigMap, so don't put secrets here. <code>$</code> and file paths can't be
                            added.
                          </>,
                        )}
                      >
                        <Input.TextArea rows={5} spellCheck={false} style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                    )}
                  </>
                ),
              },
            ]}
          />
          {formError && <Alert style={{ marginTop: 12 }} type="error" showIcon message={formError} />}
        </Form>
      </Spin>
    </Modal>
  )
}
