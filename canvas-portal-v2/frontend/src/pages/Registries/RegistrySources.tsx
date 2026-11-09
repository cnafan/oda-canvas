import { t } from '@/i18n'
import React, { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Input, Popconfirm, Select, Spin, Tag, Tooltip, message } from 'antd'
import { ApiOutlined, DeleteOutlined, EditOutlined, ExportOutlined, PlusOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import {
  addRegistrySource,
  refreshAgents,
  registryErrorMessage,
  removeRegistrySource,
  testRegistryConnection,
  testSavedRegistry,
  updateRegistrySource,
  useAgents,
  useRegistrySources,
  type AuthType,
  type ConnectionTestResult,
  type GrantType,
  type RegistrySource,
  type RegistrySourceInput,
  type RegistryStatus,
} from '@/api/registries'
import { colors } from '@/theme'

const AUTH_OPTIONS: { value: AuthType; label: string }[] = [
  { value: 'token', label: 'Token' },
  { value: 'basic', label: 'Username and password' },
  { value: 'oauth', label: 'OAuth token URL' },
  { value: 'none', label: 'No credentials' },
]

const AUTH_LABELS: Record<AuthType, string> = { none: 'No credentials', token: 'Token', basic: 'Basic', oauth: 'OAuth' }

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

const STATUS_LABELS: Record<RegistryStatus['status'], string> = {
  ok: 'ready',
  error: 'error',
  credentials_missing: 'credentials missing',
}

const isPlainHttp = (url: string) => /^\s*http:\/\//i.test(url)

// Mirrors how the backend stores a registry URL: it accepts the host or the full API base.
const normalizedRegistryUrl = (url: string) =>
  url.trim().replace(/\/+$/, '').replace(/\/rest\/v1\/registry-center$/, '')

const subtleButton = {
  background: 'transparent',
  border: `1px solid ${colors.border}`,
  color: colors.textSecondary,
}

function pillStyle(background: string, color: string) {
  return { background, color, border: 'none', borderRadius: 999 }
}

function TestResultAlert({ result, onClose }: { result: ConnectionTestResult; onClose?: () => void }) {
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

/** Add and remove external registries, laid out like the Helm repositories card on the deploy page. */
export default function RegistrySources() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(EMPTY_FORM)
  const [editing, setEditing] = useState<RegistrySource | null>(null)
  const formRef = useRef<HTMLDivElement>(null)
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [testing, setTesting] = useState(false)
  const [adding, setAdding] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowResults, setRowResults] = useState<Record<string, ConnectionTestResult>>({})

  const { data, isLoading, isError, error, refetch } = useRegistrySources()
  const { data: agents } = useAgents()

  const statusOf = (id: string) => agents?.registries?.find((entry) => entry.id === id)

  function update(fields: Partial<typeof EMPTY_FORM>) {
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

  async function refreshAll() {
    await Promise.all([refetch(), refreshAgents(queryClient)])
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

  async function handleAdd() {
    setAdding(true)
    try {
      const created = await addRegistrySource(buildInput())
      message.success(`Added ${created.name}`)
      setForm(EMPTY_FORM)
      setTestResult(null)
      await refreshAll()
    } catch (err) {
      message.error(registryErrorMessage(err))
    } finally {
      setAdding(false)
    }
  }

  function startEdit(source: RegistrySource) {
    setEditing(source)
    setForm({
      ...EMPTY_FORM,
      name: source.name,
      url: source.url,
      pageUrl: source.pageUrl ?? '',
      authType: source.authType,
      grantType: source.grantType ?? EMPTY_FORM.grantType,
      tokenUrl: source.tokenUrl ?? '',
      skipTlsVerify: !!source.skipTlsVerify,
    })
    setTestResult(null)
    formRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function stopEdit() {
    setEditing(null)
    setForm(EMPTY_FORM)
    setTestResult(null)
  }

  async function handleSave() {
    if (!editing) return
    setAdding(true)
    try {
      const updated = await updateRegistrySource(editing.id, buildInput())
      message.success(`Saved ${updated.name}`)
      setRowResults(({ [editing.id]: _stale, ...rest }) => rest)
      stopEdit()
      await refreshAll()
    } catch (err) {
      message.error(registryErrorMessage(err))
    } finally {
      setAdding(false)
    }
  }

  async function handleRemove(source: RegistrySource) {
    setBusyId(source.id)
    try {
      await removeRegistrySource(source.id)
      message.success(`Removed ${source.name}`)
      if (editing?.id === source.id) stopEdit()
      await refreshAll()
    } catch (err) {
      message.error(registryErrorMessage(err))
    } finally {
      setBusyId(null)
    }
  }

  async function handleRowTest(source: RegistrySource) {
    setBusyId(source.id)
    try {
      const result = await testSavedRegistry(source.id)
      setRowResults((current) => ({ ...current, [source.id]: result }))
      if (result.ok) await refreshAll()
    } catch (err) {
      message.error(registryErrorMessage(err))
    } finally {
      setBusyId(null)
    }
  }

  async function handleRefresh() {
    setRefreshing(true)
    try {
      await refreshAll()
    } finally {
      setRefreshing(false)
    }
  }

  const storeError = data?.storeError
  const sources = data?.sources ?? []

  return (
    <div>
      {storeError && (
        <Alert type="warning" showIcon message={storeError} style={{ marginBottom: 12 }} />
      )}

      <div ref={formRef} />
      {editing && (
        <Alert
          type="info"
          showIcon
          message={`Editing ${editing.name}`}
          description={
            !needsCredentials
              ? undefined
              : keepsCredentials
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
          placeholder={t("Registry name")}
          maxLength={64}
          style={{ flex: '1 1 180px', minWidth: 180 }}
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
          placeholder={t("Registry page (optional)")}
          maxLength={2048}
          style={{ flex: '2 1 260px', minWidth: 220 }}
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

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
        <Checkbox checked={form.skipTlsVerify} onChange={(e) => update({ skipTlsVerify: e.target.checked })}>

          {t("Skip TLS verification")}
        </Checkbox>
        <div style={{ flex: 1 }} />
        <Button icon={<ApiOutlined />} onClick={handleTest} disabled={!complete} loading={testing} style={subtleButton}>

          {t("Test Connection")}
        </Button>
        <Button
          type="primary"
          icon={editing ? <SaveOutlined /> : <PlusOutlined />}
          onClick={editing ? handleSave : handleAdd}
          disabled={!complete || !!storeError}
          loading={adding}
        >
          {editing ? 'Save Changes' : 'Add Registry'}
        </Button>
        {editing && (
          <Button onClick={stopEdit} disabled={adding} style={subtleButton}>

            {t("Cancel")}
          </Button>
        )}
        <Button icon={<ReloadOutlined />} onClick={handleRefresh} loading={refreshing} style={subtleButton}>

          {t("Refresh")}
        </Button>
      </div>

      {testResult && <TestResultAlert result={testResult} />}

      {isError && (
        <Alert
          type="error"
          showIcon
          message="Could not load registries"
          description={registryErrorMessage(error)}
          style={{ marginBottom: 16 }}
        />
      )}

      {isLoading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '20px 0' }}>
          <Spin />
        </div>
      ) : sources.length === 0 ? (
        <div style={{ color: colors.textMuted, fontSize: 13 }}>{t("No registries configured yet.")}</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {sources.map((source) => {
            const status = statusOf(source.id)
            const healthy = status?.status === 'ok'
            const statusLabel = status ? STATUS_LABELS[status.status] : 'checking'
            return (
              <div
                key={source.id}
                style={{
                  border: `1px solid ${colors.border}`,
                  borderRadius: 10,
                  padding: '14px 16px',
                  background: colors.surfaceSubtle,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ color: colors.textPrimary, fontWeight: 600 }}>{source.name}</span>
                      <Tag
                        style={
                          healthy
                            ? pillStyle('rgba(16,185,129,0.12)', colors.statusLive)
                            : status
                              ? pillStyle('rgba(239,68,68,0.12)', colors.statusDown)
                              : pillStyle(colors.hoverSurface, colors.textSecondary)
                        }
                      >
                        {statusLabel}
                      </Tag>
                      {status && (
                        <Tag style={pillStyle(colors.hoverSurface, colors.textSecondary)}>
                          {status.agentCount} agent{status.agentCount === 1 ? '' : 's'}
                        </Tag>
                      )}
                      <Tag style={pillStyle(colors.hoverSurface, colors.textSecondary)}>
                        {source.builtin ? 'Set by chart' : AUTH_LABELS[source.authType]}
                      </Tag>
                      {healthy && !status.fullAccess && (
                        <Tag style={pillStyle(colors.hoverSurface, colors.textSecondary)}>{t("Published agents only")}</Tag>
                      )}
                      {source.skipTlsVerify && (
                        <Tag style={pillStyle('rgba(245,158,11,0.15)', '#92400E')}>{t("TLS not verified")}</Tag>
                      )}
                    </div>
                    <div style={{ color: colors.textMuted, fontSize: 12, marginTop: 6, overflowWrap: 'anywhere' }}>
                      <code>{source.url}</code>
                    </div>
                    {status?.message && (
                      <div style={{ color: colors.statusDown, fontSize: 12, marginTop: 6 }}>{status.message}</div>
                    )}
                  </div>
                  {(source.pageUrl || !source.builtin) && (
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                      {source.pageUrl && (
                        <Tooltip title={t("Open URL")}>
                          <Button
                            aria-label={`Open the ${source.name} registry page`}
                            icon={<ExportOutlined />}
                            href={source.pageUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={subtleButton}
                          />
                        </Tooltip>
                      )}
                      {!source.builtin && (
                        <>
                          <Button
                            icon={<ApiOutlined />}
                            loading={busyId === source.id}
                            onClick={() => handleRowTest(source)}
                            style={subtleButton}
                          >
                            Test
                          </Button>
                          <Button
                            icon={<EditOutlined />}
                            disabled={busyId === source.id}
                            onClick={() => startEdit(source)}
                            style={subtleButton}
                          >

                            {t("Edit")}
                          </Button>
                          <Popconfirm
                            title={`Remove ${source.name}?`}
                            description="Its saved credentials are deleted too."
                            okText={t("Remove")}
                            okButtonProps={{ danger: true }}
                            onConfirm={() => handleRemove(source)}
                          >
                            <Button icon={<DeleteOutlined />} danger disabled={busyId === source.id}>

                              {t("Remove")}
                            </Button>
                          </Popconfirm>
                        </>
                      )}
                    </div>
                  )}
                </div>
                {rowResults[source.id] && (
                  <div style={{ marginTop: 12 }}>
                    <TestResultAlert
                      result={rowResults[source.id]}
                      onClose={() =>
                        setRowResults(({ [source.id]: _closed, ...rest }) => rest)
                      }
                    />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
