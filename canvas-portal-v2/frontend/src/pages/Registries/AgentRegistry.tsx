import { t } from '@/i18n'
import React, { useState } from 'react'
import { Alert, Button, Drawer, Grid, Input, Select, Tag, Tooltip } from 'antd'
import { CloseOutlined, ReloadOutlined, RightOutlined, SearchOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  agentPagePath,
  refreshAgents,
  registryErrorMessage,
  useAgentDetail,
  useAgents,
  type AgentListItem,
} from '@/api/registries'
import { HexBadge, type DomainPaint } from '@/components/ComponentTopology'
import { colors } from '@/theme'
import AgentDetailBody, { AgentStatusPill, isActiveAgent } from './AgentDetailBody'
import { displayAgentName } from './displayAgentName'
import { AgentIcon } from './registryUi'

export const ACTIVE_PAINT: DomainPaint = { hexFill: '#16A34A', hexBorder: '#166534', badgeBg: '#DCFCE7', badgeText: '#166534' }
export const IDLE_PAINT: DomainPaint = { hexFill: '#6B7280', hexBorder: '#4B5563', badgeBg: '#F3F4F6', badgeText: '#374151' }

const ACTIVE_BG = 'linear-gradient(180deg, #F0FDF4 0%, #DCFCE7 100%)'
const ACTIVE_BORDER = '#86EFAC'
const BASE_SHADOW = '0 1px 2px rgba(15,23,42,0.04)'
const HOVER_SHADOW = '0 4px 12px rgba(15,23,42,0.08)'

const organizationOf = (agent: AgentListItem) => agent.provider?.organization ?? ''

/** The look and lift-on-hover shared by agent and skill tiles. */
export function tileProps(active: boolean) {
  const restingBorder = active ? ACTIVE_BORDER : colors.border
  const style: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    width: '100%',
    padding: '12px 14px',
    borderRadius: 8,
    border: `1px solid ${restingBorder}`,
    background: active ? ACTIVE_BG : '#FFFFFF',
    boxShadow: BASE_SHADOW,
    cursor: 'pointer',
    textAlign: 'left',
    transition: 'transform 0.18s cubic-bezier(0.22, 1, 0.36, 1), box-shadow 0.18s ease, border-color 0.15s ease',
    fontFamily: 'inherit',
  }
  return {
    style,
    onMouseEnter: (e: React.MouseEvent<HTMLButtonElement>) => {
      const n = e.currentTarget
      n.style.transform = 'translateY(-2px)'
      n.style.boxShadow = HOVER_SHADOW
      n.style.borderColor = colors.primary
    },
    onMouseLeave: (e: React.MouseEvent<HTMLButtonElement>) => {
      const n = e.currentTarget
      n.style.transform = 'translateY(0)'
      n.style.boxShadow = BASE_SHADOW
      n.style.borderColor = restingBorder
    },
  }
}

