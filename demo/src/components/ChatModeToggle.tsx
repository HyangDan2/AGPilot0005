import React from 'react';
import { Play, Square, Bot, MessageSquare } from 'lucide-react';

interface Props {
  mode: 'chat' | 'agent';
  onModeChange: (mode: 'chat' | 'agent') => void;
  message: string;
  onMessageChange: (message: string) => void;
  onRun: () => void;
  onStop: () => void;
  busy: boolean;
  disabled?: boolean;
}

export const ChatModeToggle: React.FC<Props> = ({
  mode,
  onModeChange,
  message,
  onMessageChange,
  onRun,
  onStop,
  busy,
  disabled = false,
}) => {
  const quickPrompts = [
    { label: '기본 인사 (일반 대화)', text: '안녕, 너는 누구야?' },
    { label: '시간 조회 (DateTime Tool)', text: '오늘 날짜와 시간을 알려줘.' },
    {
      label: '다중 도구 (DateTime + Calculate)',
      text: '오늘 날짜를 알려주고 125 * 37을 계산해 줘.',
    },
    { label: '문서 조회 (Document Tool)', text: '테스트 문서의 목차를 보여줘.' },
  ];

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!busy && message.trim()) {
        onRun();
      }
    }
  };

  return (
    <section className="console-card">
      <div className="card-title">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span>Execution Console</span>
          <span className="helper-text" style={{ margin: 0 }}>
            (Chat vs LangGraph Agent Loop)
          </span>
        </div>
        <div className="mode-selector">
          <label className={`radio-label ${mode === 'chat' ? 'active' : ''}`}>
            <input
              type="radio"
              name="chatMode"
              value="chat"
              checked={mode === 'chat'}
              onChange={() => onModeChange('chat')}
              disabled={busy}
            />
            <MessageSquare size={14} />
            <span>Chat Mode</span>
          </label>
          <label className={`radio-label ${mode === 'agent' ? 'active' : ''}`}>
            <input
              type="radio"
              name="chatMode"
              value="agent"
              checked={mode === 'agent'}
              onChange={() => onModeChange('agent')}
              disabled={busy}
            />
            <Bot size={14} color="var(--accent)" />
            <span style={{ color: mode === 'agent' ? 'var(--accent)' : 'inherit' }}>
              Agent Mode (LangGraph)
            </span>
          </label>
        </div>
      </div>

      <div className="quick-prompts">
        <span style={{ fontSize: '0.75rem', color: 'var(--muted)', alignSelf: 'center' }}>
          추천 테스트:
        </span>
        {quickPrompts.map((p, idx) => (
          <button
            key={idx}
            type="button"
            className="prompt-pill"
            onClick={() => onMessageChange(p.text)}
            disabled={busy}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className="composer-box">
        <textarea
          className="composer-input"
          placeholder={
            mode === 'agent'
              ? '에이전트에게 지시할 메시지를 입력하세요 (예: 오늘 날짜를 조회하고 125 * 37을 계산해 줘)'
              : '일반 LLM에게 전달할 메시지를 입력하세요...'
          }
          value={message}
          onChange={(e) => onMessageChange(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={2}
          disabled={busy || disabled}
        />
        <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
          {busy ? (
            <button
              type="button"
              className="btn-primary btn-stop"
              onClick={onStop}
              style={{ minHeight: '44px', minWidth: '96px' }}
            >
              <Square size={16} />
              <span>Stop</span>
            </button>
          ) : (
            <button
              type="button"
              className="btn-primary"
              onClick={onRun}
              disabled={disabled || !message.trim()}
              style={{ minHeight: '44px', minWidth: '96px' }}
            >
              <Play size={16} />
              <span>Run</span>
            </button>
          )}
        </div>
      </div>
    </section>
  );
};
