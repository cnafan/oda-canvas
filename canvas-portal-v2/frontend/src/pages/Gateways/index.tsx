import React, { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Space, Spin, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ApiOutlined } from '@ant-design/icons'
import {
  fetchAiGateways,
  type AiGatewayAgent,
  type AiGatewayModel,
  type AiGatewaysSummary,
  type AiGatewayTool,
  type AiGatewayTrafficPolicy,
} from '@/api/aiGateways'
import SectionedPage, { PageSection } from '@/components/SectionedPage'
import { colors } from '@/theme'
import ModelTestModal from './ModelTestModal'

const renderMatches = (matches: string[]) => (
  <Space size={[4, 4]} wrap>
    {matches.map((match) => (
      <Tag key={match} style={{ fontFamily: 'monospace', marginInlineEnd: 0 }}>
        {match}
      </Tag>
    ))}
  </Space>
)

const modelColumns = (onTest: (model: string) => void): ColumnsType<AiGatewayModel> => [
  { title: 'Model', dataIndex: 'name' },
  { title: 'Provider', dataIndex: 'provider' },
  { title: 'Upstream', dataIndex: 'upstream' },
  { title: 'Paths', dataIndex: 'matches', render: renderMatches },
  {
    title: 'Status',
    dataIndex: 'status',
    render: (status: AiGatewayModel['status']) => (
      <Tag color={status === 'active' ? 'green' : 'red'}>{status}</Tag>
    ),
  },
  {
    title: 'Test',
    key: 'test',
    render: (_, model) => (
      <Button
        size="small"
        disabled={model.status !== 'active'}
        aria-label={`Test ${model.name}`}
        onClick={() => onTest(model.name)}
      >
        Test
      </Button>
    ),
  },
]

const toolColumns: ColumnsType<AiGatewayTool> = [
  { title: 'Target', dataIndex: 'name' },
  { title: 'Upstream', dataIndex: 'upstream' },
  { title: 'Paths', dataIndex: 'matches', render: renderMatches },
  { title: 'Sessions', dataIndex: 'stateful', render: (stateful: boolean) => (stateful ? 'Stateful' : 'Stateless') },
]

const agentColumns: ColumnsType<AiGatewayAgent> = [
  { title: 'Agent', dataIndex: 'name' },
  { title: 'Paths', dataIndex: 'matches', render: renderMatches },
  { title: 'Backend', dataIndex: 'backend' },
]

