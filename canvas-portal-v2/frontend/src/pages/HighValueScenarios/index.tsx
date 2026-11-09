import { t } from '@/i18n'
import React from 'react'
import { ThunderboltOutlined } from '@ant-design/icons'
import SectionedPage, { PageSection } from '@/components/SectionedPage'

const sections: PageSection[] = [
  { key: 'autonomous-flows', title: t("Autonomous Flows"), description: 'End-to-end flows executed autonomously by agents.' },
  { key: 'solution-packages', title: t("Solution Packages"), description: 'Packaged solutions bundling components, agents and flows.' },
]

export default function HighValueScenariosPage() {
  return (
    <SectionedPage
      title={t("High Value Scenarios")}
      description="Autonomous flows and solution packages"
      icon={<ThunderboltOutlined />}
      sections={sections}
    />
  )
}
