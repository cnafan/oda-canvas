import apiClient from './client'

export type AiGatewayState = 'ok' | 'not_deployed' | 'unauthorized' | 'unreachable'

export interface AiGatewayModel {
  name: string
  provider: string
  upstream: string
  status: 'active' | 'rejected'
  matches: string[]
}

export interface AiGatewayTool {
  name: string
  transport: string
  upstream: string
  stateful: boolean
  failureMode: string
  matches: string[]
}

export interface AiGatewayAgent {
  name: string
  matches: string[]
  backend: string
}

export interface AiGatewayTrafficPolicy {
  route: string
  matches: string[]
  policy: string
  settings: Record<string, unknown>
}

export interface AiGatewayDiscovery {
  tools: { name: string; description: string }[]
  resources: { name: string; uri: string; description: string }[]
  partial: boolean
}

export interface AiGatewaysSummary {
  state: AiGatewayState
  message: string
  version?: string
  models?: AiGatewayModel[]
  tools?: AiGatewayTool[]
  agents?: AiGatewayAgent[]
  traffic?: AiGatewayTrafficPolicy[]
  warnings?: string[]
  runtime?: { synchronized: boolean; lastError: string | null } | null
  discovered?: AiGatewayDiscovery | null
  discoveryError?: string | null
}

export async function fetchAiGateways(): Promise<AiGatewaysSummary> {
  const { data } = await apiClient.get<AiGatewaysSummary>('/api/ai-gateways')
  return data
}
