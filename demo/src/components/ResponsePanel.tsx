import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Copy, Check, Terminal, AlertCircle } from 'lucide-react';

interface Props {
  content: string;
  isStreaming: boolean;
  error?: string | null;
  mode: 'chat' | 'agent';
}

export const ResponsePanel: React.FC<Props> = ({ content, isStreaming, error, mode }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!content) return;
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback
    }
  };

  if (!content && !isStreaming && !error) {
    return null;
  }

  return (
    <div className="response-container">
      <div className="response-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Terminal size={16} color="var(--accent)" />
          <span style={{ fontSize: '0.88rem', fontWeight: 600, color: 'var(--olive-text)' }}>
            {mode === 'agent' ? 'Agent Final Response' : 'Chat Completion'}
          </span>
          {isStreaming && (
            <span className="status-badge testing" style={{ fontSize: '0.7rem', padding: '1px 6px' }}>
              Streaming...
            </span>
          )}
        </div>
        {content && (
          <button
            type="button"
            className="btn-secondary"
            onClick={handleCopy}
            style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '0.75rem' }}
          >
            {copied ? <Check size={12} color="var(--success)" /> : <Copy size={12} />}
            <span>{copied ? 'Copied!' : 'Copy Response'}</span>
          </button>
        )}
      </div>

      {content ? (
        <div className="markdown-body">
          <ReactMarkdown>{content}</ReactMarkdown>
        </div>
      ) : isStreaming ? (
        <div style={{ color: 'var(--muted)', fontSize: '0.9rem', fontStyle: 'italic' }}>
          답변을 생성하고 있습니다...
        </div>
      ) : null}

      {error && (
        <div className="error-callout">
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600, marginBottom: '4px' }}>
            <AlertCircle size={15} />
            <span>실행 중 오류 발생</span>
          </div>
          <div>{error}</div>
        </div>
      )}
    </div>
  );
};
