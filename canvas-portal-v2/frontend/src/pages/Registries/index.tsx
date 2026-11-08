import React from 'react'
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

export default function RegistriesPage() {
  return <SectionedPage header={<RegistrySources />} sections={sections} />
}
