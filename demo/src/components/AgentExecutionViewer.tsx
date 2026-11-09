import React, { useState } from 'react';
import { Check, Loader2, AlertTriangle } from 'lucide-react';

export interface ExecutionEventItem {
  id: string;
  type: 'step' | 'model' | 'tool_started' | 'tool_completed' | 'generating' | 'completed' | 'error';
  title: string;
  detail?: any;
  status: 'pending' | 'running' | 'done' | 'failed';
  timestamp?: number;
}

interface Props {
  events: ExecutionEventItem[];
  busy: boolean;
}

export const AgentExecutionViewer: React.FC<Props> = ({ events, busy }) => {
  const [showDetails, setShowDetails] = useState(false);

  if (events.length === 0) {
    return null;
  }

  return (
    <div className="execution-viewer">
      <div className="execution-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {busy ? (
            <>
              <Loader2 className="animate-spin" size={16} color="var(--accent)" />
              <span>Agent Running...</span>
            </>
          ) : (
            <>
              <Check size={16} color="var(--success)" />
              <span>Agent Execution Complete</span>
            </>
          )}
        </div>
        <button
          type="button"
          className="btn-secondary"
          style={{ padding: '3px 8px', fontSize: '0.75rem' }}
          onClick={() => setShowDetails(!showDetails)}
        >
          {showDetails ? 'Hide Details' : 'View Details'}
        </button>
      </div>

      <div className="steps-list">
        {events.map((item) => (
          <div
            key={item.id}
            className={`step-item ${item.status === 'running' ? 'active' : item.status === 'done' ? 'done' : ''}`}
          >
            {item.status === 'done' ? (
              <span className="step-check">✓</span>
            ) : item.status === 'running' ? (
              <Loader2 className="animate-spin" size={12} color="var(--accent)" />
            ) : item.status === 'failed' ? (
              <AlertTriangle size={12} color="var(--danger)" />
            ) : (
              <span style={{ width: '12px' }}>•</span>
            )}
            <span>{item.title}</span>
          </div>
        ))}
      </div>

      {showDetails && (
        <div className="trace-details">
          <div style={{ fontSize: '0.75rem', color: 'var(--muted)', fontWeight: 600 }}>
            Execution Trace Log (Tool Arguments & Results)
          </div>
          {events
            .filter((e) => e.type === 'tool_started' || e.type === 'tool_completed' || e.type === 'model')
            .map((e) => (
              <div key={e.id} className="trace-card">
                <div className="trace-top">
                  <span>{e.title}</span>
                  <span style={{ fontSize: '0.7rem', color: 'var(--muted)' }}>
                    {e.status.toUpperCase()}
                  </span>
                </div>
                {e.detail && (
                  <pre className="trace-json">
                    {typeof e.detail === 'string'
                      ? e.detail
                      : JSON.stringify(e.detail, null, 2)}
                  </pre>
                )}
              </div>
            ))}
        </div>
      )}
    </div>
  );
};
