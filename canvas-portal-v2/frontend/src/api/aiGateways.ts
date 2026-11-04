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

/** The part of a JSON Schema that the backend passes on. Other fields are dropped. */
export interface McpSchema {
  type?: string | string[]
  title?: string
  description?: string
  default?: unknown
  minimum?: number
  maximum?: number
  enum?: unknown[]
  required?: string[]
  properties?: Record<string, McpSchema>
  anyOf?: McpSchema[]
  oneOf?: McpSchema[]
}

export interface McpTool {
  name: string
  title: string
  description: string
  inputSchema: McpSchema | null
  annotations: {
    title?: string
    readOnlyHint?: boolean
    destructiveHint?: boolean
    idempotentHint?: boolean
    openWorldHint?: boolean
  }
}

export interface McpResource {
  name: string
  uri: string
  description: string
  mimeType: string
}

export interface AiGatewayDiscovery {
  tools: McpTool[]
  resources: McpResource[]
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

/** One JSON-RPC request and its response. A notification has no response. */
export interface McpExchange {
  method: string
  request: unknown
  status: number | null
  response: { result?: unknown; error?: { code?: number; message?: string } } | null
}

export interface McpCallOutcome {
  /** Set when the call failed before a JSON-RPC response. */
  error: string | null
  /** Every exchange in order. The last one is the call itself, if it got that far. */
  exchanges: McpExchange[]
}

export async function callMcpTool(
  name: string,
  args: Record<string, unknown>,
  signal: AbortSignal,
): Promise<McpCallOutcome> {
  const { data } = await apiClient.post<McpCallOutcome>(
    '/api/ai-gateways/mcp/tools/call',
    { name, arguments: args },
    { signal },
  )
  return data
}

export async function readMcpResource(uri: string, signal: AbortSignal): Promise<McpCallOutcome> {
  const { data } = await apiClient.post<McpCallOutcome>('/api/ai-gateways/mcp/resources/read', { uri }, { signal })
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
