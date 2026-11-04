import React from 'react'
import { DatabaseOutlined } from '@ant-design/icons'
import SectionedPage, { PageSection } from '@/components/SectionedPage'

const sections: PageSection[] = [
  { key: 'agents', title: 'Agents', description: 'Registry of AI agents available to the Canvas.' },
  { key: 'skills', title: 'Skills', description: 'Registry of reusable skills that agents can use.' },
]

export default function RegistriesPage() {
  return (
    <SectionedPage
      title="Registries"
      description="Catalogs of agents and skills"
      icon={<DatabaseOutlined />}
      sections={sections}
    />
  )
}
