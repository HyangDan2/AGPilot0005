/**
 * Demo Console API Client and Streaming SSE Parser.
 */

export interface ModelConnectionConfig {
  mode: 'direct' | 'gateway';
  baseUrl: string;
  model: string;
  apiKey: string;
  gatewayRoute?: string;
}

export interface GatewayRouteItem {
  route: string;
  authorized: boolean;
  ready: boolean;
  model_label?: string | null;
  engine?: string | null;
  context_tokens?: number | null;
  max_output_tokens?: number | null;
  reasoning_effort?: string | null;
  error?: string | null;
}

export interface GatewayRoutesResponse {
  socket_available: boolean;
  default_route: string | null;
  routes: GatewayRouteItem[];
  gatewayAdminAccess: 'granted' | 'denied' | 'unavailable';
}

export interface ConnectionStatusResult {
  success: boolean;
  status_code?: number;
  duration_ms?: number;
  message: string;
  ready?: boolean;
  route?: string;
  model_label?: string | null;
}

export interface ToolDefinitionItem {
  name: string;
  description: string;
  parameters: Record<string, any>;
  is_read_only: boolean;
  active: boolean;
}

export interface ToolTraceRecord {
  step: number;
  tool_name: string;
  tool_call_id: string;
  arguments?: string;
  status: 'started' | 'completed' | 'failed';
  duration_ms?: number;
  result_size_bytes?: number;
  is_error?: boolean;
}

export interface AgentRunCallbacks {
  onRunStarted?: (data: any) => void;
  onStepStarted?: (data: any) => void;
  onModelStarted?: (data: any) => void;
  onToolStarted?: (data: any) => void;
  onToolCompleted?: (data: any) => void;
  onStepCompleted?: (data: any) => void;
  onAnswerDelta?: (delta: string) => void;
  onRunCompleted?: (data: any) => void;
  onRunFailed?: (error: { code: string; message: string }) => void;
  onRunCancelled?: (data: any) => void;
}

const IS_ADMIN_PROXY = typeof window !== 'undefined' && window.location.pathname.startsWith('/admin/agcore');

const API_BASE = IS_ADMIN_PROXY
  ? '/admin/agcore/api/v1/demo'
  : '/api/v1/demo';

const CONFIG_API_BASE = IS_ADMIN_PROXY
  ? '/admin/agcore/api/v1/config/agent'
  : '/api/v1/config/agent';

async function getAdminAuthHeaders(signal?: AbortSignal): Promise<Record<string, string>> {
  if (!IS_ADMIN_PROXY) return {};
  try {
    const res = await fetch('/auth/session', {
      credentials: 'same-origin',
      cache: 'no-store',
      signal,
    });
    if (!res.ok) return {};
    const session = (await res.json()) as { authenticated?: boolean; csrf?: string };
    return session.csrf ? { 'X-CSRF-Token': session.csrf } : {};
  } catch {
    return {};
  }
}

export async function fetchGatewayRoutes(signal?: AbortSignal): Promise<GatewayRoutesResponse> {
  const authorizedMap = new Map<string, GatewayRouteItem>();
  let socketAvailable = false;
  let defaultRoute: string | null = null;

  try {
    const res = await fetch(`${API_BASE}/gateway-routes`, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal,
    });
    if (res.ok) {
      const data = await res.json();
      socketAvailable = Boolean(data.socket_available);
      defaultRoute = data.default_route || null;
      for (const item of Array.isArray(data.routes) ? data.routes : []) {
        if (item && typeof item.route === 'string') {
          authorizedMap.set(item.route, {
            route: item.route,
            authorized: Boolean(item.authorized),
            ready: Boolean(item.ready),
            model_label: item.model_label ?? null,
            context_tokens: item.context_tokens ?? null,
            max_output_tokens: item.max_output_tokens ?? null,
            reasoning_effort: item.reasoning_effort ?? null,
            error: item.error ?? null,
          });
        }
      }
    }
  } catch {
    // handled below with empty list
  }

  let gatewayAdminAccess: 'granted' | 'denied' | 'unavailable' = 'unavailable';

  if (IS_ADMIN_PROXY) {
    try {
      // Uses the logged-in user's own SSO cookie; Nginx admin-sso-api.conf enforces per-user RBAC.
      const gwRes = await fetch('/admin/ai-gateway/api/settings', {
        credentials: 'same-origin',
        cache: 'no-store',
        signal,
      });
      if (gwRes.status === 401 || gwRes.status === 403) {
        gatewayAdminAccess = 'denied';
      } else if (gwRes.ok) {
        gatewayAdminAccess = 'granted';
        const gwData = await gwRes.json();
        const policyRoutes = gwData?.policy?.routes;
        if (policyRoutes && typeof policyRoutes === 'object') {
          for (const [routeName, cfg] of Object.entries<any>(policyRoutes)) {
            const existing = authorizedMap.get(routeName);
            const engineLabel = typeof cfg?.engine === 'string' ? cfg.engine : null;
            const modelFromCfg =
              typeof cfg?.model === 'string' && cfg.model.trim()
                ? cfg.model.trim()
                : typeof cfg?.local_model === 'string' && cfg.local_model.trim()
                  ? cfg.local_model.trim()
                  : null;
            if (existing) {
              existing.engine = engineLabel;
              if (!existing.model_label && modelFromCfg) {
                existing.model_label = modelFromCfg;
              }
            } else {
              authorizedMap.set(routeName, {
                route: routeName,
                authorized: false,
                ready: Boolean(cfg?.enabled),
                model_label: modelFromCfg,
                engine: engineLabel,
                context_tokens: cfg?.context_tokens ?? null,
                max_output_tokens: cfg?.max_output_tokens ?? null,
                reasoning_effort: cfg?.reasoning_effort ?? null,
              });
            }
          }
        }
      }
    } catch {
      gatewayAdminAccess = 'unavailable';
    }
  }

  const routes = Array.from(authorizedMap.values()).sort((a, b) => {
    if (a.authorized !== b.authorized) return a.authorized ? -1 : 1;
    return a.route.localeCompare(b.route);
  });

  if (!defaultRoute) {
    const firstAuth = routes.find((r) => r.authorized);
    defaultRoute = firstAuth ? firstAuth.route : null;
  }

  return {
    socket_available: socketAvailable,
    default_route: defaultRoute,
    routes,
    gatewayAdminAccess,
  };
}

