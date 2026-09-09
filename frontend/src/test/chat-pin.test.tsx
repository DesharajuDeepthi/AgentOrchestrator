import {
  adoptDraftPin,
  chatBody,
  clearDraftPin,
  NO_PIN,
  pinFor,
  withPin,
  type ConvPin,
} from '../pages/ChatPage'

// spec §7.5 (M40) — the composer pin is per-conversation state: switching
// chats never carries a target (or its summary checkbox) across, a new
// conversation starts at Orchestrator (auto), and a ?target= deep link
// pins only the conversation it opens.

const pin = (patch: Partial<ConvPin>): ConvPin => ({ ...NO_PIN, ...patch })

describe('per-conversation pin map (spec §7.5, M40)', () => {
  it('pin set in chat A: B shows auto, back to A restores it', () => {
    let pins = withPin({}, 'conv-a', { targetId: 'agent-1' })
    expect(pinFor(pins, 'conv-a').targetId).toBe('agent-1')
    expect(pinFor(pins, 'conv-b')).toEqual(NO_PIN) // switching shows auto
    expect(pinFor(pins, null)).toEqual(NO_PIN) // new chat starts at auto
    pins = withPin(pins, 'conv-b', { targetId: 'agent-2' })
    expect(pinFor(pins, 'conv-a').targetId).toBe('agent-1') // A untouched
    expect(pinFor(pins, 'conv-b').targetId).toBe('agent-2')
  })

  it('summary checkbox is scoped to its conversation', () => {
    let pins = withPin({}, 'conv-a', { targetId: 'agent-1' })
    pins = withPin(pins, 'conv-a', { includeSummary: true })
    expect(pinFor(pins, 'conv-a')).toEqual({ targetId: 'agent-1', includeSummary: true })
    expect(pinFor(pins, 'conv-b').includeSummary).toBe(false)
    // patching one field keeps the other
    pins = withPin(pins, 'conv-a', { targetId: 'agent-9' })
    expect(pinFor(pins, 'conv-a').includeSummary).toBe(true)
  })

  it('deep-link draft pin travels to the conversation the first send creates', () => {
    // ?target= lands on the new-chat draft (convId null → key '')
    let pins = withPin({}, null, { targetId: 'agent-1' })
    expect(pinFor(pins, null).targetId).toBe('agent-1')
    pins = adoptDraftPin(pins, 'conv-new')
    expect(pinFor(pins, 'conv-new').targetId).toBe('agent-1')
    expect(pinFor(pins, null)).toEqual(NO_PIN) // draft consumed
  })

  it('adoptDraftPin is a no-op without a draft', () => {
    const pins = withPin({}, 'conv-a', { targetId: 'agent-1' })
    expect(adoptDraftPin(pins, 'conv-new')).toEqual(pins)
  })

  it('+ New conversation clears only the draft pin', () => {
    let pins = withPin({}, 'conv-a', { targetId: 'agent-1' })
    pins = withPin(pins, null, { targetId: 'agent-2' })
    pins = clearDraftPin(pins)
    expect(pinFor(pins, null)).toEqual(NO_PIN)
    expect(pinFor(pins, 'conv-a').targetId).toBe('agent-1')
    expect(clearDraftPin(pins)).toEqual(pins) // idempotent
  })
})

describe('chatBody (spec §7.5) — the pin rides only for its own chat', () => {
  it('carries the target and conversation id when pinned', () => {
    expect(chatBody('hi', 'conv-a', pin({ targetId: 'agent-1' }), false)).toEqual({
      message: 'hi',
      conversation_id: 'conv-a',
      target_sub_agent_id: 'agent-1',
    })
  })

  it('unpinned chat sends no target at all', () => {
    expect(chatBody('hi', 'conv-b', NO_PIN, true)).toEqual({
      message: 'hi',
      conversation_id: 'conv-b',
    })
  })

  it('include_history_summary needs pin AND checkbox AND history', () => {
    const p = pin({ targetId: 'agent-1', includeSummary: true })
    expect(chatBody('hi', 'conv-a', p, true)).toHaveProperty('include_history_summary', true)
    expect(chatBody('hi', 'conv-a', p, false)).not.toHaveProperty('include_history_summary')
    expect(
      chatBody('hi', 'conv-a', pin({ includeSummary: true }), true),
    ).not.toHaveProperty('include_history_summary')
  })

  it('new chat omits conversation_id but keeps the draft pin target', () => {
    expect(chatBody('hi', null, pin({ targetId: 'agent-1' }), false)).toEqual({
      message: 'hi',
      target_sub_agent_id: 'agent-1',
    })
  })
})