function AgentTile({
  agent,
  showRegistry,
  onSelect,
}: {
  agent: AgentListItem
  showRegistry: boolean
  onSelect: () => void
}) {
  const active = isActiveAgent(agent.publishStatus)
  const organization = organizationOf(agent)
  const agentName = displayAgentName(agent.name, organization)
  const skillCount = agent.skills?.length ?? 0
  const paint = active ? ACTIVE_PAINT : IDLE_PAINT
  // The layer is only in the agent's details; version and status fall back to the listing until they load.
  const { data: detail } = useAgentDetail(
    organization,
    agent.name,
    agent.registry && !agent.registry.builtin ? agent.registry.id : undefined
  )
  const layer = detail?.layer
  const version = detail?.card.version ?? agent.version
  const publishStatus = detail?.publishStatus ?? agent.publishStatus

  const tooltip = (
    <div style={{ minWidth: 220 }}>
      <div style={{ fontWeight: 700, marginBottom: 6, color: colors.textPrimary }}>{agentName}</div>
      {agent.description && (
        <div style={{ fontSize: 12, color: colors.textSecondary, marginBottom: 4 }}>{agent.description}</div>
      )}
      <div style={{ fontSize: 11, color: colors.textMuted, fontFamily: 'monospace' }}>
        {[agent.version && `v${agent.version}`, `${skillCount} skill${skillCount === 1 ? '' : 's'}`]
          .filter(Boolean)
          .join(' · ')}
      </div>
      <div
        style={{
          marginTop: 8,
          paddingTop: 8,
          borderTop: `1px solid ${colors.border}`,
          fontSize: 11,
          color: colors.primary,
          fontWeight: 600,
        }}
      >

        {t("Click to view agent details")}
      </div>
    </div>
  )

  return (
    <Tooltip title={tooltip} color="#FFFFFF" overlayStyle={{ maxWidth: 320 }} mouseEnterDelay={0.25}>
      <button
        type="button"
        aria-label={`Open ${agentName}`}
        onClick={onSelect}
        {...tileProps(active)}
      >
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          <HexBadge paint={active ? ACTIVE_PAINT : IDLE_PAINT} label="AGENT" />
          <AgentIcon iconUrl={agent.iconUrl} />
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: colors.textPrimary,
              lineHeight: 1.3,
              overflow: 'hidden',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflowWrap: 'anywhere',
            }}
          >
            {agentName}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
            {organization && (
              <span
                style={{
                  display: 'inline-block',
                  padding: '1px 6px',
                  borderRadius: 4,
                  background: paint.badgeBg,
                  color: paint.badgeText,
                  fontSize: 10,
                  fontWeight: 700,
                  fontFamily: 'monospace',
                  letterSpacing: 0.3,
                }}
              >
                {organization}
              </span>
            )}
            {layer && <Tag style={{ marginInlineEnd: 0, fontSize: 10 }}>{layer}</Tag>}
            {version && <Tag style={{ marginInlineEnd: 0, fontSize: 10 }}>v{version}</Tag>}
            <AgentStatusPill status={publishStatus} />
          </div>
          {showRegistry && agent.registry && (
            <div style={{ fontSize: 11, color: colors.textMuted, marginTop: 4, overflowWrap: 'anywhere' }}>
              {agent.registry.name}
            </div>
          )}
        </div>

        <RightOutlined style={{ fontSize: 11, color: colors.textMuted, flexShrink: 0 }} />
      </button>
    </Tooltip>
  )
}

export function Notice({ tone = 'muted', children }: { tone?: 'muted' | 'error'; children: React.ReactNode }) {
  const error = tone === 'error'
  return (
    <div
      style={{
        padding: 16,
        borderRadius: 8,
        fontSize: 13,
        textAlign: error ? 'left' : 'center',
        color: error ? colors.statusDown : colors.textMuted,
        background: error ? colors.statusDownBg : colors.surfaceSubtle,
        border: error ? `1px solid ${colors.statusDown}40` : `1px dashed ${colors.border}`,
      }}
    >
      {children}
    </div>
  )
}

