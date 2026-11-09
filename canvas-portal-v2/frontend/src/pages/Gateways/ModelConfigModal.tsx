import { t } from '@/i18n'
import React, { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Collapse, Form, Input, message, Modal, Select, Spin, Switch } from 'antd'
import {
  addModelConfig,
  updateModelConfig,
  type ModelConfig,
  type ModelConfigInput,
  type ModelParams,
  type ModelProvider,
} from '@/api/aiGateways'
import { colors } from '@/theme'
import { errorDetail } from './errorDetail'

/** config is null when adding a new model. */
export interface EditedModel {
  config: ModelConfig | null
}

const CUSTOM = 'custom'
const REFERENCE = 'reference'

// The provider names accepted by agentgateway v1.7.0 (llm.models[].provider).
const PROVIDER_OPTIONS = [
  {
    label: 'Built in',
    options: ['openAI', 'anthropic', 'gemini', 'vertex', 'bedrock', 'azure', 'copilot'].map((value) => ({ value, label: value })),
  },
  {
    label: 'Presets',
    options: [
      'baseten', 'cerebras', 'cohere', 'deepinfra', 'deepseek', 'fireworks', 'groq', 'huggingface',
      'meta', 'mistral', 'ollama', 'openrouter', 'perplexity', 'togetherai', 'typesafe', 'xai',
    ].map((value) => ({ value, label: value })),
  },
  {
    label: 'Other',
    options: [
      { value: CUSTOM, label: 'Custom provider' },
      { value: REFERENCE, label: 'Provider from llm.providers' },
    ],
  },
]

const options = (...values: string[]) => values.map((value) => ({ value, label: value }))

interface FormValues extends Omit<ModelParams, 'tokenize'> {
  name: string
  provider: string
  customProvider?: string
  providerReference?: string
  visibility?: string
  passthrough?: string
  tokenize?: boolean
  apiKey?: string
  removeApiKey?: boolean
  advanced?: string
}

const toJson = (value: unknown) => JSON.stringify(value, null, 2)

function initialValues(config: ModelConfig | null): Partial<FormValues> {
  if (!config) return { provider: 'openAI' }
  const { provider } = config
  const hasAdvanced = Object.keys(config.advanced).length > 0
  return {
    ...config.params,
    name: config.name,
    provider: typeof provider === 'string' ? provider : 'custom' in provider ? CUSTOM : REFERENCE,
    customProvider: typeof provider === 'object' && 'custom' in provider ? toJson(provider.custom) : undefined,
    providerReference: typeof provider === 'object' && 'reference' in provider ? provider.reference : undefined,
    visibility: config.visibility ?? undefined,
    passthrough: config.passthrough ?? undefined,
    advanced: hasAdvanced ? toJson(config.advanced) : undefined,
  }
}

