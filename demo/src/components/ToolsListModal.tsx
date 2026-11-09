import React from 'react';
import { X, Wrench, CheckCircle } from 'lucide-react';
import type { ToolDefinitionItem } from '../api/client';

interface Props {
  tools: ToolDefinitionItem[];
  onClose: () => void;
}

export const ToolsListModal: React.FC<Props> = ({ tools, onClose }) => {
  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 9999,
        padding: '20px',
      }}
      onClick={onClose}
    >
      <div
        className="console-card"
        style={{
          width: '100%',
          maxWidth: '680px',
          maxHeight: '80vh',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="card-title" style={{ marginBottom: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Wrench size={16} color="var(--accent)" />
            <span>AGCore Registered Tools ({tools.length})</span>
          </div>
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            style={{ padding: '2px 8px' }}
          >
            <X size={14} />
          </button>
        </div>

        <div style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {tools.map((t) => (
            <div
              key={t.name}
              style={{
                background: 'var(--card-sub-bg)',
                border: '1px solid var(--line)',
                borderRadius: '6px',
                padding: '12px',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  marginBottom: '6px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <CheckCircle size={14} color="var(--success)" />
                  <strong style={{ color: 'var(--text)', fontFamily: 'var(--mono-family)' }}>
                    {t.name}
                  </strong>
                </div>
                <span
                  style={{
                    fontSize: '0.7rem',
                    color: t.is_read_only ? 'var(--olive-text)' : 'var(--accent)',
                    border: '1px solid var(--line)',
                    padding: '1px 6px',
                    borderRadius: '4px',
                  }}
                >
                  {t.is_read_only ? 'READ-ONLY (PARALLEL)' : 'MUTATING'}
                </span>
              </div>
              <p style={{ fontSize: '0.82rem', color: 'var(--muted)', marginBottom: '8px' }}>
                {t.description}
              </p>
              <pre className="trace-json" style={{ maxHeight: '120px' }}>
                {JSON.stringify(t.parameters, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
