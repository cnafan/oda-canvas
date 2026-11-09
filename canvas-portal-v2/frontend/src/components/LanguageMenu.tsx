import React from 'react'
import { Dropdown } from 'antd'
import { CaretDownOutlined, GlobalOutlined } from '@ant-design/icons'
import { currentLanguage, languages, setLanguage, type Language } from '@/i18n'
import { colors } from '@/theme'

export default function LanguageMenu() {
  const selected = currentLanguage()
  const current = languages.find((language) => language.code === selected)!

  return (
    <Dropdown
      trigger={['click']}
      placement="bottomRight"
      menu={{
        selectable: true,
        selectedKeys: [selected],
        items: languages.map((language) => ({ key: language.code, label: language.label })),
        onClick: ({ key }) => setLanguage(key as Language),
      }}
    >
      <button
        type="button"
        aria-label={selected === 'zh' ? '选择语言' : 'Select language'}
        aria-haspopup="menu"
        style={{
          display: 'flex', alignItems: 'center', gap: 8, height: 40, padding: '0 12px',
          border: `1px solid ${colors.border}`, borderRadius: 8, background: colors.bgCard,
          color: colors.textPrimary, cursor: 'pointer', font: 'inherit', fontSize: 13,
        }}
        onMouseEnter={(event) => { event.currentTarget.style.background = colors.hoverSurface }}
        onMouseLeave={(event) => { event.currentTarget.style.background = colors.bgCard }}
      >
        <GlobalOutlined style={{ fontSize: 16, color: colors.textSecondary }} />
        <span>{current.label}</span>
        <CaretDownOutlined style={{ fontSize: 10, color: colors.textMuted }} />
      </button>
    </Dropdown>
  )
}
