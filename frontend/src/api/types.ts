export type Source = 'static' | 'dynamic'
export type Status = 'active' | 'inactive' | 'error'
export type RunStatus = 'running' | 'paused_hitl' | 'completed' | 'failed' | 'cancelled'

export interface RegistryRecord {
  id: string
  name: string
  description: string
  source: Source
  status: Status
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface McpServer extends RegistryRecord {
  transport: 'stdio' | 'http'
  command: string | null
  args: string[] | null
  env: Record<string, string> | null
  url: string | null
  headers: Record<string, string> | null
  last_connected_at: string | null
  last_error: string | null
  tool_count: number
}

export interface RemoteAgent extends RegistryRecord {
  card_url: string
  card: Record<string, unknown> | null
  card_fetched_at: string | null
  last_error: string | null
  tool_count: number
  auth: Record<string, { type: string; supported: boolean; configured: boolean }>
  auth_status: 'ok' | 'unconfigured' | 'unsupported' | 'open'
}

export interface A2ATask {
  id: string
  remote_agent_id: string
  run_id: string | null
  remote_task_id: string | null
  state: string
  question: string | null
  error: string | null
  parked_at: string | null
  delivered: boolean
  created_at: string
  updated_at: string
}

export interface Tool extends RegistryRecord {
  kind: 'mcp' | 'native' | 'a2a'
  remote_agent_id: string | null
  mcp_server_id: string | null
  tool_name: string
  native_ref: string | null
  tool_key: string
  direct_exposure: boolean
  input_schema: Record<string, unknown> | null
}

export interface ModelParams {
  effort?: 'none' | 'low' | 'medium' | 'high' | null
  temperature?: number | null
  max_output_tokens?: number | null
}

export interface Skill extends RegistryRecord {
  kind: 'native' | 'custom'
  persona: string
  instructions: string
  direct_exposure: boolean
  model: string | null
  model_params: ModelParams | null
  max_tool_iterations: number | null
  tools: Tool[]
}

export interface WorkflowNode {
  id: string
  type: 'skill' | 'hitl'
  skill_id?: string
  prompt?: string
  instructions?: string
}

export interface WorkflowEdge {
  from: string
  to: string
  condition?: string
  on?: 'success' | 'error'
}

export interface Workflow {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
}

export interface SubAgent extends RegistryRecord {
  kind: 'native' | 'custom'
  persona: string
  model: string | null
  model_params: ModelParams | null
  workflow: Workflow | null
  native_ref: string | null
  covers_skill_ids: string[] | null
  direct_exposure: boolean
  skills: Skill[]
}

export interface RunStep {
  id: string
  parent_step_id: string | null
  sub_agent_id: string | null
  node_id: string | null
  step_type: 'plan' | 'route' | 'skill' | 'hitl' | 'tool_call' | 'aggregate'
  input: Record<string, unknown> | null
  output: Record<string, unknown> | null
  model: string | null
  input_tokens: number
  output_tokens: number
  status: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface Run {
  id: string
  conversation_id: string
  chat_message: string
  status: RunStatus
  orchestrator_mode: 'graph' | 'agentic' | 'direct'
  target_sub_agent_id?: string | null
  include_history_summary?: boolean
  plan: Record<string, unknown> | null
  snapshot?: Record<string, unknown> | null
  final_answer: string | null
  answer_ui: { a2ui?: unknown[]; charts?: unknown[]; presentation?: string; coverage?: number; blocks?: { a2ui?: unknown[]; chart?: unknown; tool_chart_ref?: number; table?: unknown }[] } | null
  charts?: unknown[] | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  total_input_tokens: number
  total_output_tokens: number
  steps?: RunStep[]
}

export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
  run_count: number
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'error'
  content: string
  run_id: string
  answer_ui?: { a2ui?: unknown[]; charts?: unknown[]; presentation?: string; coverage?: number; blocks?: { a2ui?: unknown[]; chart?: unknown; tool_chart_ref?: number; table?: unknown }[] } | null
  charts?: unknown[] | null
}

export interface ConversationDetail {
  id: string
  title: string
  messages: ChatMessage[]
  runs: Run[]
}

export interface ProviderInfo {
  provider_id: string
  configured: boolean
  models: {
    id: string
    display_name: string
    supports_effort: boolean
    supports_temperature: boolean
    supports_max_output_tokens: boolean
  }[]
}

export type Settings = Record<string, unknown> & {
  orchestrator_mode: 'graph' | 'agentic'
  orchestrator_full_fallback_enabled: boolean
  default_model: string
  default_model_params: ModelParams | null
  planner_model: string | null
  planner_model_params: ModelParams | null
  aggregator_model: string | null
  aggregator_model_params: ModelParams | null
  max_parallel_dispatch: number
  max_plan_steps: number
  max_tool_iterations: number
  dynamic_worker_fallback_enabled: boolean
  direct_exposure_cap_warning: number
  formatter_enabled: boolean
  formatter_presentation: 'a2ui_first' | 'raw_first'
  formatter_model: string | null
  formatter_model_params: ModelParams | null
  formatter_coverage_flag_threshold: number
  mcp_health_interval_s: number
  log_level: string
  langsmith_enabled: boolean
  langsmith_endpoint: string
  langsmith_project: string
  otlp_endpoint: string
  registry_cache_mode: 'bypass' | 'memory' | 'redis'
  retrieval_enabled: boolean
  retrieval_threshold: number
  retrieval_top_k: number
  embedding_model: string | null
}

export interface CacheRegistryStatus {
  records: number | null
  generation: number
  loaded_at: string | null
  cached: boolean
}

export interface CacheStatus {
  mode: 'bypass' | 'memory' | 'redis'
  registries: Record<string, CacheRegistryStatus>
}

export interface HitlPending {
  run_id: string
  conversation_id: string
  chat_message: string
  started_at: string
}

export interface SseEvent {
  type: string
  run_id: string
  ts: string
  payload: Record<string, unknown>
}

export interface OverlapCheck {
  overlap: boolean
  threshold: number
  overlap_percent: number
  match_type: string
  match_id: string | null
  match_name: string | null
  reasoning: string
}
