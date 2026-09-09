import { act, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { AmbientToaster } from '../components/AmbientToaster'

const settingsState: { ambient_enabled: boolean } = { ambient_enabled: true }
vi.mock('../api/hooks', () => ({
  useSettings: () => ({ data: settingsState }),
}))

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((e: MessageEvent) => void) | null = null
  closed = false
  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
}

describe('AmbientToaster (spec §18.4)', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    settingsState.ambient_enabled = true
  })
  afterEach(() => vi.unstubAllGlobals())

  it('subscribes to the ambient stream and toasts tier-0 events', () => {
    render(<AmbientToaster />)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].url).toBe('/api/v1/ambient/stream')
    act(() =>
      FakeEventSource.instances[0].emit({
        id: 'd1',
        mode: 'interrupt',
        tier: 0,
        urgency: 5,
        category: 'ops',
        title: 'queue on fire',
        at: 't1',
      }),
    )
    expect(screen.getByText('queue on fire')).toBeInTheDocument()
    expect(screen.getByText(/ambient interrupt/)).toBeInTheDocument()
  })

  it('never toasts digest-tier events', () => {
    render(<AmbientToaster />)
    act(() =>
      FakeEventSource.instances[0].emit({
        id: 'd2',
        mode: 'digest',
        tier: 2,
        urgency: 2,
        category: 'ops',
        title: 'digest item',
        at: 't2',
      }),
    )
    expect(screen.queryByText('digest item')).not.toBeInTheDocument()
  })

  it('does not subscribe at all while ambient is dark', () => {
    settingsState.ambient_enabled = false
    render(<AmbientToaster />)
    expect(FakeEventSource.instances).toHaveLength(0)
  })
})
