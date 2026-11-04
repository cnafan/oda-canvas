import React from 'react'
import { ApiOutlined } from '@ant-design/icons'
import SectionedPage, { PageSection } from '@/components/SectionedPage'

const sections: PageSection[] = [
  { key: 'agent', title: 'Agent', description: 'Gateways fronting agent-to-agent (A2A) traffic.' },
  {
    key: 'model',
    title: 'Model',
    description: 'Gateways fronting AI model access.',
    link: { to: '/model-gateway-services', label: 'Open Model Gateway services' },
  },
  { key: 'tool', title: 'Tool', description: 'Gateways fronting tool (MCP) servers.' },
  { key: 'resource', title: 'Resource', description: 'Gateways fronting resources exposed to agents.' },
]

export default function GatewaysPage() {
  return (
    <SectionedPage
      title="Gateways"
      description="Agent, model, tool and resource gateways"
      icon={<ApiOutlined />}
      sections={sections}
    />
  )
}
