import React, { useState } from 'react'
import { Alert, Button, Checkbox, Input, Modal, Select, message } from 'antd'
import { ApiOutlined, DatabaseOutlined, SaveOutlined } from '@ant-design/icons'
import {
  addRegistrySource,
  registryErrorMessage,
  testRegistryConnection,
  testSavedRegistry,
  updateRegistrySource,
  type AuthType,
  type ConnectionTestResult,
  type GrantType,
  type RegistrySource,
  type RegistrySourceInput,
} from '@/api/registries'
import { colors } from '@/theme'
import { TestResultAlert, subtleButton } from './registryUi'

const AUTH_OPTIONS: { value: AuthType; label: string }[] = [
  { value: 'token', label: 'Token' },
  { value: 'basic', label: 'Username and password' },
  { value: 'oauth', label: 'OAuth token URL' },
  { value: 'none', label: 'No credentials' },
]

const EMPTY_FORM = {
  name: '',
  url: '',
  pageUrl: '',
  authType: 'token' as AuthType,
  grantType: 'client_credentials' as GrantType,
  token: '',
  username: '',
  password: '',
  tokenUrl: '',
  clientId: '',
  clientSecret: '',
  scope: '',
  skipTlsVerify: false,
  allowInsecureHttp: false,
}

type FormState = typeof EMPTY_FORM

const isPlainHttp = (url: string) => /^\s*http:\/\//i.test(url)

// Mirrors how the backend stores a registry URL: it accepts the host or the full API base.
const normalizedRegistryUrl = (url: string) =>
  url.trim().replace(/\/+$/, '').replace(/\/rest\/v1\/registry-center$/, '')

function initialForm(source: RegistrySource | null): FormState {
  if (!source) return EMPTY_FORM
  return {
    ...EMPTY_FORM,
    name: source.name,
    url: source.url,
    pageUrl: source.pageUrl ?? '',
    authType: source.authType,
    grantType: source.grantType ?? EMPTY_FORM.grantType,
    tokenUrl: source.tokenUrl ?? '',
    skipTlsVerify: !!source.skipTlsVerify,
  }
}

interface RegistrySourceModalProps {
  open: boolean
  /** The registry being edited, or null to add a new one. */
  editing: RegistrySource | null
  /** Set when the registry store cannot be written, which blocks saving. */
  storeError?: string | null
  onClose: () => void
  /** Called after a successful save, before the modal closes. */
  onSaved: (source: RegistrySource) => void | Promise<void>
}

/** Modal to add a registry or edit a saved one, with an optional connection test before saving. */
export default function RegistrySourceModal({ open, editing, storeError, onClose, onSaved }: RegistrySourceModalProps) {
  const [saving, setSaving] = useState(false)

  return (
    <Modal
      open={open}
      onCancel={() => !saving && onClose()}
      footer={null}
      width={760}
      destroyOnClose
      closable={!saving}
      maskClosable={!saving}
      keyboard={!saving}
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: colors.textPrimary }}>
          <DatabaseOutlined style={{ color: colors.primary }} />
          {editing ? `Edit ${editing.name}` : 'Add Registry'}
        </div>
      }
      styles={{
        content: { background: colors.bgCard, padding: 0 },
        header: {
          background: colors.bgCard,
          borderBottom: `1px solid ${colors.border}`,
          padding: '14px 20px',
          margin: 0,
        },
        body: { padding: '20px 24px 24px' },
      }}
    >
      {/* Rendered as the modal body so destroyOnClose starts every opening from a fresh form. */}
      <RegistrySourceForm
        editing={editing}
        storeError={storeError}
        saving={saving}
        setSaving={setSaving}
        onCancel={onClose}
        onSaved={onSaved}
      />
    </Modal>
  )
}

interface RegistrySourceFormProps {
  editing: RegistrySource | null
  storeError?: string | null
  saving: boolean
  setSaving: (saving: boolean) => void
  onCancel: () => void
  onSaved: (source: RegistrySource) => void | Promise<void>
}

