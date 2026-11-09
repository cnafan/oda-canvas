import { t } from '@/i18n'
import React from 'react'
import { Form, Input, InputNumber, Select, Switch } from 'antd'
import type { McpSchema } from '@/api/aiGateways'
import { colors } from '@/theme'

type FieldKind = 'string' | 'number' | 'integer' | 'choice' | 'json'

interface ArgumentsDraft {
  /** Field values. JSON fields hold their text, and empty fields are left out or undefined. */
  values: Record<string, unknown>
  /** Set while the whole arguments object is edited as JSON. */
  jsonText: string | null
}

/** Either the arguments to send, or why they can't be sent yet. */
type BuiltArguments = { args: Record<string, unknown>; problem?: undefined } | { args?: undefined; problem: string }

const BOOLEAN_CHOICES = [true, false]

/** Optional fields from Python tools are often `anyOf: [T, null]`, which still suits a plain input. */
function withoutNull(schema: McpSchema): McpSchema {
  const options = (schema.anyOf ?? schema.oneOf ?? []).filter((option) => option.type !== 'null')
  return options.length === 1 ? { ...options[0], ...schema, anyOf: undefined, oneOf: undefined } : schema
}

function fieldKind(rawSchema: McpSchema): FieldKind {
  const schema = withoutNull(rawSchema)
  if (schema.enum?.length) return 'choice'
  const types = (Array.isArray(schema.type) ? schema.type : [schema.type]).filter((type) => type !== 'null')
  if (types.length !== 1) return 'json'
  if (types[0] === 'boolean') return 'choice'
  return types[0] === 'string' || types[0] === 'number' || types[0] === 'integer' ? types[0] : 'json'
}

function choicesFor(rawSchema: McpSchema): unknown[] {
  return withoutNull(rawSchema).enum ?? BOOLEAN_CHOICES
}

function fieldsOf(schema: McpSchema | null): [string, McpSchema][] {
  return Object.entries(schema?.properties ?? {})
}

/** Can a form show every part of this schema? Without properties, only raw JSON can be accurate. */
function hasForm(schema: McpSchema | null): boolean {
  return schema !== null && (fieldsOf(schema).length > 0 || schema.type === 'object')
}

function valueToField(schema: McpSchema, value: unknown): unknown {
  return fieldKind(schema) === 'json' && value !== undefined ? JSON.stringify(value, null, 2) : value
}

export function initialDraft(schema: McpSchema | null): ArgumentsDraft {
  const values: Record<string, unknown> = {}
  for (const [name, field] of fieldsOf(schema)) {
    const fallback = withoutNull(field).default
    if (fallback !== undefined && fallback !== null) values[name] = valueToField(field, fallback)
  }
  return { values, jsonText: hasForm(schema) ? null : '{}' }
}

function isEmpty(value: unknown): boolean {
  return value === undefined || value === null || value === ''
}

function parseObject(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text)
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

export function buildArguments(schema: McpSchema | null, draft: ArgumentsDraft): BuiltArguments {
  if (draft.jsonText !== null) {
    const args = parseObject(draft.jsonText)
    return args ? { args } : { problem: 'Arguments must be a valid JSON object.' }
  }
  const required = schema?.required ?? []
  const args: Record<string, unknown> = {}
  for (const [name, field] of fieldsOf(schema)) {
    const value = draft.values[name]
    if (isEmpty(value)) {
      if (required.includes(name)) return { problem: `Enter a value for ${name}.` }
      continue
    }
    if (fieldKind(field) !== 'json') {
      args[name] = value
      continue
    }
    try {
      args[name] = JSON.parse(String(value))
    } catch {
      return { problem: `${name} must be valid JSON.` }
    }
  }
  return { args }
}

/** What's been entered so far, even if it can't be sent yet. Invalid JSON fields stay as their text. */
function enteredValues(schema: McpSchema | null, draft: ArgumentsDraft): Record<string, unknown> {
  const entered: Record<string, unknown> = {}
  for (const [name, field] of fieldsOf(schema)) {
    const value = draft.values[name]
    if (isEmpty(value)) continue
    entered[name] = value
    if (fieldKind(field) === 'json') {
      try {
        entered[name] = JSON.parse(String(value))
      } catch {
        // Keep the text, so that nothing typed is lost.
      }
    }
  }
  return entered
}

