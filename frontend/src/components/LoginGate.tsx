import { useEffect, useState } from 'react'
import { setToken } from '../api/client'
import { Button, TextInput } from './ui'

/** §18.8 login gate (dark by default): any 401 raises this overlay; with
 * auth off no request ever 401s and the gate never appears. */
export function LoginGate() {
  const [required, setRequired] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const onRequired = () => setRequired(true)
    window.addEventListener('auth:required', onRequired)
    return () => window.removeEventListener('auth:required', onRequired)
  }, [])

  if (!required) return null

  const login = async () => {
    setBusy(true)
    setError(null)
    try {
      const resp = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (!resp.ok) {
        const body = (await resp.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? resp.statusText)
      }
      const out = (await resp.json()) as { token: string }
      setToken(out.token)
      window.location.reload()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      data-testid="login-gate"
      className="fixed inset-0 z-50 flex items-center justify-center bg-void-950/90 backdrop-blur"
    >
      <div className="w-80 rounded-xl border border-slate-800 bg-void-900 p-6 shadow-2xl">
        <div className="font-display text-sm font-bold uppercase tracking-[0.08em] text-slate-100">
          Sign in<span className="text-accent-400">▮</span>
        </div>
        <div className="mt-1 text-[10px] uppercase tracking-[0.25em] text-slate-600">
          auth is enabled on this deployment
        </div>
        <div className="mt-4 space-y-2">
          <TextInput
            placeholder="username"
            value={username}
            autoFocus
            onChange={(e) => setUsername(e.target.value)}
          />
          <TextInput
            placeholder="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void login()
            }}
          />
          {error && <div className="text-xs text-rose-300">{error}</div>}
          <Button variant="primary" onClick={() => void login()} disabled={busy || !username}>
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
        </div>
      </div>
    </div>
  )
}
