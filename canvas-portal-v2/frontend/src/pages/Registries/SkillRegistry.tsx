import { t } from '@/i18n'
import React, { useState } from 'react'
import { Input, Select, Tag } from 'antd'
import { RightOutlined, SearchOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { agentPagePath, registryErrorMessage, useAgents, type AgentListItem, type AgentSkill } from '@/api/registries'
import { HexBadge } from '@/components/ComponentTopology'
import { colors } from '@/theme'
import { ACTIVE_PAINT, IDLE_PAINT, Notice, tileProps } from './AgentRegistry'
import { isActiveAgent } from './AgentDetailBody'
import { displayAgentName } from './displayAgentName'
import { AgentIcon } from './registryUi'

interface SkillEntry {
  key: string
  skill: AgentSkill
  agent: AgentListItem
}

/** No registry has a skills API, so skills are read from the agent cards of every registry. */
function flattenSkills(agents: AgentListItem[]): SkillEntry[] {
  return agents.flatMap((agent) =>
    (agent.skills ?? []).map((skill, index) => ({
      key: `${agent.registry?.id}/${agent.provider?.organization}/${agent.name}/${skill.id ?? index}`,
      skill,
      agent,
    }))
  )
}

function SkillTile({ entry, onSelect }: { entry: SkillEntry; onSelect: () => void }) {
  const { skill, agent } = entry
  const active = isActiveAgent(agent.publishStatus)
  const tile = tileProps(active)
  const skillName = skill.name ?? skill.id ?? 'Unnamed skill'
  const agentName = displayAgentName(agent.name, agent.provider?.organization)

  return (
    <button
      type="button"
      aria-label={`Open ${agentName}, which provides ${skillName}`}
      onClick={onSelect}
      {...tile}
      style={{ ...tile.style, alignItems: 'flex-start' }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        <HexBadge paint={active ? ACTIVE_PAINT : IDLE_PAINT} label="SKILL" />
        <AgentIcon iconUrl={agent.iconUrl} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: colors.textPrimary, overflowWrap: 'anywhere' }}>{skillName}</div>
        {skill.description && (
          <div
            style={{
              fontSize: 12,
              color: colors.textSecondary,
              marginTop: 4,
              overflow: 'hidden',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflowWrap: 'anywhere',
            }}
          >
            {skill.description}
          </div>
        )}
        {!!skill.tags?.length && (
          <div style={{ marginTop: 6 }}>
            {skill.tags.map((tag) => (
              <Tag key={tag} style={{ marginInlineEnd: 4, fontSize: 10 }}>
                {tag}
              </Tag>
            ))}
          </div>
        )}
        <div style={{ fontSize: 11, color: colors.textMuted, marginTop: 6, overflowWrap: 'anywhere' }}>
          {agentName}
          {agent.registry && ` · ${agent.registry.name}`}
        </div>
      </div>
      <RightOutlined style={{ fontSize: 11, color: colors.textMuted, marginTop: 4 }} />
    </button>
  )
}

export default function SkillRegistry() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [registryFilter, setRegistryFilter] = useState('all')

  const { data, isLoading, isError, error } = useAgents()

  const entries = flattenSkills(data?.agents ?? [])
  const registries = Array.from(new Map(entries.map(({ agent }) => [agent.registry?.id, agent.registry])).values())

  const term = search.trim().toLowerCase()
  const filtered = entries
    .filter(({ agent }) => registryFilter === 'all' || agent.registry?.id === registryFilter)
    .filter(
      ({ skill, agent }) =>
        !term ||
        [skill.name, skill.id, skill.description, agent.name, ...(skill.tags ?? [])].some((value) =>
          (value ?? '').toLowerCase().includes(term)
        )
    )
    .sort((a, b) => (a.skill.name ?? a.skill.id ?? '').localeCompare(b.skill.name ?? b.skill.id ?? ''))

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <Input
          placeholder={t("Search skills...")}
          prefix={<SearchOutlined style={{ color: colors.textMuted }} />}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          allowClear
          style={{ width: 300 }}
        />
        {registries.length > 1 && (
          <Select
            value={registryFilter}
            onChange={setRegistryFilter}
            style={{ width: 220 }}
            options={[
              { value: 'all', label: t("All Registries") },
              ...registries.map((registry) => ({ value: registry?.id ?? '', label: registry?.name ?? '' })),
            ]}
          />
        )}
        <div style={{ flex: 1 }} />
        {data && (
          <span style={{ fontSize: 11, color: colors.textMuted, fontWeight: 600 }}>
            {entries.length} skill{entries.length === 1 ? '' : 's'}
          </span>
        )}
      </div>

      {isError && <Notice tone="error">{registryErrorMessage(error)}</Notice>}
      {isLoading && <Notice>{t("Loading skills…")}</Notice>}
      {!isLoading && !isError && entries.length === 0 && (
        <Notice>{t("No skills are advertised by the listed agents.")}</Notice>
      )}
      {!isLoading && !isError && entries.length > 0 && filtered.length === 0 && (
        <Notice>{t("No skills match the current filters.")}</Notice>
      )}
      {!isLoading && !isError && filtered.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12 }}>
          {filtered.map((entry) => (
            <SkillTile key={entry.key} entry={entry} onSelect={() => navigate(agentPagePath(entry.agent))} />
          ))}
        </div>
      )}
    </div>
  )
}
