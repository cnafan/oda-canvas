import { useQuery, type QueryClient } from '@tanstack/react-query'
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

export interface RegistryRef {
  id: string
  name: string
  /** True for the in-cluster registry configured through the chart. */
  builtin: boolean
}

export interface AgentListItem extends AgentCard {
  publishStatus?: string
  health?: AgentHealth | null
  registry?: RegistryRef
}

export interface RegistryStatus extends RegistryRef {
  status: 'ok' | 'error' | 'credentials_missing'
  agentCount: number
  /** False when only published agents could be read from this registry. */
  fullAccess: boolean
  message?: string | null
}

export interface AgentList {
  agents: AgentListItem[]
  statusCounts: Record<string, number>
  heartbeatEnabled: boolean
  /** False for non-admins, who only receive published agents. */
  fullAccess: boolean
  registries?: RegistryStatus[]
  /** Set when saved external registries could not be read. */
  storeError?: string | null
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
  registry?: RegistryRef
}

export type AuthType = 'none' | 'token' | 'basic' | 'oauth'
export type GrantType = 'client_credentials' | 'password'

export interface RegistrySource {
  id: string
  name: string
  url: string
  /** The registry's own web page, for people to open. */
  pageUrl?: string | null
  authType: AuthType
  grantType?: GrantType | null
  tokenUrl?: string | null
  skipTlsVerify?: boolean
  credentialsSet: boolean
  builtin: boolean
}

export interface RegistrySourceList {
  canManage: boolean
  /** Why external registries can't be saved or read right now. */
  storeError: string | null
  sources: RegistrySource[]
}

export interface RegistrySourceInput {
  name: string
  url: string
  pageUrl?: string
  authType: AuthType
  skipTlsVerify: boolean
  allowInsecureHttp: boolean
  token?: string
  username?: string
  password?: string
  tokenUrl?: string
  grantType?: GrantType
  clientId?: string
  clientSecret?: string
  scope?: string
}

export interface ConnectionTestResult {
  ok: boolean
  classification: string
  message: string
  /** 'admin' can read every review state; 'discovery' only published agents. */
  level: 'admin' | 'discovery' | null
  agentCount: number | null
  warnings: string[]
}

/** Pass refresh to skip the backend's short cache of external registry results. */
export async function fetchAgents(refresh = false): Promise<AgentList> {
  const { data } = await apiClient.get<AgentList>('/api/registries/agents', {
    params: refresh ? { refresh: true } : undefined,
  })
  return data
}

const AGENTS_KEY = ['registry-agents']
const SOURCES_KEY = ['registry-sources']

/** The agent list, shared by the Agents, Skills and Manage Registries sections. */
export function useAgents() {
  return useQuery({ queryKey: AGENTS_KEY, queryFn: () => fetchAgents() })
}

export function useRegistrySources() {
  return useQuery({ queryKey: SOURCES_KEY, queryFn: fetchRegistrySources })
}

/**
 * Reload the shared agent list, skipping both the backend's cache and React
 * Query's: without staleTime 0, fetchQuery can return cached data with no request.
 * A failure is left for the query's error state to show.
 */
export function refreshAgents(queryClient: QueryClient) {
  return queryClient
    .fetchQuery({ queryKey: AGENTS_KEY, queryFn: () => fetchAgents(true), staleTime: 0 })
    .catch(() => undefined)
}

export async function fetchAgent(organization: string, name: string, registryId?: string): Promise<AgentDetail> {
  const agentPath = `${encodeURIComponent(organization)}/${encodeURIComponent(name)}`
  const path = registryId ? `${encodeURIComponent(registryId)}/${agentPath}` : agentPath
  const { data } = await apiClient.get<AgentDetail>(`/api/registries/agents/${path}`)
  return data
}

/** The page URL of an agent. In-cluster agents keep their original, shorter URL. */
export function agentPagePath(agent: AgentListItem): string {
  const agentPath = `${encodeURIComponent(agent.provider?.organization ?? '')}/${encodeURIComponent(agent.name)}`
  return agent.registry && !agent.registry.builtin
    ? `/registries/agents/${encodeURIComponent(agent.registry.id)}/${agentPath}`
    : `/registries/agents/${agentPath}`
}

export async function fetchRegistrySources(): Promise<RegistrySourceList> {
  const { data } = await apiClient.get<RegistrySourceList>('/api/registries/sources')
  return data
}

export async function addRegistrySource(input: RegistrySourceInput): Promise<RegistrySource> {
  const { data } = await apiClient.post<RegistrySource>('/api/registries/sources', input)
  return data
}

/** Credential fields left blank keep the saved credentials, while the address and authentication stay the same. */
export async function updateRegistrySource(id: string, input: RegistrySourceInput): Promise<RegistrySource> {
  const { data } = await apiClient.put<RegistrySource>(`/api/registries/sources/${encodeURIComponent(id)}`, input)
  return data
}

export async function removeRegistrySource(id: string): Promise<void> {
  await apiClient.delete(`/api/registries/sources/${encodeURIComponent(id)}`)
}

export async function testRegistryConnection(input: RegistrySourceInput): Promise<ConnectionTestResult> {
  const { data } = await apiClient.post<ConnectionTestResult>('/api/registries/sources/test', input)
  return data
}

/** With edits, tests those values before they are saved. Without, tests the registry as saved. */
export async function testSavedRegistry(id: string, edits?: RegistrySourceInput): Promise<ConnectionTestResult> {
  const { data } = await apiClient.post<ConnectionTestResult>(`/api/registries/sources/${encodeURIComponent(id)}/test`, edits)
  return data
}

/** The message the backend sent, for showing "not configured" or "unreachable". */
export function registryErrorMessage(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : 'Failed to load data from the Registry Center.'
}