function parseJsonObject(text: string | undefined, label: string): Record<string, unknown> {
  if (!text?.trim()) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error(`${label} must be valid JSON.`)
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object.`)
  }
  return parsed as Record<string, unknown>
}

function providerFrom(values: FormValues): ModelProvider {
  if (values.provider === CUSTOM) return { custom: parseJsonObject(values.customProvider, 'The custom provider') }
  if (values.provider === REFERENCE) return { reference: values.providerReference ?? '' }
  return values.provider
}

function toInput(values: FormValues): ModelConfigInput {
  // Fields for another provider stay in the form while it's open, but shouldn't be saved.
  const provider = values.provider
  const anyProvider = provider === CUSTOM || provider === REFERENCE
  const params: ModelParams = {
    model: values.model,
    baseUrl: values.baseUrl,
    tokenize: values.tokenize || undefined,
  }
  if (provider === 'bedrock' || anyProvider) {
    params.awsRegion = values.awsRegion
    params.bedrockEndpointPreference = values.bedrockEndpointPreference
  }
  if (provider === 'vertex' || anyProvider) {
    params.vertexRegion = values.vertexRegion
    params.vertexProject = values.vertexProject
  }
  if (provider === 'azure' || anyProvider) {
    params.azureResourceName = values.azureResourceName
    params.azureResourceType = values.azureResourceType
    params.azureApiVersion = values.azureApiVersion
    params.azureProjectName = values.azureProjectName
  }
  return {
    name: values.name.trim(),
    provider: providerFrom(values),
    visibility: values.visibility ?? null,
    passthrough: values.passthrough ?? null,
    params,
    apiKey: values.removeApiKey ? null : values.apiKey?.trim() || null,
    removeApiKey: Boolean(values.removeApiKey),
    advanced: parseJsonObject(values.advanced, 'Advanced settings'),
  }
}

const help = (text: React.ReactNode) => <span style={{ fontSize: 12, color: colors.textMuted }}>{text}</span>

export default function ModelConfigModal({
  config,
  onSaved,
  onClose,
}: {
  config: ModelConfig | null
  onSaved: () => void
  onClose: () => void
}) {
  const [form] = Form.useForm<FormValues>()
  const [formError, setFormError] = useState('')
  const provider = Form.useWatch('provider', form)
  const removeApiKey = Form.useWatch('removeApiKey', form)
  // A custom or referenced provider can be any kind, so it can use any of these settings.
  const anyProvider = provider === CUSTOM || provider === REFERENCE

  const save = useMutation({
    mutationFn: (input: ModelConfigInput) =>
      config ? updateModelConfig(input, config.name, config.revision) : addModelConfig(input),
    onSuccess: (result) => {
      message.success(result.message)
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
      title={config ? `Edit ${config.name}` : 'Add a model'}
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
          {config ? 'Save' : 'Add model'}
        </Button>,
      ]}
    >
      <Spin spinning={save.isPending} tip="Applying to the gateway">
        <Form form={form} layout="vertical" size="small" initialValues={initialValues(config)} onFinish={submit}>
          <Form.Item
            name="name"
            label={t("Name")}
            extra={help('The model name clients request. It can include * as a wildcard, for example anthropic/*.')}
            rules={[{ required: true, whitespace: true, message: 'Enter a name' }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="provider" label={t("Provider")} rules={[{ required: true, message: 'Choose a provider' }]}>
            <Select showSearch options={PROVIDER_OPTIONS} />
          </Form.Item>
          {provider === CUSTOM && (
            <Form.Item
              name="customProvider"
              label="Custom provider (JSON)"
              extra={help('For example {"formats": [{"type": "completions"}]}')}
              rules={[{ required: true, message: 'Enter the custom provider' }]}
            >
              <Input.TextArea rows={4} spellCheck={false} style={{ fontFamily: 'monospace' }} />
            </Form.Item>
          )}
          {provider === REFERENCE && (
            <Form.Item
              name="providerReference"
              label={t("Provider name")}
              rules={[{ required: true, whitespace: true, message: 'Enter the provider name' }]}
            >
              <Input autoComplete="off" />
            </Form.Item>
          )}
          <Form.Item
            name="model"
            label={t("Upstream model")}
            extra={help('The model to send to the provider. If empty, the requested name is sent.')}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="baseUrl"
            label={t("Base URL")}
            extra={help('For example https://api.openai.com/v1. Endpoint paths are added after this.')}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="apiKey"
            label={t("API key")}
            extra={help(
              config?.apiKeySet
                ? 'A key is set, and is kept if you leave this empty. Changing where requests go needs the key again.'
                : 'Stored in a Kubernetes Secret, and not shown again. If empty, the gateway looks for a key in its environment.',
            )}
          >
            <Input.Password autoComplete="new-password" disabled={removeApiKey} />
          </Form.Item>
          {config?.apiKeySet && (
            <Form.Item name="removeApiKey" valuePropName="checked">
              <Checkbox>Remove the current key</Checkbox>
            </Form.Item>
          )}
          {(provider === 'bedrock' || anyProvider) && (
            <>
              <Form.Item name="awsRegion" label="AWS region">
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="bedrockEndpointPreference" label="Bedrock endpoint preference">
                <Select
                  allowClear
                  placeholder="runtimePreferred"
                  options={options('runtimePreferred', 'mantlePreferred', 'mantleOnly', 'runtimeOnly')}
                />
              </Form.Item>
            </>
          )}
          {(provider === 'vertex' || anyProvider) && (
            <>
              <Form.Item name="vertexRegion" label="Vertex region">
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="vertexProject" label="Vertex project">
                <Input autoComplete="off" />
              </Form.Item>
            </>
          )}
          {(provider === 'azure' || anyProvider) && (
            <>
              <Form.Item name="azureResourceName" label="Azure resource name">
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="azureResourceType" label="Azure resource type">
                <Select allowClear options={options('openAI', 'foundry', 'aiServices')} />
              </Form.Item>
              <Form.Item name="azureApiVersion" label="Azure API version">
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="azureProjectName" label="Azure project name">
                <Input autoComplete="off" />
              </Form.Item>
            </>
          )}
          <Collapse
            size="small"
            ghost
            defaultActiveKey={config && Object.keys(config.advanced).length > 0 ? ['advanced'] : []}
            items={[
              {
                key: 'advanced',
                label: 'Advanced',
                forceRender: true,
                children: (
                  <>
                    <Form.Item name="visibility" label="Visibility">
                      <Select allowClear placeholder="public" options={options('public', 'internal')} />
                    </Form.Item>
                    <Form.Item name="passthrough" label="Passthrough">
                      <Select allowClear placeholder="Off" options={options('detect', 'opaque')} />
                    </Form.Item>
                    <Form.Item name="tokenize" label="Tokenize requests" valuePropName="checked">
                      <Switch size="small" />
                    </Form.Item>
                    <Form.Item
                      name="advanced"
                      label="Other settings (JSON)"
                      extra={help(
                        <>
                          Any other <code>llm.models[]</code> field from the agentgateway config reference, such as{' '}
                          <code>defaults</code>, <code>overrides</code>, <code>requestHeaders</code>, <code>tls</code>,{' '}
                          <code>guardrails</code> or <code>matches</code>. These are stored in a ConfigMap, so don't put
                          secrets here, and <code>$</code> can't be used.
                        </>,
                      )}
                    >
                      <Input.TextArea rows={5} spellCheck={false} style={{ fontFamily: 'monospace' }} />
                    </Form.Item>
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
