import React from 'react'
import BrainOutlined from '@/components/BrainOutlined'
import SectionedPage, { PageSection } from '@/components/SectionedPage'

const sections: PageSection[] = [
  { key: 'knowledge-base', title: 'Knowledge-Base', description: 'Curated knowledge sources that agents retrieve from and reason over.' },
  { key: 'federated-ontology', title: 'Federated Ontology', description: 'Shared ontology federated across components, agents and domains.' },
]

export default function KnowledgePage() {
  return (
    <SectionedPage
      title="Knowledge"
      description="Knowledge-base and federated ontology"
      icon={<BrainOutlined />}
      sections={sections}
    />
  )
}
