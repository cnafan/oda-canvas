import { t } from '@/i18n'
import React from 'react'
import { Link } from 'react-router-dom'
import { colors } from '@/theme'

export interface PageSection {
  key: string
  title: string
  description: string
  /** Optional link to an existing page that already covers this section. */
  link?: { to: string; label: string }
  /** Replaces the placeholder. The link, if any, is shown underneath. */
  content?: React.ReactNode
  /** Shown at the end of the section's title line. */
  actions?: React.ReactNode
}

type SectionedPageProps = {
  sections: PageSection[]
  hideWipBadge?: boolean
  /** Shown between the page header and the sections. */
  banner?: React.ReactNode
} & (
  | { title: string; description: string; icon: React.ReactNode; header?: never }
  /** A custom header shown in place of the default title row. */
  | { header: React.ReactNode; title?: never; description?: never; icon?: never }
)

function WipBadge() {
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        background: '#fef3c7',
        border: '1px solid #fcd34d',
        borderRadius: 999,
        padding: '6px 14px',
        fontSize: 12,
        fontWeight: 600,
        color: '#92400e',
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#f59e0b' }} />

      {t("Work in progress")}
    </div>
  )
}

/** Page header followed by one stacked card per section. */
export default function SectionedPage({ title, description, icon, header, sections, hideWipBadge, banner }: SectionedPageProps) {
  return (
    // A custom header is a full-width card like the other inventory pages, so it needs no extra inset.
    <div style={{ padding: header ? 0 : 24 }}>
      {header ? (
        <div style={{ marginBottom: 16 }}>{header}</div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 8,
                background: colors.primaryGradient,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 18,
                color: '#fff',
              }}
            >
              {icon}
            </div>
            <div>
              <h1 style={{ fontSize: 20, fontWeight: 700, color: colors.textPrimary, margin: 0 }}>{title}</h1>
              <p style={{ fontSize: 12, color: colors.textMuted, margin: '2px 0 0' }}>{description}</p>
            </div>
          </div>
          {!hideWipBadge && <WipBadge />}
        </div>
      )}

      {banner && <div style={{ marginBottom: 16 }}>{banner}</div>}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {sections.map((section) => (
          <section
            key={section.key}
            style={{
              background: colors.bgCard,
              border: `1px solid ${colors.border}`,
              borderRadius: 12,
              padding: 24,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, margin: '0 0 6px' }}>
              <h3 style={{ fontSize: 14, fontWeight: 650, color: colors.textPrimary, margin: 0 }}>{section.title}</h3>
              {section.actions}
            </div>
            <p style={{ fontSize: 12, color: colors.textSecondary, margin: '0 0 14px', lineHeight: 1.55 }}>
              {section.description}
            </p>
            {section.content ? (
              <>
                {section.content}
                {section.link && (
                  <div style={{ fontSize: 12, marginTop: 12 }}>
                    <Link to={section.link.to}>{section.link.label}</Link>
                  </div>
                )}
              </>
            ) : (
              <div
                style={{
                  border: `1px dashed ${colors.border}`,
                  borderRadius: 10,
                  padding: 14,
                  background: colors.surfaceSubtle,
                  fontSize: 12,
                  color: colors.textMuted,
                }}
              >
                Nothing to show yet.
                {section.link && (
                  <>
                    {' '}
                    <Link to={section.link.to}>{section.link.label}</Link>
                  </>
                )}
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  )
}
