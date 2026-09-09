import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type {
  CacheStatus,
  Conversation,
  ConversationDetail,
  HitlPending,
  McpServer,
  ProviderInfo,
  RemoteAgent,
  Run,
  Settings,
  Skill,
  SubAgent,
  Tool,
} from './types'

export function useServers(params = '') {
  return useQuery({
    queryKey: ['mcp-servers', params],
    queryFn: () => api.get<McpServer[]>(`/mcp-servers${params}`),
    refetchInterval: 5000,
  })
}

export function useRemoteAgents(params = '') {
  return useQuery({
    queryKey: ['remote-agents', params],
    queryFn: () => api.get<RemoteAgent[]>(`/remote-agents${params}`),
    refetchInterval: 5000,
  })
}

export function useTools(params = '') {
  return useQuery({
    queryKey: ['tools', params],
    queryFn: () => api.get<Tool[]>(`/tools${params}`),
    refetchInterval: 5000,
  })
}

export function useSkills(params = '') {
  return useQuery({
    queryKey: ['skills', params],
    queryFn: () => api.get<Skill[]>(`/skills${params}`),
  })
}

export function useSubAgents(params = '') {
  return useQuery({
    queryKey: ['sub-agents', params],
    queryFn: () => api.get<SubAgent[]>(`/sub-agents${params}`),
  })
}

export function useRuns() {
  return useQuery({
    queryKey: ['runs'],
    queryFn: () => api.get<Run[]>('/runs'),
    refetchInterval: 3000,
  })
}

export function useRun(runId: string | null) {
  return useQuery({
    queryKey: ['runs', runId],
    queryFn: () => api.get<Run>(`/runs/${runId}`),
    enabled: runId !== null,
    refetchInterval: 2000,
  })
}

export function useConversations() {
  return useQuery({
    queryKey: ['conversations'],
    queryFn: () => api.get<Conversation[]>('/conversations'),
  })
}

export function useConversation(id: string | null) {
  return useQuery({
    queryKey: ['conversations', id],
    queryFn: () => api.get<ConversationDetail>(`/conversations/${id}`),
    enabled: id !== null,
  })
}

export function useSettings() {
  return useQuery({ queryKey: ['settings'], queryFn: () => api.get<Settings>('/settings') })
}

export function useProviders() {
  return useQuery({ queryKey: ['providers'], queryFn: () => api.get<ProviderInfo[]>('/providers') })
}

export function useHitlPending() {
  return useQuery({
    queryKey: ['hitl-pending'],
    queryFn: () => api.get<HitlPending[]>('/hitl/pending'),
    refetchInterval: 3000,
  })
}

/** M42: delivered-but-never-opened count for the Ambient nav badge. */
export function useUnreadDeliveries(enabled: boolean) {
  return useQuery({
    queryKey: ['deliveries', 'unread-count'],
    queryFn: () => api.get<{ count: number; attention: number }>('/deliveries/unread-count'),
    refetchInterval: 5000,
    enabled,
  })
}

export function useInvalidate() {
  const qc = useQueryClient()
  return (...keys: string[]) => {
    for (const key of keys) void qc.invalidateQueries({ queryKey: [key] })
  }
}

export function useCacheStatus() {
  return useQuery({
    queryKey: ['cache-status'],
    queryFn: () => api.get<CacheStatus>('/cache/status'),
    refetchInterval: 10000,
  })
}

export function useRefreshCache() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (registry: string) =>
      api.post<Record<string, unknown>>(`/cache/refresh/${registry}`, {}),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['cache-status'] }),
  })
}

export function usePatchSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (updates: Record<string, unknown>) => api.patch<Settings>('/settings', updates),
    onSuccess: (data) => {
      qc.setQueryData(['settings'], data)
      // registry_cache_mode lives in settings — reflect flips immediately
      void qc.invalidateQueries({ queryKey: ['cache-status'] })
    },
  })
}
