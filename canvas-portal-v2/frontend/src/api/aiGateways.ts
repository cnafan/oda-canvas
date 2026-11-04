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

export type ModelProvider = string | { custom: Record<string, unknown> } | { reference: string }

export interface ModelParams {
  model?: string
  baseUrl?: string
  awsRegion?: string
  bedrockEndpointPreference?: string
  vertexRegion?: string
  vertexProject?: string
  azureResourceName?: string
  azureResourceType?: string
  azureApiVersion?: string
  azureProjectName?: string
  tokenize?: boolean
}

/** A model as it is in the gateway's config. API keys are never included. */
export interface ModelConfig {
  name: string
  provider: ModelProvider
  visibility: string | null
  passthrough: string | null
  params: ModelParams
  apiKeySet: boolean
  advanced: Record<string, unknown>
  revision: string
}

export interface ModelConfigs {
  configurable: boolean
  models: ModelConfig[]
}

export interface ModelConfigInput {
  name: string
  provider: ModelProvider
  visibility: string | null
  passthrough: string | null
  params: ModelParams
  apiKey: string | null
  removeApiKey: boolean
  advanced: Record<string, unknown>
}

export interface ModelChangeResult {
  applied: boolean
  message: string
}

export async function fetchModelConfigs(): Promise<ModelConfigs> {
  const { data } = await apiClient.get<ModelConfigs>('/api/ai-gateways/models/config')
  return data
}

export async function addModelConfig(input: ModelConfigInput): Promise<ModelChangeResult> {
  const { data } = await apiClient.post<ModelChangeResult>('/api/ai-gateways/models/config', input)
  return data
}

export async function updateModelConfig(
  input: ModelConfigInput,
  originalName: string,
  revision: string,
): Promise<ModelChangeResult> {
  const { data } = await apiClient.put<ModelChangeResult>('/api/ai-gateways/models/config', {
    ...input,
    originalName,
    revision,
  })
  return data
}

export async function removeModelConfig(name: string): Promise<ModelChangeResult> {
  const { data } = await apiClient.delete<ModelChangeResult>('/api/ai-gateways/models/config', { params: { name } })
  return data
}

export interface ChangeResult extends ModelChangeResult {
  warning?: string
}

/** An MCP server as it is in the gateway's config. Auth tokens are never included. */
export interface McpTarget {
  name: string
  transport: 'mcp' | 'sse' | 'openapi' | 'stdio'
  host: string | null
  port: number | null
  path: string | null
  backend: string | null
  schema: string | null
  /** The schema isn't inline, so the form can't show it. It stays if none is entered. */
  schemaKept: boolean
  command: string | null
  condition: string | null
  tokenSet: boolean
  policies: Record<string, unknown>
  revision: string
}

export interface McpTargets {
  configurable: boolean
  targets: McpTarget[]
}

export interface McpTargetInput {
  name: string
  transport: McpTarget['transport']
  host: string | null
  port: number | null
  path: string | null
  schema: string | null
  condition: string | null
  authToken: string | null
  removeAuthToken: boolean
  policies: Record<string, unknown>
}

export async function fetchMcpTargets(): Promise<McpTargets> {
  const { data } = await apiClient.get<McpTargets>('/api/ai-gateways/mcp/targets')
  return data
}

export async function addMcpTarget(input: McpTargetInput): Promise<ChangeResult> {
  const { data } = await apiClient.post<ChangeResult>('/api/ai-gateways/mcp/targets', input)
  return data
}

export async function updateMcpTarget(
  input: McpTargetInput,
  originalName: string,
  revision: string,
): Promise<ChangeResult> {
  const { data } = await apiClient.put<ChangeResult>('/api/ai-gateways/mcp/targets', { ...input, originalName, revision })
  return data
}

export async function removeMcpTarget(name: string): Promise<ChangeResult> {
  const { data } = await apiClient.delete<ChangeResult>('/api/ai-gateways/mcp/targets', { params: { name } })
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