const trafficColumns: ColumnsType<AiGatewayTrafficPolicy> = [
  { title: 'Route', dataIndex: 'route' },
  { title: 'Paths', dataIndex: 'matches', render: renderMatches },
  { title: 'Policy', dataIndex: 'policy' },
  {
    title: 'Settings',
    dataIndex: 'settings',
    render: (settings: AiGatewayTrafficPolicy['settings']) =>
      Object.entries(settings)
        .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : String(value)}`)
        .join(' · '),
  },
]

function ConfigTable<T extends object>({
  rows,
  columns,
  rowKey,
  emptyText,
}: {
  rows: T[]
  columns: ColumnsType<T>
  rowKey: (row: T, index?: number) => string
  emptyText: string
}) {
  if (rows.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />
  }
  return <Table size="small" pagination={false} rowKey={rowKey} columns={columns} dataSource={rows} />
}

function LiveDiscovery({ data }: { data: AiGatewaysSummary }) {
  const headingStyle = { fontSize: 12, fontWeight: 600, color: colors.textSecondary, margin: '16px 0 8px' }
  if (!data.discovered) {
    return (
      <Alert
        style={{ marginTop: 16 }}
        type="warning"
        showIcon
        message="Live tools and resources are unavailable"
        description={data.discoveryError ?? undefined}
      />
    )
  }
  const { tools, resources, partial } = data.discovered
  return (
    <>
      <h4 style={headingStyle}>Tools available now</h4>
      <ConfigTable
        rows={tools}
        rowKey={(tool) => tool.name}
        emptyText="No tools available"
        columns={[
          { title: 'Tool', dataIndex: 'name' },
          { title: 'Description', dataIndex: 'description' },
        ]}
      />
      <h4 style={headingStyle}>Resources available now</h4>
      <ConfigTable
        rows={resources}
        rowKey={(resource) => resource.uri}
        emptyText="No resources available"
        columns={[
          { title: 'Resource', dataIndex: 'name' },
          { title: 'URI', dataIndex: 'uri' },
          { title: 'Description', dataIndex: 'description' },
        ]}
      />
      {partial && (
        <p style={{ fontSize: 12, color: colors.textMuted, margin: '8px 0 0' }}>
          Showing the first page only.
        </p>
      )}
    </>
  )
}

function buildSections(data: AiGatewaysSummary | undefined, onTestModel: (model: string) => void): PageSection[] {
  const ok = data?.state === 'ok'
  return [
    {
      key: 'models',
      title: 'Models',
      description: 'LLM consumption: the models that clients can call through the gateway.',
      link: { to: '/model-gateway-services', label: 'Open Model Gateway services' },
      content: ok ? (
        <ConfigTable
          rows={data.models ?? []}
          columns={modelColumns(onTestModel)}
          rowKey={(model) => `${model.name}/${model.upstream}`}
          emptyText="No models are configured"
        />
      ) : undefined,
    },
    {
      key: 'tools-resources',
      title: 'Tools & Resources',
      description: 'MCP connectivity: the configured MCP targets, and what they serve through the gateway right now.',
      content: ok ? (
        <>
          <ConfigTable
            rows={data.tools ?? []}
            columns={toolColumns}
            rowKey={(tool) => tool.name}
            emptyText="No MCP targets are configured"
          />
          <LiveDiscovery data={data} />
        </>
      ) : undefined,
    },
    {
      key: 'agents',
      title: 'Agents',
      description: 'Agent connectivity: the routes that carry agent-to-agent (A2A) traffic.',
      content: ok ? (
        <ConfigTable
          rows={data.agents ?? []}
          columns={agentColumns}
          rowKey={(agent) => `${agent.name}/${agent.backend}`}
          emptyText="No agent routes are configured"
        />
      ) : undefined,
    },
    {
      key: 'traffic',
      title: 'Traffic Management',
      description: 'The timeout, retry, rate limit and other policies applied to each route.',
      content: ok ? (
        <ConfigTable
          rows={data.traffic ?? []}
          columns={trafficColumns}
          rowKey={(row, index) => `${row.route}/${row.policy}/${index}`}
          emptyText="No traffic policies are configured"
        />
      ) : undefined,
    },
  ]
}

const STATE_TITLES: Record<Exclude<AiGatewaysSummary['state'], 'ok'>, string> = {
  not_deployed: 'agentgateway is not deployed',
  unauthorized: 'The portal is not authorized to read the agentgateway configuration',
  unreachable: 'agentgateway is unreachable',
}

function Banner({ data, isLoading, isError }: { data?: AiGatewaysSummary; isLoading: boolean; isError: boolean }) {
  if (isLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 24 }}>
        <Spin />
      </div>
    )
  }
  if (isError || !data) {
    return <Alert type="error" showIcon message="Couldn't load the AI gateway configuration" />
  }
  if (data.state !== 'ok') {
    return <Alert type="warning" showIcon message={STATE_TITLES[data.state]} description={data.message} />
  }
  const warnings = data.warnings ?? []
  const reloadFailed = data.runtime?.synchronized === false
  if (!reloadFailed && warnings.length === 0) return null
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {reloadFailed && (
        <Alert
          type="warning"
          showIcon
          message="The latest configuration was not applied, so this page shows the configuration still running"
          description="Check the agentgateway logs for the error."
        />
      )}
      {warnings.map((warning) => (
        <Alert key={warning} type="warning" showIcon message={warning} />
      ))}
    </Space>
  )
}

export default function GatewaysPage() {
  const [testedModel, setTestedModel] = useState<string | null>(null)
  const { data, isLoading, isError } = useQuery({
    queryKey: ['ai-gateways'],
    queryFn: fetchAiGateways,
    refetchInterval: 30_000,
    retry: 1,
  })

  return (
    <>
      <SectionedPage
        title="AI Gateways"
        description={data?.version ? `Active configuration of agentgateway ${data.version}` : 'Active configuration of agentgateway'}
        icon={<ApiOutlined />}
        hideWipBadge
        banner={<Banner data={data} isLoading={isLoading} isError={isError} />}
        sections={buildSections(data, setTestedModel)}
      />
      {testedModel && <ModelTestModal model={testedModel} onClose={() => setTestedModel(null)} />}
    </>
  )
}
