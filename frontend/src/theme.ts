/** Client-side theme switching (spec §8.7): four palettes over the same
 * token names — default is the mission-control theme baked into @theme. */
export const THEMES = ['default', 'anthropic', 'openai', 'google'] as const
export type Theme = (typeof THEMES)[number]

const KEY = 'concierge-theme'

export function applyTheme(theme: Theme) {
  if (theme === 'default') document.documentElement.removeAttribute('data-theme')
  else document.documentElement.setAttribute('data-theme', theme)
  localStorage.setItem(KEY, theme)
}

export function currentTheme(): Theme {
  const stored = localStorage.getItem(KEY) as Theme | null
  return stored && (THEMES as readonly string[]).includes(stored) ? stored : 'default'
}

export function initTheme(): Theme {
  const theme = currentTheme()
  applyTheme(theme)
  return theme
}
