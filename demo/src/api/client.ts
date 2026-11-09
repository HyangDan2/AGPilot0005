/**
 * Demo Console API Client and Streaming SSE Parser.
 */

export interface ModelConnectionConfig {
  mode: 'direct' | 'gateway';
  baseUrl: string;
  model: string;
  apiKey: string;
}

export interface ConnectionStatusResult {
  success: boolean;
  status_code?: number;
  duration_ms?: number;
  message: string;
  ready?: boolean;
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

export async function testConnection(config: ModelConnectionConfig): Promise<ConnectionStatusResult> {
  const res = await fetch('/api/v1/demo/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: config.mode,
      base_url: config.baseUrl,
      model: config.model,
      api_key: config.apiKey,
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
  const res = await fetch('/api/v1/demo/tools');
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
  const res = await fetch('/api/v1/demo/run', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({
      mode,
      model_mode: config.mode,
      base_url: config.baseUrl,
      model: config.model,
      api_key: config.apiKey,
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
