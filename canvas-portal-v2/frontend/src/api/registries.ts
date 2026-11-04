import apiClient from './client'

// Registry Center cards are serialized with protobuf MessageToDict, which drops
// false and empty values, so every card field may be missing.
export interface AgentSkill {
  id?: string
  name?: string
  description?: string
  tags?: string[]
  examples?: string[]
}

export interface AgentInterface {
  url?: string
  protocolBinding?: string
  protocolVersion?: string
}

export interface AgentExtension {
  uri?: string
  description?: string
  required?: boolean
  params?: Record<string, unknown>
}

export interface AgentCard {
  name: string
  description?: string
  version?: string
  documentationUrl?: string
  provider?: { organization?: string; url?: string }
  skills?: AgentSkill[]
  supportedInterfaces?: AgentInterface[]
  capabilities?: {
    streaming?: boolean
    pushNotifications?: boolean
    stateTransitionHistory?: boolean
    extendedAgentCard?: boolean
    extensions?: AgentExtension[]
  }
  securitySchemes?: Record<string, unknown>
  defaultInputModes?: string[]
  defaultOutputModes?: string[]
}

export interface AgentHealth {
  health_status?: string
  last_heartbeat_at?: string
  status_changed_at?: string
}

export interface AgentListItem extends AgentCard {
  publishStatus?: string
  health?: AgentHealth | null
}

export interface AgentList {
  agents: AgentListItem[]
  statusCounts: Record<string, number>
  heartbeatEnabled: boolean
  /** False for non-admins, who only receive published agents. */
  fullAccess: boolean
}

export interface ReviewTransition {
  from_status?: string | null
  to_status?: string
  reason?: string
  operator?: string
  at?: string
}

export interface AgentDetail {
  card: AgentCard
  publishStatus?: string | null
  health?: AgentHealth | null
  heartbeatEnabled: boolean
  layer?: string | null
  review?: { status?: string; history?: ReviewTransition[] } | null
  fullAccess: boolean
}

export async function fetchAgents(): Promise<AgentList> {
  const { data } = await apiClient.get<AgentList>('/api/registries/agents')
  return data
}

export async function fetchAgent(organization: string, name: string): Promise<AgentDetail> {
  const { data } = await apiClient.get<AgentDetail>(
    `/api/registries/agents/${encodeURIComponent(organization)}/${encodeURIComponent(name)}`
  )
  return data
}

/** The message the backend sent, for showing "not configured" or "unreachable". */
export function registryErrorMessage(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : 'Failed to load data from the Registry Center.'
}