function RegistrySourceForm({ editing, storeError, saving, setSaving, onCancel, onSaved }: RegistrySourceFormProps) {
  const [form, setForm] = useState(() => initialForm(editing))
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [testing, setTesting] = useState(false)

  function update(fields: Partial<FormState>) {
    setForm((current) => ({ ...current, ...fields }))
    // A result describes the values it tested, so it goes stale as soon as they change.
    setTestResult(null)
  }

  const isOauth = form.authType === 'oauth'
  const usesPassword = form.authType === 'basic' || (isOauth && form.grantType === 'password')
  const usesClientSecret = isOauth && form.grantType === 'client_credentials'
  const needsCredentials = form.authType !== 'none'
  const insecure = needsCredentials && (isPlainHttp(form.url) || (isOauth && isPlainHttp(form.tokenUrl)))

  // The backend applies the same rule: saved credentials never follow a registry to a new address.
  const keepsCredentials =
    !!editing &&
    needsCredentials &&
    editing.credentialsSet &&
    form.authType === editing.authType &&
    normalizedRegistryUrl(form.url) === editing.url &&
    (!form.skipTlsVerify || !!editing.skipTlsVerify) &&
    (!isOauth || (form.grantType === editing.grantType && form.tokenUrl.trim() === editing.tokenUrl))

  const typedCredentials = [
    form.authType === 'token' && form.token,
    usesPassword && form.username,
    usesPassword && form.password,
    isOauth && form.clientId,
    isOauth && form.clientSecret,
    isOauth && form.scope,
  ].some((value) => value && value.trim())

  const credentialsComplete =
    !needsCredentials ||
    (keepsCredentials && !typedCredentials) ||
    (form.authType === 'token'
      ? !!form.token.trim()
      : (!usesPassword || (!!form.username.trim() && !!form.password.trim())) &&
        (!usesClientSecret || (!!form.clientId.trim() && !!form.clientSecret.trim())) &&
        (!isOauth || !!form.tokenUrl.trim()))
  const complete = !!form.name.trim() && !!form.url.trim() && credentialsComplete && (!insecure || form.allowInsecureHttp)

  const credentialPlaceholder = (label: string) => (keepsCredentials ? `${label} (leave blank to keep)` : label)

  function buildInput(): RegistrySourceInput {
    const base = {
      name: form.name,
      url: form.url,
      pageUrl: form.pageUrl.trim() || undefined,
      authType: form.authType,
      skipTlsVerify: form.skipTlsVerify,
      allowInsecureHttp: form.allowInsecureHttp,
    }
    if (form.authType === 'none') return base
    if (form.authType === 'token') return { ...base, token: form.token }
    if (form.authType === 'basic') return { ...base, username: form.username, password: form.password }
    return {
      ...base,
      tokenUrl: form.tokenUrl,
      grantType: form.grantType,
      clientId: form.clientId || undefined,
      clientSecret: form.clientSecret || undefined,
      scope: form.scope || undefined,
      ...(usesPassword ? { username: form.username, password: form.password } : {}),
    }
  }

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await (editing ? testSavedRegistry(editing.id, buildInput()) : testRegistryConnection(buildInput())))
    } catch (err) {
      message.error(registryErrorMessage(err))
    } finally {
      setTesting(false)
    }
  }

  async function handleSave() {
    setSaving(true)
    try {
      const saved = await (editing ? updateRegistrySource(editing.id, buildInput()) : addRegistrySource(buildInput()))
      message.success(`${editing ? 'Saved' : 'Added'} ${saved.name}`)
      await onSaved(saved)
    } catch (err) {
      message.error(registryErrorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      {storeError && <Alert type="warning" showIcon message={storeError} style={{ marginBottom: 12 }} />}

      {editing && needsCredentials && (
        <Alert
          type="info"
          showIcon
          message={
            keepsCredentials
              ? 'Leave the credential fields blank to keep the saved ones.'
              : 'Enter the credentials again. Saved ones are kept only while the authentication type and URLs stay the same and TLS verification stays on.'
          }
          style={{ marginBottom: 12 }}
        />
      )}

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
        <Input
          aria-label="Registry name"
          value={form.name}
          onChange={(e) => update({ name: e.target.value })}
          placeholder="Registry name"
          maxLength={64}
          style={{ flex: '1 1 200px', minWidth: 180 }}
        />
        <Input
          aria-label="Registry URL"
          value={form.url}
          onChange={(e) => update({ url: e.target.value })}
          placeholder="https://registry.example.com"
          maxLength={2048}
          style={{ flex: '2 1 320px', minWidth: 260 }}
        />
        <Input
          aria-label="Registry page"
          value={form.pageUrl}
          onChange={(e) => update({ pageUrl: e.target.value })}
          placeholder="Registry page (optional)"
          maxLength={2048}
          style={{ flex: '2 1 320px', minWidth: 220 }}
        />
        <Select
          aria-label="Authentication"
          value={form.authType}
          onChange={(authType) => update({ authType })}
          options={AUTH_OPTIONS}
          style={{ flex: '1 1 200px', minWidth: 200 }}
        />
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: needsCredentials ? 10 : 0 }}>
        {form.authType === 'token' && (
          <Input.Password
            aria-label="Token"
            value={form.token}
            onChange={(e) => update({ token: e.target.value })}
            placeholder={credentialPlaceholder('Bearer token')}
            autoComplete="new-password"
            style={{ flex: '1 1 320px', minWidth: 260 }}
          />
        )}
        {isOauth && (
          <>
            <Input
              aria-label="Token URL"
              value={form.tokenUrl}
              onChange={(e) => update({ tokenUrl: e.target.value })}
              placeholder="https://iam.example.com/oauth2/token"
              maxLength={2048}
              style={{ flex: '2 1 320px', minWidth: 260 }}
            />
            <Select
              aria-label="Grant type"
              value={form.grantType}
              onChange={(grantType) => update({ grantType })}
              options={[
                { value: 'client_credentials', label: 'Client credentials' },
                { value: 'password', label: 'Password grant' },
              ]}
              style={{ flex: '1 1 180px', minWidth: 180 }}
            />
          </>
        )}
        {usesPassword && (
          <>
            <Input
              aria-label="Username"
              value={form.username}
              onChange={(e) => update({ username: e.target.value })}
              placeholder={credentialPlaceholder('Username')}
              autoComplete="off"
              style={{ flex: '1 1 180px', minWidth: 180 }}
            />
            <Input.Password
              aria-label="Password"
              value={form.password}
              onChange={(e) => update({ password: e.target.value })}
              placeholder={credentialPlaceholder('Password')}
              autoComplete="new-password"
              style={{ flex: '1 1 180px', minWidth: 180 }}
            />
          </>
        )}
        {isOauth && (
          <>
            <Input
              aria-label="Client ID"
              value={form.clientId}
              onChange={(e) => update({ clientId: e.target.value })}
              placeholder={credentialPlaceholder(usesClientSecret ? 'Client ID' : 'Client ID (optional)')}
              autoComplete="off"
              style={{ flex: '1 1 180px', minWidth: 180 }}
            />
            <Input.Password
              aria-label="Client secret"
              value={form.clientSecret}
              onChange={(e) => update({ clientSecret: e.target.value })}
              placeholder={credentialPlaceholder(usesClientSecret ? 'Client secret' : 'Client secret (optional)')}
              autoComplete="new-password"
              style={{ flex: '1 1 180px', minWidth: 180 }}
            />
            <Input
              aria-label="Scope"
              value={form.scope}
              onChange={(e) => update({ scope: e.target.value })}
              placeholder={credentialPlaceholder('Scope (optional)')}
              style={{ flex: '1 1 180px', minWidth: 180 }}
            />
          </>
        )}
      </div>

      {insecure && (
        <Alert
          type="warning"
          showIcon
          message="This URL uses http://, so the credentials will be sent unencrypted."
          description={
            <Checkbox
              checked={form.allowInsecureHttp}
              onChange={(e) => update({ allowInsecureHttp: e.target.checked })}
            >
              I understand and want to continue
            </Checkbox>
          }
          style={{ marginBottom: 10 }}
        />
      )}

      <Checkbox
        checked={form.skipTlsVerify}
        onChange={(e) => update({ skipTlsVerify: e.target.checked })}
        style={{ marginBottom: 16 }}
      >
        Skip TLS verification
      </Checkbox>

      {testResult && <TestResultAlert result={testResult} />}

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          flexWrap: 'wrap',
          paddingTop: 16,
          borderTop: `1px solid ${colors.border}`,
        }}
      >
        <Button icon={<ApiOutlined />} onClick={handleTest} disabled={!complete || saving} loading={testing} style={subtleButton}>
          Test Connection
        </Button>
        <div style={{ flex: 1 }} />
        <Button onClick={onCancel} disabled={saving} style={subtleButton}>
          Cancel
        </Button>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          onClick={handleSave}
          disabled={!complete || !!storeError || testing}
          loading={saving}
        >
          Save
        </Button>
      </div>
    </div>
  )
}
