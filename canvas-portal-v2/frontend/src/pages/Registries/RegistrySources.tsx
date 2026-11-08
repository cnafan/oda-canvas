import React, { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Popconfirm, Spin, Tag, Tooltip, message } from 'antd'
import {
  ApiOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  refreshAgents,
  registryErrorMessage,
  removeRegistrySource,
  testSavedRegistry,
  useAgents,
  useRegistrySources,
  type AuthType,
  type ConnectionTestResult,
  type RegistrySource,
  type RegistryStatus,
} from '@/api/registries'
import { colors } from '@/theme'
import RegistrySourceModal from './RegistrySourceModal'
import { TestResultAlert, subtleButton } from './registryUi'

const AUTH_LABELS: Record<AuthType, string> = { none: 'No credentials', token: 'Token', basic: 'Basic', oauth: 'OAuth' }

const STATUS_LABELS: Record<RegistryStatus['status'], string> = {
  ok: 'ready',
  error: 'error',
  credentials_missing: 'credentials missing',
}

function pillStyle(background: string, color: string) {
  return { background, color, border: 'none', borderRadius: 999 }
}

/**
 * Page header card for the registries page, laid out like the Component Inventory header:
 * admins add and edit registries through a modal and see the configured registries listed below.
 */
export default function RegistrySources() {
  const queryClient = useQueryClient()
  // The source stays set while the modal fades out, so its title does not flip mid-close.
  const [modal, setModal] = useState<{ open: boolean; source: RegistrySource | null }>({ open: false, source: null })
  const openModal = (source: RegistrySource | null) => setModal({ open: true, source })
  const closeModal = () => setModal((current) => ({ ...current, open: false }))
  const [refreshing, setRefreshing] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowResults, setRowResults] = useState<Record<string, ConnectionTestResult>>({})

  const { data, isLoading, isError, error, refetch } = useRegistrySources()
  const { data: agents } = useAgents()

  const canManage = !!data?.canManage
  const storeError = data?.storeError
  const sources = data?.sources ?? []
  const statusOf = (id: string) => agents?.registries?.find((entry) => entry.id === id)

  async function refreshAll() {
    await Promise.all([refetch(), refreshAgents(queryClient)])
  }

  function handleSaved(source: RegistrySource) {
    setRowResults(({ [source.id]: _stale, ...rest }) => rest)
    closeModal()
    // Refresh in the background so the modal leaves its saving state as soon as the save succeeds.
    void refreshAll()
  }

  async function handleRemove(source: RegistrySource) {
    setBusyId(source.id)
    try {
      await removeRegistrySource(source.id)
      message.success(`Removed ${source.name}`)
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

  return (
    <div
      className="portal-shell-card"
      style={{
        background: colors.bgCard,
        border: `1px solid ${colors.border}`,
        borderRadius: 8,
        padding: '22px 24px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 8,
              background: colors.primaryGradient,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 20,
              color: '#fff',
              flexShrink: 0,
            }}
          >
            <DatabaseOutlined />
          </div>
          <div>
            <h1 style={{ fontSize: 24, fontWeight: 760, color: colors.textPrimary, margin: 0 }}>Manage Registries</h1>
            <p style={{ fontSize: 12, color: colors.textMuted, margin: '2px 0 0' }}>
              Agent and skill registries available to the Canvas
            </p>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          {canManage && (
            <Button
              icon={<PlusOutlined />}
              onClick={() => openModal(null)}
              style={{
                background: colors.infoSurface,
                border: `1px solid ${colors.primary}40`,
                color: colors.primary,
              }}
            >
              Add Registry
            </Button>
          )}
          <Button icon={<ReloadOutlined />} onClick={handleRefresh} loading={refreshing} style={subtleButton}>
            Refresh
          </Button>
        </div>
      </div>

      {(isLoading || isError || canManage) && (
        <div style={{ marginTop: 20 }}>
          {storeError && <Alert type="warning" showIcon message={storeError} style={{ marginBottom: 12 }} />}

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
          ) : !canManage ? null : sources.length === 0 ? (
            <div style={{ color: colors.textMuted, fontSize: 13 }}>No registries configured yet.</div>
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
                            <Tag style={pillStyle(colors.hoverSurface, colors.textSecondary)}>Published agents only</Tag>
                          )}
                          {source.skipTlsVerify && (
                            <Tag style={pillStyle('rgba(245,158,11,0.15)', '#92400E')}>TLS not verified</Tag>
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
                            <Tooltip title="Open URL">
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
                                onClick={() => openModal(source)}
                                style={subtleButton}
                              >
                                Edit
                              </Button>
                              <Popconfirm
                                title={`Remove ${source.name}?`}
                                description="Its saved credentials are deleted too."
                                okText="Remove"
                                okButtonProps={{ danger: true }}
                                onConfirm={() => handleRemove(source)}
                              >
                                <Button icon={<DeleteOutlined />} danger disabled={busyId === source.id}>
                                  Remove
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
      )}

      <RegistrySourceModal
        open={modal.open}
        editing={modal.source}
        storeError={storeError}
        onClose={closeModal}
        onSaved={handleSaved}
      />
    </div>
  )
}
