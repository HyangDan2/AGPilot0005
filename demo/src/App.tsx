import { useState, useEffect, useRef } from 'react';
import { ModelConnectionPanel } from './components/ModelConnectionPanel';
import { ChatModeToggle } from './components/ChatModeToggle';
import { AgentExecutionViewer, type ExecutionEventItem } from './components/AgentExecutionViewer';
import { ResponsePanel } from './components/ResponsePanel';
import { ToolsListModal } from './components/ToolsListModal';
import {
  type ModelConnectionConfig,
  type ConnectionStatusResult,
  type ToolDefinitionItem,
  fetchTools,
  streamRun,
} from './api/client';
import { Wrench } from 'lucide-react';
import './styles/console.css';

export default function App() {
  const [config, setConfig] = useState<ModelConnectionConfig>({
    mode: 'direct',
    baseUrl: 'https://integrate.api.nvidia.com/v1',
    model: 'nvidia/nemotron-3.5-super-vl-120b-a12b',
    apiKey: '',
  });

  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatusResult | null>(null);
  const [chatMode, setChatMode] = useState<'chat' | 'agent'>('agent');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<ExecutionEventItem[]>([]);
  const [responseContent, setResponseContent] = useState('');
  const [error, setError] = useState<string | null>(null);

  const [tools, setTools] = useState<ToolDefinitionItem[]>([]);
  const [showToolsModal, setShowToolsModal] = useState(false);

  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    fetchTools().then(setTools).catch(() => {});
  }, []);

  const handleRun = async () => {
    if (!message.trim() || busy) return;

    setBusy(true);
    setEvents([]);
    setResponseContent('');
    setError(null);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    let eventCounter = 0;
    const addEvent = (
      type: ExecutionEventItem['type'],
      title: string,
      detail?: any,
      status: ExecutionEventItem['status'] = 'running'
    ) => {
      eventCounter += 1;
      const newItem: ExecutionEventItem = {
        id: `evt-${Date.now()}-${eventCounter}`,
        type,
        title,
        detail,
        status,
        timestamp: Date.now(),
      };
      setEvents((prev) => [...prev, newItem]);
    };

    const updateLastEvent = (status: ExecutionEventItem['status'], detail?: any) => {
      setEvents((prev) => {
        if (!prev.length) return prev;
        const copy = [...prev];
        const lastIdx = copy.length - 1;
        copy[lastIdx] = {
          ...copy[lastIdx],
          status,
          detail: detail !== undefined ? detail : copy[lastIdx].detail,
        };
        return copy;
      });
    };

    try {
      await streamRun(
        config,
        chatMode,
        message,
        {
          onRunStarted: () => {
            addEvent('step', chatMode === 'agent' ? 'Agent Run Started' : 'Chat Request Started', undefined, 'done');
          },
          onStepStarted: (data) => {
            addEvent('step', `Step ${data.step} Started`, data, 'done');
          },
          onModelStarted: (data) => {
            addEvent('model', `LLM Request (Step ${data.step})`, undefined, 'running');
          },
          onToolStarted: (data) => {
            updateLastEvent('done');
            addEvent('tool_started', `Tool Selected: ${data.tool_name}`, data.arguments, 'running');
          },
          onToolCompleted: (data) => {
            updateLastEvent(data.is_error ? 'failed' : 'done', {
              duration_ms: data.duration_ms,
              result_size: data.result_size_bytes,
            });
            addEvent(
              'tool_completed',
              `Tool Executed: ${data.tool_name} (${data.duration_ms ?? 0}ms)`,
              undefined,
              'done'
            );
          },
          onStepCompleted: () => {
            updateLastEvent('done');
          },
          onAnswerDelta: (delta) => {
            setEvents((prev) => {
              const hasGen = prev.some((e) => e.type === 'generating');
              if (!hasGen) {
                return [
                  ...prev,
                  {
                    id: `evt-gen-${Date.now()}`,
                    type: 'generating',
                    title: 'Generating Final Answer',
                    status: 'running',
                  },
                ];
              }
              return prev;
            });
            setResponseContent((prev) => prev + delta);
          },
          onRunCompleted: () => {
            setEvents((prev) =>
              prev.map((e) => (e.type === 'generating' ? { ...e, status: 'done' } : e))
            );
            addEvent('completed', 'Execution Completed Successfully', undefined, 'done');
          },
          onRunFailed: (err) => {
            setError(err.message || '실행 실패');
            addEvent('error', `Execution Failed: ${err.message}`, err, 'failed');
          },
          onRunCancelled: () => {
            addEvent('error', 'Execution Cancelled by User', undefined, 'failed');
          },
        },
        controller.signal
      );
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || '알 수 없는 오류가 발생했습니다.');
        addEvent('error', `Error: ${err.message}`, err, 'failed');
      }
    } finally {
      setBusy(false);
      abortControllerRef.current = null;
    }
  };

  const handleStop = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setBusy(false);
  };

  return (
    <div className="console-container">
      {/* Header */}
      <header className="console-header">
        <div className="brand-wrapper">
          <img src="/assets/LOGO.png" alt="HD2Lab" className="brand-logo" />
          <span className="brand-title">AGPilot0005 — Agent Demo Console</span>
          <span className="brand-badge">v1.1 MVP</span>
        </div>
        <div className="header-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setShowToolsModal(true)}
            style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
          >
            <Wrench size={14} color="var(--accent)" />
            <span>Active Tools ({tools.length})</span>
          </button>
        </div>
      </header>

      {/* Model Connection Panel */}
      <ModelConnectionPanel
        config={config}
        onChange={setConfig}
        status={connectionStatus}
        onStatusChange={setConnectionStatus}
        disabled={busy}
      />

      {/* Execution Console & Mode Toggle */}
      <ChatModeToggle
        mode={chatMode}
        onModeChange={setChatMode}
        message={message}
        onMessageChange={setMessage}
        onRun={handleRun}
        onStop={handleStop}
        busy={busy}
      />

      {/* Real-time Agent Execution Viewer */}
      {chatMode === 'agent' && (
        <AgentExecutionViewer events={events} busy={busy} />
      )}

      {/* Final Markdown Response */}
      <ResponsePanel
        content={responseContent}
        isStreaming={busy}
        error={error}
        mode={chatMode}
      />

      {/* Active Tools Modal */}
      {showToolsModal && (
        <ToolsListModal tools={tools} onClose={() => setShowToolsModal(false)} />
      )}
    </div>
  );
}
