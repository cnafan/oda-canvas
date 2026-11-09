import { t } from '@/i18n'
import React from 'react'
import SectionedPage, { PageSection } from '@/components/SectionedPage'
import AgentRegistry from './AgentRegistry'
import RegistrySources from './RegistrySources'
import SkillRegistry from './SkillRegistry'

const sections: PageSection[] = [
  {
    key: 'agents',
    title: t("Agents"),
    description: t("Registry of AI agents available to the Canvas."),
    content: <AgentRegistry />,
  },
  {
    key: 'skills',
    title: t("Skills"),
    description: t("Skills advertised by the agents in every configured registry."),
    content: <SkillRegistry />,
  },
]

export default function RegistriesPage() {
  return <SectionedPage header={<RegistrySources />} sections={sections} />
}
