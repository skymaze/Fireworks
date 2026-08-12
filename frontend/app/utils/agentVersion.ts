export type AgentVersionRelation = 'older' | 'equal' | 'newer'
export type AgentDeployAction = 'upgrade' | 'downgrade' | 'reinstall'

export interface AgentVersionNode {
  agent_version?: string | null
  agent_required?: string | null
}

function versionSegments(version: string): number[] {
  const segments: number[] = []
  for (const part of version.trim().split('.')) {
    const digits = part.match(/^\d+/)?.[0]
    if (!digits) break
    segments.push(Number(digits))
  }
  return segments
}

/** Compare dotted numeric versions in the browser, ignoring suffixes such as -dev. */
export function compareAgentVersions(a: string, b: string): number {
  const left = versionSegments(a)
  const right = versionSegments(b)
  if (!left.length || !right.length) return a === b ? 0 : (a > b ? 1 : -1)
  const sharedLength = Math.min(left.length, right.length)
  for (let i = 0; i < sharedLength; i++) {
    if (left[i] !== right[i]) return left[i]! > right[i]! ? 1 : -1
  }
  return left.length === right.length ? 0 : (left.length > right.length ? 1 : -1)
}

export function agentVersionRelation(node: AgentVersionNode): AgentVersionRelation | null {
  if (!node.agent_version || !node.agent_required) return null
  const comparison = compareAgentVersions(node.agent_version, node.agent_required)
  if (comparison < 0) return 'older'
  if (comparison > 0) return 'newer'
  return 'equal'
}

export function agentVersionMismatch(node: AgentVersionNode): boolean {
  const relation = agentVersionRelation(node)
  return relation === 'older' || relation === 'newer'
}

/** Button is always available; only its version-control verb changes. */
export function agentDeployAction(node: AgentVersionNode): AgentDeployAction {
  const relation = agentVersionRelation(node)
  if (relation === 'older') return 'upgrade'
  if (relation === 'newer') return 'downgrade'
  return 'reinstall'
}

export function agentDeployLabelKey(action: AgentDeployAction): string {
  if (action === 'upgrade') return 'nodes.upgrade_agent'
  if (action === 'downgrade') return 'nodes.downgrade_agent'
  return 'nodes.reinstall_agent'
}
