import { t } from '@/i18n'
import React, { useState } from 'react'
import { Alert, Card, Spin, Table, Tabs, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ApiOutlined,
  AppstoreOutlined,
  CodeOutlined,
  HistoryOutlined,
  RobotOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import {
  useAgentDetail,
  registryErrorMessage,
  type AgentInterface,
  type ReviewTransition,
} from '@/api/registries'
import { InfoRow, RawDocumentView } from '@/pages/Components/ComponentDetailBody'
import { colors } from '@/theme'
import { displayAgentName } from './displayAgentName'
import { AgentIcon } from './registryUi'

/** Active is defined as approved for discovery in the Registry Center. */
export function isActiveAgent(publishStatus?: string | null): boolean {
  return publishStatus === 'published'
}

const STATUS_PILLS: Record<string, { label: string; bg: string; border: string; text: string; dot: string }> = {
  published: { label: t("Active"), bg: '#DCFCE7', border: '#86EFAC', text: '#166534', dot: '#16A34A' },
  registered: { label: t("Registered"), bg: '#FEF3C7', border: '#FCD34D', text: '#92400E', dot: '#F59E0B' },
  rejected: { label: t("Rejected"), bg: '#FEE2E2', border: '#FCA5A5', text: '#991B1B', dot: '#DC2626' },
  deprecated: { label: t("Deprecated"), bg: '#F3F4F6', border: '#D1D5DB', text: '#374151', dot: '#6B7280' },
}

export function AgentStatusPill({ status }: { status?: string | null }) {
  const pill = STATUS_PILLS[status ?? ''] ?? { ...STATUS_PILLS.deprecated, label: status || 'Unknown' }
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 8px',
        borderRadius: 999,
        background: pill.bg,
        border: `1px solid ${pill.border}`,
        color: pill.text,
        fontSize: 10,
        fontWeight: 700,
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: pill.dot, display: 'inline-block' }} />
      {pill.label}
    </span>
  )
}

const cardStyle = { background: colors.bgCard, border: `1px solid ${colors.border}`, borderRadius: 12 }

function Empty({ children }: { children: React.ReactNode }) {
  return <div style={{ color: colors.textMuted, padding: '24px 0', fontSize: 13 }}>{children}</div>
}

function TagList({ values }: { values?: string[] }) {
  if (!values?.length) return <>—</>
  return (
    <span>
      {values.map((value) => (
        <Tag key={value} style={{ marginInlineEnd: 4 }}>
          {value}
        </Tag>
      ))}
    </span>
  )
}

function formatTime(value?: string) {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '—'
}

/** Links only for http(s), since card values are supplied by the agent's publisher. */
function ExternalLink({ href }: { href?: string }) {
  if (!href) return <>—</>
  if (!/^https?:\/\//i.test(href)) return <>{href}</>
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {href}
    </a>
  )
}

const interfaceColumns: ColumnsType<AgentInterface> = [
  {
    title: 'URL',
    dataIndex: 'url',
    render: (url?: string) => <span style={{ fontFamily: 'monospace', fontSize: 12, overflowWrap: 'anywhere' }}>{url ?? '—'}</span>,
  },
  { title: t("Protocol binding"), dataIndex: 'protocolBinding', render: (value?: string) => value ?? '—' },
  { title: t("Protocol version"), dataIndex: 'protocolVersion', render: (value?: string) => value ?? '—' },
]

const reviewColumns: ColumnsType<ReviewTransition> = [
  { title: 'Time', dataIndex: 'at', render: formatTime },
  { title: 'From', dataIndex: 'from_status', render: (value?: string | null) => value || '—' },
  { title: 'To', dataIndex: 'to_status', render: (value?: string) => value || '—' },
  { title: 'Reason', dataIndex: 'reason', render: (value?: string) => value || '—' },
  { title: 'Operator', dataIndex: 'operator', render: (value?: string) => value || '—' },
]

