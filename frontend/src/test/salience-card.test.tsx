import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// spec §8.9 (M43) — the salience decision surface on the delivery card:
// consequence-first headline, Do it / Leave it on a proposal, Undo on an
// applied verdict, layered "why this?" disclosure, and honest refusals
// (a 409 detail rendered, never swallowed).

const post = vi.fn()

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    detail: string
    constructor(status: number, detail: string) {
      super(detail)
      this.status = status
      this.detail = detail
    }
  }
  return {
    api: {
      post: (...args: unknown[]) => post(...args),
      get: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
    ApiError,
    getToken: () => null,
    setToken: () => {},
    sseUrl: (p: string) => p,
  }
})

import { SalienceBlock, type DeliveryRow } from '../pages/AmbientPage'

const row = (salience: DeliveryRow['salience']): DeliveryRow => ({
  id: 'd-1',
  run_id: null,
  category: 'ops',
  tier: 0,
  urgency: 5,
  title: 'Payment webhook failing',
  body: 'signature mismatch',
  channel: 'interrupt',
  delivered_at: '2026-08-30T00:00:00Z',
  superseded_by: null,
  feedback: null,
  seen_at: null,
  salience,
  reward: null,
  created_at: '2026-08-30T00:00:00Z',
})

const proposal = (verdict: string): DeliveryRow['salience'] => ({
  verdict,
  reason: 'revenue-impacting and unhandled',
  confidence: 0.9,
  applied: false,
  mode: 'propose',
  decision: null,
})

afterEach(() => post.mockReset())

describe('SalienceBlock (spec §8.9 — M43)', () => {
  it('renders nothing without a verdict', () => {
    const { container } = render(<SalienceBlock row={row(null)} onDone={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('leads with the consequence, not the mechanism', () => {
    render(<SalienceBlock row={row(proposal('escalate'))} onDone={vi.fn()} />)
    expect(screen.getByText('Worth your attention')).toBeInTheDocument()
    expect(screen.getByText('Lead the next digest with this.')).toBeInTheDocument()
    // the mechanism word is not shown until "why this?" is opened
    expect(screen.queryByText(/escalate/)).not.toBeInTheDocument()
  })

  it('Do it posts apply and refreshes', async () => {
    post.mockResolvedValue({})
    const onDone = vi.fn()
    render(<SalienceBlock row={row(proposal('retain'))} onDone={onDone} />)
    expect(screen.getByText('Worth remembering')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Do it' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/deliveries/d-1/salience/apply'))
    expect(onDone).toHaveBeenCalled()
  })

  it('Leave it posts decline', async () => {
    post.mockResolvedValue({})
    render(<SalienceBlock row={row(proposal('drop'))} onDone={vi.fn()} />)
    expect(screen.getByText('Looks like noise')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Leave it' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/deliveries/d-1/salience/decline'))
  })

  it('an applied verdict offers Undo — and no Do it / Leave it', async () => {
    post.mockResolvedValue({})
    render(
      <SalienceBlock
        row={row({ ...proposal('escalate')!, applied: true, decision: 'applied', decided_by: 'system' })}
        onDone={vi.fn()}
      />,
    )
    expect(screen.getByText('Leading the next digest.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Do it' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/deliveries/d-1/salience/undo'))
  })

  it('declined and undone states are terminal — no action buttons', () => {
    render(
      <SalienceBlock
        row={row({ ...proposal('escalate')!, decision: 'declined' })}
        onDone={vi.fn()}
      />,
    )
    expect(screen.getByText('Left as-is.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Do it' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
  })

  it('"why this?" discloses the mechanism — model, verdict, confidence, mode, reason', () => {
    render(<SalienceBlock row={row(proposal('escalate'))} onDone={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'why this?' }))
    const why = screen.getByTestId('salience-why')
    expect(why).toHaveTextContent('A model judged this delivery')
    expect(why).toHaveTextContent('escalate')
    expect(why).toHaveTextContent('0.90')
    expect(why).toHaveTextContent('propose')
    expect(why).toHaveTextContent('revenue-impacting and unhandled')
  })

  it('a refusal is shown, not swallowed (spent escalation → 409 detail)', async () => {
    const { ApiError } = await import('../api/client')
    post.mockRejectedValue(
      new ApiError(409, 'already delivered in a digest — the escalation is spent'),
    )
    const onDone = vi.fn()
    render(
      <SalienceBlock
        row={row({ ...proposal('escalate')!, applied: true, decision: 'applied' })}
        onDone={onDone}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await screen.findByText(/the escalation is spent/)
    expect(onDone).not.toHaveBeenCalled()
  })
})
