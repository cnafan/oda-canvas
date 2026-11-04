import apiClient from './client'
import { getApiBaseUrl } from '@/config'
import { getToken, logout, refreshAccessToken } from '@/lib/auth'

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
  runtime?: { synchronized: boolean } | null
  discovered?: AiGatewayDiscovery | null
  discoveryError?: string | null
}

export async function fetchAiGateways(): Promise<AiGatewaysSummary> {
  const { data } = await apiClient.get<AiGatewaysSummary>('/api/ai-gateways')
  return data
}

export type ModelTestEvent =
  | { type: 'reasoning' | 'content'; text: string }
  | { type: 'done'; finishReason: string | null }
  | { type: 'error'; message: string }

function postModelTest(model: string, signal: AbortSignal): Promise<Response> {
  const token = getToken()
  return fetch(`${getApiBaseUrl()}/api/ai-gateways/models/test`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ model }),
    signal,
  })
}

async function requestModelTest(model: string, signal: AbortSignal): Promise<Response> {
  const response = await postModelTest(model, signal)
  // fetch skips the apiClient interceptor, so refresh an expired session here.
  if (response.status !== 401) return response
  if (await refreshAccessToken()) return postModelTest(model, signal)
  logout()
  return response
}

/** Sends PING to a model and calls onEvent for each event. Abort the signal to stop it. */
export async function streamModelTest(
  model: string,
  onEvent: (event: ModelTestEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await requestModelTest(model, signal)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : `Request failed: HTTP ${response.status}`)
  }
  if (!response.body) throw new Error('The response has no content')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let pending = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) return
    // An event can arrive split across reads, so only parse complete lines.
    const lines = (pending + decoder.decode(value, { stream: true })).split('\n')
    pending = lines.pop() ?? ''
    for (const line of lines) {
      if (line) onEvent(JSON.parse(line) as ModelTestEvent)
    }
  }
}
