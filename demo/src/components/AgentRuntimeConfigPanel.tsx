import React, { useState, useEffect } from 'react';
import { Sliders, CheckCircle, RefreshCw, RotateCcw } from 'lucide-react';
import {
  type AgentRuntimeConfigDto,
  fetchAgentConfig,
  updateAgentConfig,
  resetAgentConfig,
} from '../api/client';

export const AgentRuntimeConfigPanel: React.FC = () => {
  const [config, setConfig] = useState<AgentRuntimeConfigDto>({
    max_tool_calls: 16,
    max_agent_steps: 20,
    run_timeout_seconds: 180,
  });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    fetchAgentConfig()
      .then((cfg) => setConfig(cfg))
      .catch((err) => console.warn('Config fetch error:', err))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setNotice(null);
    try {
      const updated = await updateAgentConfig(config);
      setConfig(updated);
      setNotice('AI Gateway & Agent 런타임 설정이 성공적으로 저장되었습니다.');
      setTimeout(() => setNotice(null), 3000);
    } catch (e: any) {
      setNotice(`저장 실패: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!window.confirm('기본 정책(16회 도구 호출, 20단계, 180초 제한)으로 초기화할까요?')) return;
    setSaving(true);
    setNotice(null);
    try {
      const def = await resetAgentConfig();
      setConfig(def);
      setNotice('기본 정책으로 복원되었습니다 (16회/20단계/180초).');
      setTimeout(() => setNotice(null), 3000);
    } catch (e: any) {
      setNotice(`초기화 실패: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="console-card" style={{ borderLeft: '3px solid #ff824f' }}>
      <div className="card-title">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Sliders size={16} color="#ff824f" />
          <span style={{ fontWeight: 700, color: '#f3f4f6' }}>AI Gateway Tool Calling 설정</span>
          <span className="helper-text" style={{ margin: 0 }}>
            (오케스트레이션 런타임 예산 제어)
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button
            type="button"
            className="btn-secondary"
            style={{ padding: '3px 8px', fontSize: '0.75rem', display: 'flex', alignItems: 'center', gap: '4px' }}
            onClick={handleReset}
            disabled={saving || loading}
            title="기본값 복원 (16 Tools / 20 Steps / 180s)"
          >
            <RotateCcw size={12} />
            <span>기본값 복원</span>
          </button>
          <button
            type="button"
            className="btn-primary"
            style={{ padding: '3px 12px', fontSize: '0.75rem', display: 'flex', alignItems: 'center', gap: '4px' }}
            onClick={handleSave}
            disabled={saving || loading}
          >
            {saving ? <RefreshCw className="animate-spin" size={12} /> : <CheckCircle size={12} />}
            <span>설정 저장</span>
          </button>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '14px', marginTop: '10px' }}>
        <div className="form-group" style={{ margin: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
            <label className="form-label" style={{ margin: 0 }}>최대 도구 호출 수 (Tool Calls)</label>
            <span style={{ fontWeight: 700, color: '#ff824f', fontFamily: 'monospace' }}>{config.max_tool_calls}회</span>
          </div>
          <input
            type="range"
            min={1}
            max={32}
            value={config.max_tool_calls}
            onChange={(e) => setConfig({ ...config, max_tool_calls: Number(e.target.value) })}
            style={{ width: '100%', accentColor: '#ff824f' }}
          />
          <small className="helper-text" style={{ marginTop: '2px' }}>기본값: 16회 (범위: 1~32회)</small>
        </div>

        <div className="form-group" style={{ margin: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
            <label className="form-label" style={{ margin: 0 }}>최대 에이전트 단계 수 (Max Steps)</label>
            <span style={{ fontWeight: 700, color: '#ff824f', fontFamily: 'monospace' }}>{config.max_agent_steps}단계</span>
          </div>
          <input
            type="range"
            min={1}
            max={40}
            value={config.max_agent_steps}
            onChange={(e) => setConfig({ ...config, max_agent_steps: Number(e.target.value) })}
            style={{ width: '100%', accentColor: '#ff824f' }}
          />
          <small className="helper-text" style={{ marginTop: '2px' }}>권장: 도구 수 + 4 이상 (기본값: 20단계)</small>
        </div>

        <div className="form-group" style={{ margin: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
            <label className="form-label" style={{ margin: 0 }}>실행 제한 시간 (Timeout)</label>
            <span style={{ fontWeight: 700, color: '#ff824f', fontFamily: 'monospace' }}>{config.run_timeout_seconds}초</span>
          </div>
          <input
            type="range"
            min={30}
            max={600}
            step={10}
            value={config.run_timeout_seconds}
            onChange={(e) => setConfig({ ...config, run_timeout_seconds: Number(e.target.value) })}
            style={{ width: '100%', accentColor: '#ff824f' }}
          />
          <small className="helper-text" style={{ marginTop: '2px' }}>기본값: 180초 (범위: 30~600초)</small>
        </div>
      </div>

      {notice && (
        <div style={{ marginTop: '10px', fontSize: '0.8rem', color: notice.includes('실패') ? '#fca5a5' : '#86efac', display: 'flex', alignItems: 'center', gap: '6px' }}>
          <CheckCircle size={14} />
          <span>{notice}</span>
        </div>
      )}
    </section>
  );
};