/** Switch between the form and raw JSON, keeping what's been entered. */
function toggleJsonMode(schema: McpSchema | null, draft: ArgumentsDraft, useJson: boolean): ArgumentsDraft {
  if (useJson) return { ...draft, jsonText: JSON.stringify(enteredValues(schema, draft), null, 2) }
  const parsed = parseObject(draft.jsonText ?? '')
  if (!parsed) return { ...draft, jsonText: null }
  const values: Record<string, unknown> = {}
  for (const [name, field] of fieldsOf(schema)) values[name] = valueToField(field, parsed[name])
  return { values, jsonText: null }
}

function ArgumentField({
  schema,
  value,
  onChange,
  disabled,
  label,
}: {
  schema: McpSchema
  value: unknown
  onChange: (value: unknown) => void
  disabled: boolean
  label: string
}) {
  const kind = fieldKind(schema)
  if (kind === 'choice') {
    return (
      <Select
        allowClear
        aria-label={label}
        disabled={disabled}
        placeholder={t("Not set")}
        // Options can be any JSON value, so select them by their JSON text.
        value={isEmpty(value) ? undefined : JSON.stringify(value)}
        onChange={(text?: string) => onChange(text === undefined ? undefined : JSON.parse(text))}
        options={choicesFor(schema).map((choice) => ({ value: JSON.stringify(choice), label: String(choice) }))}
      />
    )
  }
  if (kind === 'number' || kind === 'integer') {
    const { minimum, maximum } = withoutNull(schema)
    return (
      <InputNumber
        aria-label={label}
        disabled={disabled}
        style={{ width: '100%' }}
        min={minimum}
        max={maximum}
        precision={kind === 'integer' ? 0 : undefined}
        value={isEmpty(value) ? null : (value as number)}
        onChange={(next) => onChange(next ?? undefined)}
      />
    )
  }
  if (kind === 'string') {
    return (
      <Input
        aria-label={label}
        disabled={disabled}
        value={String(value ?? '')}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  return (
    <Input.TextArea
      aria-label={label}
      disabled={disabled}
      autoSize={{ minRows: 2, maxRows: 8 }}
      spellCheck={false}
      style={{ fontFamily: 'monospace', fontSize: 13 }}
      placeholder="JSON value"
      value={String(value ?? '')}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

export default function McpArgumentsForm({
  schema,
  draft,
  onChange,
  disabled,
}: {
  schema: McpSchema | null
  draft: ArgumentsDraft
  onChange: (draft: ArgumentsDraft) => void
  disabled: boolean
}) {
  const required = schema?.required ?? []
  const fields = fieldsOf(schema)
  const formAvailable = hasForm(schema)

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: colors.textSecondary }}>{t("Arguments")}</span>
        {formAvailable ? (
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color: colors.textSecondary }}>
            Edit as JSON
            <Switch
              size="small"
              disabled={disabled}
              checked={draft.jsonText !== null}
              onChange={(useJson) => onChange(toggleJsonMode(schema, draft, useJson))}
            />
          </label>
        ) : (
          <span style={{ fontSize: 12, color: colors.textMuted }}>This tool's schema can't be shown as a form.</span>
        )}
      </div>
      {draft.jsonText !== null ? (
        <Input.TextArea
          aria-label="Arguments as JSON"
          disabled={disabled}
          autoSize={{ minRows: 4, maxRows: 12 }}
          spellCheck={false}
          style={{ fontFamily: 'monospace', fontSize: 13 }}
          value={draft.jsonText}
          onChange={(event) => onChange({ ...draft, jsonText: event.target.value })}
        />
      ) : fields.length === 0 ? (
        <div style={{ fontSize: 12, color: colors.textMuted }}>This tool takes no arguments.</div>
      ) : (
        <Form layout="vertical" component="div" size="small">
          {fields.map(([name, field]) => (
            <Form.Item
              key={name}
              required={required.includes(name)}
              label={withoutNull(field).title ?? name}
              help={withoutNull(field).description}
              style={{ marginBottom: 10 }}
            >
              <ArgumentField
                schema={field}
                label={name}
                disabled={disabled}
                value={draft.values[name]}
                onChange={(value) => onChange({ ...draft, values: { ...draft.values, [name]: value } })}
              />
            </Form.Item>
          ))}
        </Form>
      )}
    </div>
  )
}
