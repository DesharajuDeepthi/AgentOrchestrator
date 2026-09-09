import { render, screen } from '@testing-library/react'
import type { RemoteAgent } from '../api/types'
import {
  AuthSchemeChips,
  credentialsOut,
  EMPTY_CRED,
  type CredentialRow,
} from '../pages/RemoteAgentsPage'

// spec §8.10 / §19.3 — Remote Agents page helpers: the credentials payload
// mapper (write-only semantics ride on what we send) and the per-scheme
// auth chips (supported/configured states must be visually distinct).

const row = (patch: Partial<CredentialRow>): CredentialRow => ({ ...EMPTY_CRED, ...patch })

const agent = (auth: RemoteAgent['auth']): RemoteAgent => ({
  id: 'a1',
  name: 'stub-agent',
  description: '',
  source: 'dynamic',
  status: 'active',
  created_at: '',
  updated_at: '',
  deleted_at: null,
  card_url: 'http://x/card.json',
  card: null,
  card_fetched_at: null,
  last_error: null,
  tool_count: 0,
  auth,
  auth_status: 'ok',
})

describe('credentialsOut (spec §19.3 — write-only payload mapper)', () => {
  it('maps secret rows to plain string values', () => {
    expect(credentialsOut([row({ scheme: 'main', value: 'tok-123' })])).toEqual({
      main: 'tok-123',
    })
  })

  it('keeps env: indirection verbatim', () => {
    expect(credentialsOut([row({ scheme: 'key', value: 'env:MY_VAR' })])).toEqual({
      key: 'env:MY_VAR',
    })
  })

  it('maps oauth2 rows to client_id/client_secret objects', () => {
    expect(
      credentialsOut([
        row({ scheme: 'oauth', kind: 'oauth2', clientId: 'cid', clientSecret: 'sec' }),
      ]),
    ).toEqual({ oauth: { client_id: 'cid', client_secret: 'sec' } })
  })

  it('drops rows without a scheme or value and returns null when empty', () => {
    expect(credentialsOut([])).toBeNull()
    expect(credentialsOut([row({ scheme: '', value: 'x' })])).toBeNull()
    expect(credentialsOut([row({ scheme: 'a', value: '' })])).toBeNull()
  })

  it('trims scheme names', () => {
    expect(credentialsOut([row({ scheme: ' main ', value: 'v' })])).toEqual({ main: 'v' })
  })
})

describe('AuthSchemeChips (spec §8.10)', () => {
  it('shows the open note when the card declares no auth', () => {
    render(<AuthSchemeChips agent={agent({})} />)
    expect(screen.getByText(/no auth declared/)).toBeInTheDocument()
  })

  it('marks configured, unconfigured, and unsupported schemes distinctly', () => {
    render(
      <AuthSchemeChips
        agent={agent({
          ok: { type: 'http', supported: true, configured: true },
          pending: { type: 'apiKey', supported: true, configured: false },
          mtls: { type: 'mutualTLS', supported: false, configured: false },
        })}
      />,
    )
    expect(screen.getByText(/ok · http/)).toHaveTextContent('✓')
    expect(screen.getByText(/pending · apiKey/)).toHaveTextContent('…')
    expect(screen.getByText(/mtls · mutualTLS/)).toHaveTextContent('✕')
    expect(screen.getByTitle('credentials configured')).toBeInTheDocument()
    expect(screen.getByTitle('no credentials stored')).toBeInTheDocument()
    expect(screen.getByTitle('scheme not supported by this build')).toBeInTheDocument()
  })
})
