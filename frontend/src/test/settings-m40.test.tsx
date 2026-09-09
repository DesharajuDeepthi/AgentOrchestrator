import { render, screen } from '@testing-library/react'

// spec §8.7 (M40) — the Settings page gains Ambient, A2A, and API-guardrail
// sections; the Orchestrator section gains the overlap threshold and the
// agentic recursion limit. Master toggles gate their sections' knobs the
// way the Formatter section already does.

const SETTINGS: Record<string, unknown> = {
  orchestrator_mode: 'graph',
  formatter_enabled: false,
  registry_cache_mode: 'bypass',
  embedding_model: null,
  log_level: 'INFO',
  langsmith_endpoint: '',
  langsmith_project: '',
  otlp_endpoint: '',
  ambient_enabled: false,
  a2a_enabled: false,
  ambient_learning_mode: 'off',
  ambient_digest_times: ['09:00', '17:00'],
  ambient_quiet_hours: ['22:00', '07:00'],
  ambient_channels: {},
  ambient_pursuit: 'always',
  ambient_salience_mode: 'off',
  ambient_salience_min_urgency: 3,
  ambient_salience_learning: 'off',
  memory_admission_min_confidence: 0.5,
  memory_quarantine_kinds: [],
  memory_extraction_learning: 'off',
  ambient_tick_interval_s: 60,
  run_stall_after_s: 300,
  overlap_threshold_percent: 70,
  agentic_recursion_limit: 100,
  rate_limit_burst: 120,
  rate_limit_per_s: 10,
  a2a_card_refresh_interval_s: 300,
  a2a_task_timeout_s: 120,
  a2a_poll_interval_s: 60,
  a2a_max_parked: 20,
  a2a_http_timeout_s: 15,
  a2a_fence_max_chars: 8000,
}
let settings: Record<string, unknown> = SETTINGS
let patchError: Error | null = null

vi.mock('../api/hooks', () => ({
  useSettings: () => ({ data: settings }),
  useProviders: () => ({ data: [] }),
  useServers: () => ({ data: [] }),
  useHitlPending: () => ({ data: [] }),
  useCacheStatus: () => ({ data: null }),
  useRefreshCache: () => ({ mutate: vi.fn(), isPending: false }),
  usePatchSettings: () => ({ mutate: vi.fn(), error: patchError }),
  useInvalidate: () => vi.fn(),
}))

import { SettingsPage, channelRoutingOut, csvOut } from '../pages/SettingsPage'

describe('csvOut / channelRoutingOut (§18.4 routing serializers)', () => {
  it('splits, trims, and drops empties', () => {
    expect(csvOut(' 09:00, 17:00 ,')).toEqual(['09:00', '17:00'])
    expect(csvOut('')).toEqual([])
  })

  it('sets one mode without touching the others', () => {
    expect(channelRoutingOut({ digest: ['email'] }, 'interrupt', 'in_app, webhook')).toEqual({
      digest: ['email'],
      interrupt: ['in_app', 'webhook'],
    })
  })

  it('clearing a mode drops it — {} means in-app only', () => {
    expect(channelRoutingOut({ interrupt: ['email'] }, 'interrupt', ' ')).toEqual({})
  })
})

describe('SettingsPage M40 sections (spec §8.7)', () => {
  afterEach(() => {
    settings = SETTINGS
    patchError = null
  })

  it('renders the three new sections and the new Orchestrator knobs', () => {
    render(<SettingsPage />)
    expect(screen.getByText('Ambient (§17)')).toBeInTheDocument()
    expect(screen.getByText('A2A — remote agents (§19)')).toBeInTheDocument()
    expect(screen.getByText('API guardrails')).toBeInTheDocument()
    expect(screen.getByText('Overlap-guard threshold (%)')).toBeInTheDocument()
    expect(screen.getByText('Agentic recursion limit')).toBeInTheDocument()
    // guardrails are always visible (auth enforcement is env-gated)
    expect(screen.getByText('Rate-limit burst')).toBeInTheDocument()
  })

  it('masters off → section knobs hidden', () => {
    render(<SettingsPage />)
    expect(screen.queryByText('Tick interval (s)')).not.toBeInTheDocument()
    expect(screen.queryByText('Poll interval (s)')).not.toBeInTheDocument()
  })

  it('a rejected PATCH surfaces its 422 detail inline (§14e-42)', () => {
    settings = { ...SETTINGS, ambient_enabled: true }
    patchError = new Error('ambient_tick_interval_s must be an integer >= 15')
    render(<SettingsPage />)
    expect(
      screen.getAllByText('ambient_tick_interval_s must be an integer >= 15').length,
    ).toBeGreaterThan(0)
  })

  it('masters on → ambient and a2a knobs appear', () => {
    settings = { ...SETTINGS, ambient_enabled: true, a2a_enabled: true }
    render(<SettingsPage />)
    expect(screen.getByText('Tick interval (s)')).toBeInTheDocument()
    expect(screen.getByText('Stall reaper window (s)')).toBeInTheDocument()
    expect(screen.getByText('Delivery channels (§18.4)')).toBeInTheDocument()
    // M41: pursuit sits with the routing it modifies
    expect(screen.getByText('Pursuit (§17.5)')).toBeInTheDocument()
    // M42: salience sits with the delivery controls it re-judges
    expect(screen.getByText('Salience (§17.5)')).toBeInTheDocument()
    expect(screen.getByText(/lead the next digest, remember the fact, or drop it/)).toBeInTheDocument()
    expect(screen.getByText('Min urgency')).toBeInTheDocument()
    expect(screen.getByText(/only when the in-app toast reached nobody/)).toBeInTheDocument()
    expect(screen.getByText('Poll interval (s)')).toBeInTheDocument()
    expect(screen.getByText(/0 disables parking/)).toBeInTheDocument()
    expect(screen.getByText('Fence cap (chars)')).toBeInTheDocument()
  })
})