export async function testConnection(config: ModelConnectionConfig): Promise<ConnectionStatusResult> {
  const authHeaders = await getAdminAuthHeaders();
  const res = await fetch(`${API_BASE}/connect`, {
    credentials: 'same-origin',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders },
    body: JSON.stringify({
      mode: config.mode,
      base_url: config.baseUrl,
      model: config.model,
      api_key: config.apiKey,
      gateway_route: config.gatewayRoute,
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    return {
      success: false,
      status_code: res.status,
      message: err.detail || '연결 테스트 요청에 실패했습니다.',
    };
  }
  return await res.json();
}

export async function fetchTools(): Promise<ToolDefinitionItem[]> {
  const res = await fetch(`${API_BASE}/tools`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.tools || [];
}

export async function streamRun(
  config: ModelConnectionConfig,
  mode: 'chat' | 'agent',
  message: string,
  callbacks: AgentRunCallbacks,
  signal?: AbortSignal
): Promise<void> {
  const authHeaders = await getAdminAuthHeaders(signal);
  const res = await fetch(`${API_BASE}/run`, {
    credentials: 'same-origin',
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...authHeaders,
    },
    body: JSON.stringify({
      mode,
      model_mode: config.mode,
      base_url: config.baseUrl,
      model: config.model,
      api_key: config.apiKey,
      gateway_route: config.gatewayRoute,
      message,
      stream: true,
    }),
    signal,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    throw new Error(err.detail || `실행 실패 (${res.status})`);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new Error('스트림 응답을 읽을 수 없습니다.');

  const decoder = new TextDecoder();
  let pending = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      pending += decoder.decode(value, { stream: true });
      const frames = pending.split('\n\n');
      pending = frames.pop() ?? '';

      for (const frame of frames) {
        if (!frame.trim()) continue;
        const lines = frame.split('\n');
        let eventType = '';
        let dataStr = '';

        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            dataStr = line.slice(5).trim();
          }
        }

        if (!dataStr) continue;
        try {
          const payload = JSON.parse(dataStr);
          switch (eventType) {
            case 'run.started':
              callbacks.onRunStarted?.(payload);
              break;
            case 'step.started':
              callbacks.onStepStarted?.(payload);
              break;
            case 'model.started':
              callbacks.onModelStarted?.(payload);
              break;
            case 'tool.started':
              callbacks.onToolStarted?.(payload);
              break;
            case 'tool.completed':
              callbacks.onToolCompleted?.(payload);
              break;
            case 'step.completed':
              callbacks.onStepCompleted?.(payload);
              break;
            case 'answer.delta':
              callbacks.onAnswerDelta?.(payload.delta || '');
              break;
            case 'run.completed':
              callbacks.onRunCompleted?.(payload);
              break;
            case 'run.failed':
              callbacks.onRunFailed?.({
                code: payload.code || 'error',
                message: payload.message || '실행 오류',
              });
              break;
            case 'run.cancelled':
              callbacks.onRunCancelled?.(payload);
              break;
          }
        } catch {
          // ignore malformed lines
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export interface AgentRuntimeConfigDto {
  max_tool_calls: number;
  max_agent_steps: number;
  run_timeout_seconds: number;
}

export async function fetchAgentConfig(): Promise<AgentRuntimeConfigDto> {
  const res = await fetch(CONFIG_API_BASE, { credentials: 'same-origin', cache: 'no-store' });
  if (!res.ok) throw new Error('설정 조회 실패');
  return res.json();
}

export async function updateAgentConfig(patch: Partial<AgentRuntimeConfigDto>): Promise<AgentRuntimeConfigDto> {
  const authHeaders = await getAdminAuthHeaders();
  const res = await fetch(CONFIG_API_BASE, {
    credentials: 'same-origin',
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error('설정 저장 실패');
  return res.json();
}

export async function resetAgentConfig(): Promise<AgentRuntimeConfigDto> {
  const authHeaders = await getAdminAuthHeaders();
  const res = await fetch(`${CONFIG_API_BASE}/reset`, {
    credentials: 'same-origin',
    method: 'POST',
    headers: { ...authHeaders },
  });
  if (!res.ok) throw new Error('설정 초기화 실패');
  return res.json();
}
