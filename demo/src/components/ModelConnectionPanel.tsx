import React, { useState } from 'react';
import { Shield, CheckCircle, AlertCircle, RefreshCw } from 'lucide-react';
import type { ModelConnectionConfig, ConnectionStatusResult } from '../api/client';
import { testConnection } from '../api/client';

interface Props {
  config: ModelConnectionConfig;
  onChange: (config: ModelConnectionConfig) => void;
  status: ConnectionStatusResult | null;
  onStatusChange: (status: ConnectionStatusResult | null) => void;
  disabled?: boolean;
}

export const ModelConnectionPanel: React.FC<Props> = ({
  config,
  onChange,
  status,
  onStatusChange,
  disabled = false,
}) => {
  const [testing, setTesting] = useState(false);

  const handleTest = async () => {
    setTesting(true);
    try {
      const res = await testConnection(config);
      onStatusChange(res);
    } catch (e: any) {
      onStatusChange({
        success: false,
        message: e.message || '연결 실패',
      });
    } finally {
      setTesting(false);
    }
  };

  const applyPreset = (preset: 'nvidia' | 'openai' | 'mock') => {
    if (preset === 'nvidia') {
      onChange({
        ...config,
        mode: 'direct',
        baseUrl: 'https://integrate.api.nvidia.com/v1',
        model: 'nvidia/nemotron-3.5-super-vl-120b-a12b',
      });
    } else if (preset === 'openai') {
      onChange({
        ...config,
        mode: 'direct',
        baseUrl: 'https://api.openai.com/v1',
        model: 'gpt-4o',
      });
    } else {
      onChange({
        ...config,
        mode: 'direct',
        baseUrl: 'mock',
        model: 'mock-nemotron',
      });
    }
    onStatusChange(null);
  };

  return (
    <section className="console-card">
      <div className="card-title">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span>Model Connection Panel</span>
          <span className="helper-text" style={{ margin: 0 }}>
            (OpenAI-Compatible & Gateway Adapter)
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {status ? (
            <span
              className={`status-badge ${
                status.success ? 'connected' : 'failed'
              }`}
            >
              <i className="status-dot" />
              {status.success
                ? `Connected (${status.duration_ms ?? 0}ms)`
                : 'Connection Failed'}
            </span>
          ) : testing ? (
            <span className="status-badge testing">
              <i className="status-dot" /> Testing...
            </span>
          ) : (
            <span className="status-badge untested">
              <i className="status-dot" /> Untested
            </span>
          )}
        </div>
      </div>

      <div className="config-grid">
        <div className="form-group full-width">
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <span className="form-label">Connection Mode</span>
            <div style={{ display: 'flex', gap: '6px' }}>
              <button
                type="button"
                className="btn-secondary"
                style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                onClick={() => applyPreset('nvidia')}
              >
                NVIDIA Preset
              </button>
              <button
                type="button"
                className="btn-secondary"
                style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                onClick={() => applyPreset('openai')}
              >
                OpenAI Preset
              </button>
              <button
                type="button"
                className="btn-secondary"
                style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                onClick={() => applyPreset('mock')}
              >
                Mock Preset
              </button>
            </div>
          </div>
          <div className="mode-selector">
            <label
              className={`radio-label ${
                config.mode === 'direct' ? 'active' : ''
              }`}
            >
              <input
                type="radio"
                name="modelMode"
                value="direct"
                checked={config.mode === 'direct'}
                onChange={() => {
                  onChange({ ...config, mode: 'direct' });
                  onStatusChange(null);
                }}
                disabled={disabled}
              />
              Direct Test Mode (사용자 API Key/Endpoint 직접 테스트)
            </label>
            <label
              className={`radio-label ${
                config.mode === 'gateway' ? 'active' : ''
              }`}
            >
              <input
                type="radio"
                name="modelMode"
                value="gateway"
                checked={config.mode === 'gateway'}
                onChange={() => {
                  onChange({ ...config, mode: 'gateway' });
                  onStatusChange(null);
                }}
                disabled={disabled}
              />
              Gateway Test Mode (기존 AI Gateway 서버 경유)
            </label>
          </div>
        </div>

        {config.mode === 'direct' && (
          <>
            <div className="form-group">
              <label className="form-label">
                <span>Base URL</span>
                <small>(/chat/completions 제외)</small>
              </label>
              <input
                type="text"
                className="form-input mono"
                placeholder="https://integrate.api.nvidia.com/v1"
                value={config.baseUrl}
                onChange={(e) => {
                  onChange({ ...config, baseUrl: e.target.value });
                  onStatusChange(null);
                }}
                disabled={disabled}
              />
            </div>

            <div className="form-group">
              <label className="form-label">Model Name</label>
              <input
                type="text"
                className="form-input mono"
                placeholder="nvidia/nemotron-3.5-super-vl-120b-a12b"
                value={config.model}
                onChange={(e) => {
                  onChange({ ...config, model: e.target.value });
                  onStatusChange(null);
                }}
                disabled={disabled}
              />
            </div>

            <div className="form-group full-width">
              <label className="form-label">
                <span>API Key</span>
                <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <Shield size={12} color="var(--olive-text)" />
                  <small>브라우저 메모리에만 임시 보관 (저장 없음)</small>
                </span>
              </label>
              <div style={{ display: 'flex', gap: '10px' }}>
                <input
                  type="password"
                  className="form-input mono"
                  style={{ flex: 1 }}
                  placeholder="nvapi-••••••••••••••••••••••••"
                  value={config.apiKey}
                  onChange={(e) => {
                    onChange({ ...config, apiKey: e.target.value });
                    onStatusChange(null);
                  }}
                  disabled={disabled}
                />
                <button
                  type="button"
                  className="btn-primary"
                  onClick={handleTest}
                  disabled={testing || disabled || !config.baseUrl || !config.model}
                >
                  {testing ? <RefreshCw className="animate-spin" size={14} /> : <CheckCircle size={14} />}
                  <span>Test Connection</span>
                </button>
              </div>
            </div>
          </>
        )}

        {config.mode === 'gateway' && (
          <div className="form-group full-width">
            <div className="helper-text" style={{ color: 'var(--olive-text)' }}>
              기존 AI Gateway에 설정된 서비스(hd2ai-admin 등)의 인증과 모델 정책을 사용합니다.
            </div>
            <div style={{ marginTop: '8px' }}>
              <button
                type="button"
                className="btn-primary"
                onClick={handleTest}
                disabled={testing || disabled}
              >
                {testing ? <RefreshCw className="animate-spin" size={14} /> : <CheckCircle size={14} />}
                <span>Check AI Gateway Status</span>
              </button>
            </div>
          </div>
        )}
      </div>

      {status && (
        <div
          style={{
            marginTop: '12px',
            fontSize: '0.8rem',
            padding: '8px 12px',
            borderRadius: '6px',
            backgroundColor: status.success ? 'var(--success-bg)' : 'var(--danger-bg)',
            color: status.success ? 'var(--success)' : '#fca5a5',
            border: `1px solid ${status.success ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)'}`,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          {status.success ? <CheckCircle size={14} /> : <AlertCircle size={14} />}
          <span>{status.message}</span>
        </div>
      )}
      <div className="helper-text">
        * 테스트 호출 시 1 토큰의 최소 핑 요청이 발생합니다. 페이지 새로고침 시 모든 인증 정보는 즉시 소멸합니다.
      </div>
    </section>
  );
};
