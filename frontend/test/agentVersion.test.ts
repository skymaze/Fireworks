import assert from 'node:assert/strict'
import test from 'node:test'

import { agentDeployAction, compareAgentVersions } from '../app/utils/agentVersion.ts'

test('compares dotted agent versions numerically and ignores suffixes', () => {
  assert.equal(compareAgentVersions('0.2.0', '0.2.0'), 0)
  assert.equal(compareAgentVersions('0.10.0', '0.2.0'), 1)
  assert.equal(compareAgentVersions('0.1.9', '0.2.0'), -1)
  assert.equal(compareAgentVersions('0.2.0-dev', '0.2.0'), 0)
})

test('always selects upgrade, downgrade, or reinstall from the two versions', () => {
  assert.equal(agentDeployAction({ agent_version: '0.1.0', agent_required: '0.2.0' }), 'upgrade')
  assert.equal(agentDeployAction({ agent_version: '0.3.0', agent_required: '0.2.0' }), 'downgrade')
  assert.equal(agentDeployAction({ agent_version: '0.2.0', agent_required: '0.2.0' }), 'reinstall')
  assert.equal(agentDeployAction({ agent_version: null, agent_required: '0.2.0' }), 'reinstall')
})
