import React, { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Modal, Popconfirm, Radio, Space, Tag, Typography, message } from 'antd'
import apiClient from '@/api/client'
import { useAuth } from '@/hooks/useAuth'
import { t } from '@/i18n'

type RegistryStatus = { name: string; organization: string; status: string }
type Layer = 'service' | 'network' | 'unknown'

function detail(error: unknown): string {
  const value = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof value === 'string' ? value : t('Registry status could not be loaded. Please retry.')
}

export default function AgentRegistration({ namespace, name }: { namespace: string; name: string }) {
  const { isAdmin } = useAuth()
  const queryClient = useQueryClient()
  const [registerOpen, setRegisterOpen] = useState(false)
  const [layer, setLayer] = useState<Layer | null>(null)
  const path = `/api/components/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
  const key = ['component-registry-status', namespace, name]
  const status = useQuery({
    queryKey: key,
    queryFn: async () => (await apiClient.get<RegistryStatus>(`${path}/registry-status`)).data,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const operation = useMutation({
    mutationFn: async (kind: 'register' | 'deregister') => {
      if (!status.data) throw new Error('Missing Agent Card identity')
      const identity = { name: status.data.name, organization: status.data.organization }
      return apiClient.post(`${path}/${kind}`, kind === 'register' ? { ...identity, layer } : identity)
    },
    onSuccess: async (_, kind) => {
      setRegisterOpen(false)
      setLayer(null)
      message.success(t(kind === 'register' ? 'Registration submitted' : 'Agent deregistered'))
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: key }),
        queryClient.invalidateQueries({ queryKey: ['registry-agents'] }),
      ])
    },
    onError: (error) => message.error(detail(error)),
  })
  const labels: Record<string, string> = {
    unregistered: t('Unregistered'), registered: t('Pending Approval'), published: t('Published'),
    rejected: t('Rejected'), deprecated: t('Deprecated'), unknown: t('Unknown'),
  }
  const isRegistered = status.data && ['registered', 'published', 'rejected', 'deprecated'].includes(status.data.status)

  return (
    <Card size="small" title={t('Agent registration')} style={{ marginTop: 16 }}
      extra={<Button size="small" onClick={() => status.refetch()} loading={status.isFetching}>{t('Refresh')}</Button>}>
      {status.isError ? (
        <Alert type="error" showIcon message={t('Registry status could not be loaded. Please retry.')} description={detail(status.error)} />
      ) : status.data ? (
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div><Typography.Text type="secondary">{t('Agent Card')}: </Typography.Text>{status.data.name} · {status.data.organization}</div>
          <div><Typography.Text type="secondary">{t('Registry status')}: </Typography.Text>
            <Tag color={status.data.status === 'published' ? 'green' : status.data.status === 'registered' ? 'orange' : 'default'}>
              {labels[status.data.status] ?? status.data.status}
            </Tag>
          </div>
          {isAdmin && status.data.status === 'unregistered' && <Button type="primary" onClick={() => setRegisterOpen(true)}>{t('Register agent')}</Button>}
          {isAdmin && isRegistered && <Popconfirm title={t('Deregister this agent?')}
            description={t('This removes the Agent Card from the in-cluster registry.')}
            onConfirm={() => operation.mutate('deregister')} okText={t('Deregister')} cancelText={t('Cancel')}>
            <Button danger loading={operation.isPending}>{t('Deregister')}</Button>
          </Popconfirm>}
        </Space>
      ) : <Typography.Text type="secondary">{t('Loading registry status…')}</Typography.Text>}
      <Modal title={t('Register agent')} open={registerOpen} onCancel={() => setRegisterOpen(false)}
        onOk={() => operation.mutate('register')} okText={t('Register')} cancelText={t('Cancel')}
        okButtonProps={{ disabled: !layer, loading: operation.isPending }}>
        <p>{t('Choose the agent layer. Registration may require approval before publication.')}</p>
        <Radio.Group value={layer} onChange={(event) => setLayer(event.target.value)}>
          <Radio value="service">{t('Service layer')}</Radio>
          <Radio value="network">{t('Network layer')}</Radio>
          <Radio value="unknown">{t('Unknown')}</Radio>
        </Radio.Group>
      </Modal>
    </Card>
  )
}
