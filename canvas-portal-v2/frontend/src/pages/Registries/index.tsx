import React from 'react'
import { DatabaseOutlined } from '@ant-design/icons'
import { useRegistrySources } from '@/api/registries'
import SectionedPage, { PageSection } from '@/components/SectionedPage'
import AgentRegistry from './AgentRegistry'
import RegistrySources from './RegistrySources'
import SkillRegistry from './SkillRegistry'

const sections: PageSection[] = [
  {
    key: 'agents',
    title: 'Agents',
    description: 'Registry of AI agents available to the Canvas.',
    content: <AgentRegistry />,
  },
  {
    key: 'skills',
    title: 'Skills',
    description: 'Skills advertised by the agents in every configured registry.',
    content: <SkillRegistry />,
  },
]

const sourcesSection: PageSection = {
  key: 'sources',
  title: 'Manage Registries',
  description: 'Manually configure agent and skill registries',
  content: <RegistrySources />,
}

export default function RegistriesPage() {
  const { data } = useRegistrySources()

  return (
    <SectionedPage
      title="Registries"
      description="Catalogs of agents and skills"
      icon={<DatabaseOutlined />}
      sections={data?.canManage ? [...sections, sourcesSection] : sections}
    />
  )
}