export default function AgentDetailBody({
  organization,
  name,
  registryId,
}: {
  organization: string
  name: string
  /** Omitted for the in-cluster registry. */
  registryId?: string
}) {
  const [rawFormat, setRawFormat] = useState<'yaml' | 'json'>('yaml')

  const { data, isLoading, isError, error } = useAgentDetail(organization, name, registryId)

  if (isLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}>
        <Spin />
      </div>
    )
  }
  if (isError || !data) {
    return <Alert type="error" showIcon message="Failed to load agent" description={registryErrorMessage(error)} />
  }

  const { card, publishStatus, health, heartbeatEnabled, layer, review, fullAccess, registry } = data
  const capabilities = card.capabilities ?? {}
  const securitySchemes = Object.keys(card.securitySchemes ?? {})
  const history = review?.history ?? []

  const tabItems = [
    {
      key: 'overview',
      label: <span><AppstoreOutlined style={{ marginRight: 6 }} />{t("Overview")}</span>,
      children: (
        <Card style={cardStyle}>
          {registry && <InfoRow label={t("Registry")} value={registry.name} />}
          <InfoRow label={t("Organization")} value={card.provider?.organization ?? '—'} />
          <InfoRow label={t("Provider URL")} value={<ExternalLink href={card.provider?.url} />} />
          <InfoRow label={t("Description")} value={card.description ?? '—'} />
          <InfoRow label={t("Version")} value={card.version ?? '—'} />
          <InfoRow label={t("Layer")} value={layer ?? '—'} />
          <InfoRow label={t("Publish status")} value={publishStatus ?? '—'} />
          <InfoRow label={t("Documentation")} value={<ExternalLink href={card.documentationUrl} />} />
          <InfoRow label={t("Input modes")} value={<TagList values={card.defaultInputModes} />} />
          <InfoRow label={t("Output modes")} value={<TagList values={card.defaultOutputModes} />} />
          {heartbeatEnabled && (
            <>
              <InfoRow label={t("Health")} value={health?.health_status ?? 'No heartbeat received'} />
              <InfoRow label={t("Last heartbeat")} value={formatTime(health?.last_heartbeat_at)} />
            </>
          )}
        </Card>
      ),
    },
    {
      key: 'skills',
      label: <span><ThunderboltOutlined style={{ marginRight: 6 }} />Skills ({card.skills?.length ?? 0})</span>,
      children: card.skills?.length ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {card.skills.map((skill, index) => (
            <Card key={skill.id ?? index} size="small" style={cardStyle}>
              <div style={{ fontSize: 14, fontWeight: 600, color: colors.textPrimary }}>{skill.name ?? skill.id ?? '—'}</div>
              {skill.description && (
                <div style={{ fontSize: 13, color: colors.textSecondary, margin: '6px 0' }}>{skill.description}</div>
              )}
              {!!skill.tags?.length && <TagList values={skill.tags} />}
              {!!skill.examples?.length && (
                <ul style={{ fontSize: 12, color: colors.textSecondary, margin: '8px 0 0', paddingLeft: 18 }}>
                  {skill.examples.map((example) => (
                    <li key={example}>{example}</li>
                  ))}
                </ul>
              )}
            </Card>
          ))}
        </div>
      ) : (
        <Empty>{t("This agent declares no skills.")}</Empty>
      ),
    },
    {
      key: 'interfaces',
      label: <span><ApiOutlined style={{ marginRight: 6 }} />{t("Interfaces")}</span>,
      children: card.supportedInterfaces?.length ? (
        <Table
          size="small"
          pagination={false}
          rowKey={(row) => `${row.url}-${row.protocolBinding}`}
          columns={interfaceColumns}
          dataSource={card.supportedInterfaces}
        />
      ) : (
        <Empty>{t("This agent declares no interfaces.")}</Empty>
      ),
    },
    {
      key: 'capabilities',
      label: <span><SettingOutlined style={{ marginRight: 6 }} />{t("Capabilities")}</span>,
      children: (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Card style={cardStyle}>
            <InfoRow label={t("Streaming")} value={capabilities.streaming ? 'Yes' : 'No'} />
            <InfoRow label={t("Push notifications")} value={capabilities.pushNotifications ? 'Yes' : 'No'} />
            <InfoRow label={t("State history")} value={capabilities.stateTransitionHistory ? 'Yes' : 'No'} />
            <InfoRow label={t("Security schemes")} value={<TagList values={securitySchemes} />} />
          </Card>
          {(capabilities.extensions ?? []).map((extension, index) => (
            <Card key={extension.uri ?? index} size="small" title={t("Extension")} style={cardStyle}>
              <div style={{ fontFamily: 'monospace', fontSize: 12, overflowWrap: 'anywhere', color: colors.textPrimary }}>
                {extension.uri ?? '—'}
              </div>
              {extension.description && (
                <div style={{ fontSize: 13, color: colors.textSecondary, marginTop: 6 }}>{extension.description}</div>
              )}
              {extension.required && <Tag style={{ marginTop: 8 }}>{t("Required")}</Tag>}
              {extension.params && (
                <pre style={{ fontSize: 12, margin: '8px 0 0', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                  {JSON.stringify(extension.params, null, 2)}
                </pre>
              )}
            </Card>
          ))}
        </div>
      ),
    },
    ...(fullAccess
      ? [
          {
            key: 'review',
            label: <span><HistoryOutlined style={{ marginRight: 6 }} />{t("Review")}</span>,
            children: history.length ? (
              <Table
                size="small"
                pagination={false}
                rowKey={(row) => `${row.at}-${row.to_status}`}
                columns={reviewColumns}
                dataSource={history}
              />
            ) : (
              <Empty>{review ? 'No review transitions recorded.' : 'Review history is unavailable.'}</Empty>
            ),
          },
        ]
      : []),
    {
      key: 'raw',
      label: <span><CodeOutlined style={{ marginRight: 6 }} />Raw</span>,
      children: (
        <RawDocumentView
          loading={false}
          error={null}
          data={card as unknown as Record<string, unknown>}
          format={rawFormat}
          onFormatChange={setRawFormat}
          filenameBase={`${organization}-${name}`}
        />
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 24 }}>
        <AgentIcon
          iconUrl={card.iconUrl}
          fallback={
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: 10,
                background: colors.primaryGradient,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontSize: 20,
              }}
            >
              <RobotOutlined />
            </div>
          }
        />
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, color: colors.textPrimary, margin: 0 }}>{displayAgentName(card.name, card.provider?.organization)}</h1>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
            <Tag
              style={{
                background: colors.infoSurface,
                color: colors.infoText,
                border: 'none',
                borderRadius: 4,
                fontFamily: 'monospace',
                fontSize: 12,
              }}
            >
              {organization}
            </Tag>
            <AgentStatusPill status={publishStatus} />
          </div>
        </div>
      </div>

      <div style={{ background: colors.bgCard, borderRadius: 12, border: `1px solid ${colors.border}`, overflow: 'hidden' }}>
        <Tabs
          items={tabItems.map((tab) => ({
            ...tab,
            children: <div style={{ padding: '20px 4px' }}>{tab.children}</div>,
          }))}
          style={{ padding: '0 20px' }}
          tabBarStyle={{ borderBottom: `1px solid ${colors.border}`, marginBottom: 0 }}
          tabBarGutter={24}
        />
      </div>
    </div>
  )
}
