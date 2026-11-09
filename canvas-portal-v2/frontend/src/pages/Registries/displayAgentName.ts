const AGENT_DISPLAY_NAMES: Record<string, string> = {
  crmagent: 'CRM Agent',
  mockagentlivestreamstub: 'Live Streaming Agent',
  livestreamingagent: 'Live Streaming Agent',
  mockagentserviceassurancestub: 'Assurance Agent',
  serviceassuranceagent: 'Assurance Agent',
  assuranceagent: 'Assurance Agent',
  mockagentwirelessdomainstub: 'RAN Agent',
  wirelessdomainagent: 'RAN Agent',
  ranagent: 'RAN Agent',
}

/** Keep user-facing Agent names consistent across legacy and canonical registry keys. */
export function displayAgentName(name: string, _organization?: string): string {
  const key = name.trim().toLowerCase().replace(/[^a-z0-9]/g, '')
  return AGENT_DISPLAY_NAMES[key] ?? name
}
