import { useState } from 'react'
import {
  updateLLM,
  testLLMConnection,
  type AllSettings,
  type LLMSettings,
} from '../../../services/api'
import { FieldLabel, SaveButton, SectionCard, SectionHeader, TextInput, Toggle } from './shared'
import { useSectionForm } from './useSectionForm'

export function LLMSection({ settings }: { settings: AllSettings }) {
  const llm = settings.llm as LLMSettings

  const systemAllows = llm.system_allows_custom !== false
  const [enabled, setEnabled] = useState(llm.custom_llm_enabled ?? false)
  const [apiFormat, setApiFormat] = useState<'openai' | 'anthropic'>(llm.api_format ?? 'openai')
  const [baseUrl, setBaseUrl] = useState(llm.base_url ?? '')
  const [apiKey, setApiKey] = useState(llm.api_key ?? '')
  const [model, setModel] = useState(llm.model ?? '')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)

  const { saving, saved, handleSave } = useSectionForm(async () => {
    await updateLLM({
      custom_llm_enabled: enabled,
      api_format: apiFormat,
      base_url: baseUrl,
      api_key: apiKey,
      model,
    })
  })

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await testLLMConnection({ api_format: apiFormat, base_url: baseUrl, api_key: apiKey, model })
      setTestResult(result)
    } catch (err: unknown) {
      setTestResult({ success: false, message: err instanceof Error ? err.message : '连接失败' })
    } finally {
      setTesting(false)
    }
  }

  return (
    <SectionCard>
      <SectionHeader icon="🤖" title="LLM 设置" />

      {!systemAllows && (
        <div style={{
          padding: '10px 12px', borderRadius: 8, marginBottom: 16,
          background: 'var(--bg-input)', border: '1px solid var(--border)',
          fontSize: 13, color: 'var(--text-muted)',
        }}>
          系统当前未开放自定义 LLM 接入。
        </div>
      )}

      <div style={{ marginBottom: 16 }}>
        <Toggle
          value={enabled}
          onChange={setEnabled}
          label="启用自定义 LLM"
        />
      </div>

      {enabled && (
        <>
          <div style={{ marginBottom: 16 }}>
            <FieldLabel>API 格式</FieldLabel>
            <div style={{ display: 'flex', gap: 8 }}>
              {(['openai', 'anthropic'] as const).map((fmt) => (
                <button
                  key={fmt}
                  onClick={() => setApiFormat(fmt)}
                  style={{
                    padding: '7px 18px', borderRadius: 6, fontSize: 13,
                    fontWeight: apiFormat === fmt ? 600 : 400,
                    cursor: 'pointer',
                    background: apiFormat === fmt ? 'var(--accent)' : 'var(--bg-input)',
                    color: apiFormat === fmt ? 'white' : 'var(--text-secondary)',
                    border: apiFormat === fmt ? '1px solid var(--accent)' : '1px solid var(--border)',
                    transition: 'background 0.15s',
                  }}
                >
                  {fmt === 'openai' ? 'OpenAI 兼容' : 'Anthropic 兼容'}
                </button>
              ))}
            </div>
          </div>

          <div style={{ marginBottom: 12 }}>
            <FieldLabel>Base URL</FieldLabel>
            <TextInput
              value={baseUrl}
              onChange={setBaseUrl}
              placeholder="https://api.openai.com/v1"
            />
          </div>

          <div style={{ marginBottom: 12 }}>
            <FieldLabel>API Key</FieldLabel>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              style={{
                width: '100%', padding: '8px 12px',
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: 6, color: 'var(--text-primary)', fontSize: 13,
                outline: 'none', boxSizing: 'border-box',
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <FieldLabel>模型名称</FieldLabel>
            <TextInput
              value={model}
              onChange={setModel}
              placeholder="gpt-4o / claude-3-5-sonnet-20241022"
            />
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
            <button
              onClick={() => void handleTest()}
              disabled={testing || !baseUrl || !apiKey || !model}
              style={{
                padding: '7px 20px',
                background: 'var(--bg-input)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border)',
                borderRadius: 6, fontSize: 13, fontWeight: 600,
                cursor: testing || !baseUrl || !apiKey || !model ? 'default' : 'pointer',
                opacity: testing || !baseUrl || !apiKey || !model ? 0.6 : 1,
                transition: 'opacity 0.15s',
              }}
            >
              {testing ? '测试中...' : '测试连接'}
            </button>

            {testResult !== null && (
              <span style={{
                fontSize: 13,
                color: testResult.success ? '#53d769' : '#ff6b6b',
                fontWeight: 500,
              }}>
                {testResult.success ? '✓ ' : '✗ '}{testResult.message}
              </span>
            )}
          </div>
        </>
      )}

      <SaveButton onClick={() => void handleSave()} saving={saving} saved={saved} />
    </SectionCard>
  )
}
