import React, { useEffect, useState, useCallback } from 'react';
import { Shield, CheckCircle, AlertCircle, RefreshCw } from 'lucide-react';
import type {
  ModelConnectionConfig,
  ConnectionStatusResult,
  GatewayRouteItem,
} from '../api/client';
import { fetchGatewayRoutes, testConnection } from '../api/client';

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
  const [loadingRoutes, setLoadingRoutes] = useState(false);
  const [gatewayRoutes, setGatewayRoutes] = useState<GatewayRouteItem[]>([]);
  const [gatewaySocketOk, setGatewaySocketOk] = useState<boolean | null>(null);
  const [gatewayAdminAccess, setGatewayAdminAccess] = useState<'granted' | 'denied' | 'unavailable'>('unavailable');

  const loadGatewayRoutes = useCallback(async () => {
    setLoadingRoutes(true);
    try {
      const data = await fetchGatewayRoutes();
      setGatewayRoutes(data.routes);
      setGatewaySocketOk(data.socket_available);
      setGatewayAdminAccess(data.gatewayAdminAccess);
      const authorized = data.routes.filter((r) => r.authorized);
      if (authorized.length > 0) {
        const currentValid = authorized.some((r) => r.route === config.gatewayRoute);
        if (!currentValid) {
          onChange({
            ...config,
            gatewayRoute: data.default_route || authorized[0].route,
          });
        }
      }
    } finally {
      setLoadingRoutes(false);
    }
  }, [config, onChange]);

  useEffect(() => {
    if (config.mode === 'gateway' && gatewayRoutes.length === 0 && !loadingRoutes) {
      loadGatewayRoutes();
    }
  }, [config.mode, gatewayRoutes.length, loadingRoutes, loadGatewayRoutes]);

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
            <label className="form-label">
              <span>AI Gateway 서비스 라우트</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                <Shield size={12} color="var(--olive-text)" />
                <small>AGCore에 토큰이 허용된 라우트만 호출 가능</small>
              </span>
            </label>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
              <select
                className="form-input mono"
                style={{ flex: 1, minWidth: '220px' }}
                value={config.gatewayRoute || ''}
                onChange={(e) => {
                  onChange({ ...config, gatewayRoute: e.target.value });
                  onStatusChange(null);
                }}
                disabled={disabled || loadingRoutes}
              >
                {gatewayRoutes.filter((r) => r.authorized).length > 0 ? (
                  <optgroup label="호출 가능 (AGCore 허용 라우트)">
                    {gatewayRoutes
                      .filter((r) => r.authorized)
                      .map((r) => (
                        <option key={r.route} value={r.route}>
                          {r.route}
                          {r.model_label ? ` — ${r.model_label}` : ''}
                          {r.ready ? ' (준비됨)' : ' (비활성)'}
                        </option>
                      ))}
                  </optgroup>
                ) : (
                  <option value={config.gatewayRoute || 'hd2ai-admin'}>
                    {loadingRoutes ? '라우트 목록 불러오는 중...' : config.gatewayRoute || 'hd2ai-admin'}
                  </option>
                )}
                {gatewayRoutes.filter((r) => !r.authorized).length > 0 && (
                  <optgroup label="조회 전용 (AGCore 호출 토큰 미할당)">
                    {gatewayRoutes
                      .filter((r) => !r.authorized)
                      .map((r) => (
                        <option key={r.route} value={r.route} disabled>
                          {r.route}
                          {r.model_label ? ` — ${r.model_label}` : ''} (권한 없음)
                        </option>
                      ))}
                  </optgroup>
                )}
              </select>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => loadGatewayRoutes()}
                disabled={loadingRoutes || disabled}
                title="AI Gateway 라우트 목록 새로고침"
                style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
              >
                <RefreshCw className={loadingRoutes ? 'animate-spin' : ''} size={14} />
                <span>새로고침</span>
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={handleTest}
                disabled={testing || disabled || !config.gatewayRoute}
              >
                {testing ? <RefreshCw className="animate-spin" size={14} /> : <CheckCircle size={14} />}
                <span>Check AI Gateway Status</span>
              </button>
            </div>
            <div className="helper-text" style={{ marginTop: '6px' }}>
              {gatewaySocketOk === false
                ? 'AI Gateway UDS 소켓에 연결되지 않았습니다.'
                : gatewayAdminAccess === 'denied'
                  ? 'AI Gateway 관리자 조회 권한이 없어 AGCore 허용 라우트만 표시합니다.'
                  : '선택한 AI Gateway 라우트의 인증·모델 정책을 사용합니다. API 키는 브라우저로 노출되지 않습니다.'}
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
            color: status.success ? 'var(--success)' : 'var(--danger)',
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