export default function AgentRegistry() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const {
    registryId: paramRegistry,
    organization: paramOrg,
    name: paramName,
  } = useParams<{ registryId?: string; organization?: string; name?: string }>()
  const screens = Grid.useBreakpoint()
  const selected = paramOrg && paramName ? { registryId: paramRegistry, organization: paramOrg, name: paramName } : null

  const [search, setSearch] = useState('')
  const [orgFilter, setOrgFilter] = useState('all')
  const [registryFilter, setRegistryFilter] = useState('all')

  const { data, isLoading, isError, error, isFetching } = useAgents()

  const agents = data?.agents ?? []
  const registries = data?.registries ?? []
  const failedRegistries = registries.filter((registry) => registry.status !== 'ok')
  const showRegistries = registries.length > 1
  const organizations = Array.from(new Set(agents.map(organizationOf).filter(Boolean))).sort()
  const activeCount = agents.filter((agent) => isActiveAgent(agent.publishStatus)).length

  const term = search.trim().toLowerCase()
  const filtered = agents
    .filter((agent) => orgFilter === 'all' || organizationOf(agent) === orgFilter)
    .filter((agent) => registryFilter === 'all' || agent.registry?.id === registryFilter)
    .filter(
      (agent) =>
        !term ||
        agent.name.toLowerCase().includes(term) ||
        organizationOf(agent).toLowerCase().includes(term) ||
        (agent.skills ?? []).some((skill) => (skill.name ?? '').toLowerCase().includes(term))
    )
    .sort((a, b) => a.name.localeCompare(b.name))

  function closeDrawer() {
    navigate('/registries')
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <Input
          placeholder={t("Search agents...")}
          prefix={<SearchOutlined style={{ color: colors.textMuted }} />}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          allowClear
          style={{ width: 300 }}
        />
        <Select
          value={orgFilter}
          onChange={setOrgFilter}
          style={{ width: 220 }}
          options={[
            { value: 'all', label: t("All Organizations") },
            ...organizations.map((organization) => ({ value: organization, label: organization })),
          ]}
        />
        {showRegistries && (
          <Select
            value={registryFilter}
            onChange={setRegistryFilter}
            style={{ width: 220 }}
            options={[
              { value: 'all', label: t("All Registries") },
              ...registries.map((registry) => ({ value: registry.id, label: registry.name })),
            ]}
          />
        )}
        <div style={{ flex: 1 }} />
        {data && (
          <span style={{ fontSize: 11, color: colors.textMuted, fontWeight: 600 }}>
            {data.fullAccess ? `${activeCount}/${agents.length} active` : 'Showing published agents only'}
          </span>
        )}
        <Button
          icon={<ReloadOutlined />}
          onClick={() => refreshAgents(queryClient)}
          loading={isFetching}
          style={{ background: 'transparent', border: `1px solid ${colors.border}`, color: colors.textSecondary }}
        >

          {t("Refresh")}
        </Button>
      </div>

      {isError && <Notice tone="error">{registryErrorMessage(error)}</Notice>}

      {isLoading && <Notice>{t("Loading agents…")}</Notice>}

      {(data?.storeError || failedRegistries.length > 0) && (
        <Alert
          type="warning"
          showIcon
          message="Some registries could not be read"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {data?.storeError && <li>External registries: {data.storeError}</li>}
              {failedRegistries.map((registry) => (
                <li key={registry.id}>
                  {registry.name}: {registry.message ?? 'Unavailable'}
                </li>
              ))}
            </ul>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {!isLoading && !isError && agents.length === 0 && (
        <Notice>
          {data?.fullAccess
            ? 'No agents are registered in the available registries.'
            : 'No published agents in the available registries.'}
        </Notice>
      )}

      {!isLoading && !isError && agents.length > 0 && filtered.length === 0 && (
        <Notice>{t("No agents match the current filters.")}</Notice>
      )}

      {!isLoading && !isError && filtered.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12 }}>
          {filtered.map((agent) => (
            <AgentTile
              key={`${agent.registry?.id}/${organizationOf(agent)}/${agent.name}`}
              agent={agent}
              showRegistry={showRegistries}
              onSelect={() => navigate(agentPagePath(agent))}
            />
          ))}
        </div>
      )}

      <Drawer
        open={!!selected}
        onClose={closeDrawer}
        placement="right"
        width={screens.lg ? '64%' : '100%'}
        destroyOnClose
        closeIcon={<CloseOutlined />}
        styles={{
          header: {
            background: colors.bgCard,
            borderBottom: `1px solid ${colors.border}`,
            padding: '14px 22px',
          },
          body: {
            background: colors.bgBase,
            padding: '22px 24px 32px',
          },
        }}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span
              style={{
                fontSize: 11,
                fontWeight: 700,
                color: colors.textMuted,
                textTransform: 'uppercase',
                letterSpacing: 0.6,
              }}
            >

              {t("Agent")}
            </span>
            <span style={{ color: colors.border }}>·</span>
            <span style={{ fontSize: 13, color: colors.textPrimary, fontWeight: 600 }}>
              {selected ? `${selected.organization}/${displayAgentName(selected.name, selected.organization)}` : ''}
            </span>
          </div>
        }
      >
        {selected && (
          <AgentDetailBody
            organization={selected.organization}
            name={selected.name}
            registryId={selected.registryId}
          />
        )}
      </Drawer>
    </div>
  )
}
