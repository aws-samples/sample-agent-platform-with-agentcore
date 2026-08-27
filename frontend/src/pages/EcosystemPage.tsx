import { useCallback, useEffect, useState } from 'react'
import { Boxes, Loader2, Plus, Server, Sparkles, Trash2 } from 'lucide-react'
import { Modal, SectionTitle } from '@/components/common/ui'
import { api, type EcosystemEntry } from '@/services/api'

const SKILL_TEMPLATE = `---
name: my-skill
description: When Claude should use this skill.
---

# My Skill

Instructions Claude follows when the skill applies.
`

function EntryCard({ e, onDelete }: { e: EcosystemEntry; onDelete: () => void }) {
  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          {e.type === 'mcp' ? (
            <Server size={15} className="text-brand-600" />
          ) : (
            <Sparkles size={15} className="text-amber-500" />
          )}
          <p className="text-sm font-semibold text-slate-900">{e.name}</p>
          {e.builtin && (
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[9px] font-medium text-slate-500">
              BUILT-IN
            </span>
          )}
        </div>
        {!e.builtin && (
          <button className="text-slate-300 transition hover:text-red-600" onClick={onDelete} title="Delete">
            <Trash2 size={14} />
          </button>
        )}
      </div>
      <p className="mt-1.5 text-xs text-slate-500">{e.description || '—'}</p>
      {e.type === 'mcp' && (
        <p className="mt-2 truncate font-mono text-[11px] text-slate-400" title={e.target}>
          {e.kind === 'agentcore-runtime'
            ? 'AgentCore Runtime · '
            : e.kind === 'agentcore-gateway'
              ? 'AgentCore Gateway · '
              : e.kind === 'builtin'
                ? 'AgentCore Built-in · '
                : e.kind === 'mcp-hub'
                  ? 'MCP Hub (HMAC-signed) · '
                  : 'HTTP · '}
          {e.target}
        </p>
      )}
      {e.type === 'skill' && (
        <p className="mt-2 truncate font-mono text-[11px] text-slate-400">s3 · {e.s3_prefix}</p>
      )}
    </div>
  )
}

export default function EcosystemPage() {
  const [mcp, setMcp] = useState<EcosystemEntry[]>([])
  const [skills, setSkills] = useState<EcosystemEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modal, setModal] = useState<'mcp' | 'skill' | null>(null)
  const [saving, setSaving] = useState(false)

  const [mName, setMName] = useState('')
  const [mDesc, setMDesc] = useState('')
  const [mKind, setMKind] = useState('agentcore-runtime')
  const [mTarget, setMTarget] = useState('')

  const [sName, setSName] = useState('')
  const [sDesc, setSDesc] = useState('')
  const [sMd, setSMd] = useState(SKILL_TEMPLATE)

  const refresh = useCallback(async () => {
    try {
      const [m, s] = await Promise.all([api.listMcpServers(), api.listSkills()])
      setMcp(m)
      setSkills(s)
      setError('')
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const saveMcp = async () => {
    setSaving(true)
    try {
      await api.createMcpServer({ name: mName, description: mDesc, kind: mKind, target: mTarget })
      setModal(null)
      setMName('')
      setMDesc('')
      setMTarget('')
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const saveSkill = async () => {
    setSaving(true)
    try {
      await api.createSkill({ name: sName, description: sDesc, skill_md: sMd })
      setModal(null)
      setSName('')
      setSDesc('')
      setSMd(SKILL_TEMPLATE)
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const del = async (e: EcosystemEntry) => {
    try {
      if (e.type === 'mcp') await api.deleteMcpServer(e.id)
      else await api.deleteSkill(e.id)
      refresh()
    } catch (err) {
      setError(String(err))
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <SectionTitle
        title="MCP & Skills"
        subtitle="Registry of tools and skills — attach them to Dev Workbench sessions at creation, or pass MCP servers to headless kernel invocations"
      />

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>
      )}
      {loading && <p className="text-sm text-slate-400">Loading…</p>}

      <div className="grid gap-8 lg:grid-cols-2">
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-800">
              <Server size={15} /> MCP Servers
            </h2>
            <button className="btn-secondary !px-2.5 !py-1 text-xs" onClick={() => setModal('mcp')}>
              <Plus size={12} /> Register
            </button>
          </div>
          <div className="space-y-3">
            {mcp.map((e) => (
              <EntryCard key={e.id} e={e} onDelete={() => del(e)} />
            ))}
            {!loading && mcp.length === 0 && (
              <div className="card p-5 text-center text-sm text-slate-400">
                <Boxes size={22} className="mx-auto mb-2" strokeWidth={1.4} />
                No MCP servers registered
              </div>
            )}
          </div>
        </section>

        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-800">
              <Sparkles size={15} /> Skills
            </h2>
            <button className="btn-secondary !px-2.5 !py-1 text-xs" onClick={() => setModal('skill')}>
              <Plus size={12} /> Register
            </button>
          </div>
          <div className="space-y-3">
            {skills.map((e) => (
              <EntryCard key={e.id} e={e} onDelete={() => del(e)} />
            ))}
            {!loading && skills.length === 0 && (
              <div className="card p-5 text-center text-sm text-slate-400">
                <Sparkles size={22} className="mx-auto mb-2" strokeWidth={1.4} />
                No skills registered
              </div>
            )}
          </div>
        </section>
      </div>

      <Modal open={modal === 'mcp'} title="Register MCP server" onClose={() => setModal(null)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
        <input className="input" placeholder="e.g. jira-tools" value={mName} onChange={(e) => setMName(e.target.value)} />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Description</label>
        <input className="input" value={mDesc} onChange={(e) => setMDesc(e.target.value)} />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Kind</label>
        <select className="input" value={mKind} onChange={(e) => setMKind(e.target.value)}>
          <option value="agentcore-runtime">AgentCore Runtime (ARN, SigV4 via kernel role)</option>
          <option value="agentcore-gateway">AgentCore Gateway (MCP URL, SigV4 via kernel role)</option>
          <option value="url">HTTP URL (streamable-http, no auth)</option>
          <option value="mcp-hub">MCP Hub (HMAC-signed)</option>
        </select>
        {mKind === 'mcp-hub' && (
          <p className="mt-1.5 text-xs text-slate-500">
            A self-hosted MCP hub with MCPHUB-HMAC-SHA256 inbound auth. Published agents sign
            with per-agent credentials minted at publish time (register the agent's access key
            with the hub after publishing); workbench sessions and the Debug console sign as the
            shared <span className="font-mono">dev-workbench</span> actor.
          </p>
        )}
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">
          {mKind === 'agentcore-runtime' ? 'Runtime ARN' : mKind === 'agentcore-gateway' ? 'Gateway MCP URL' : mKind === 'mcp-hub' ? 'Hub MCP URL' : 'URL'}
        </label>
        <input
          className="input font-mono text-xs"
          placeholder={
            mKind === 'agentcore-runtime'
              ? 'arn:aws:bedrock-agentcore:…:runtime/…'
              : mKind === 'agentcore-gateway'
                ? 'https://<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp'
                : mKind === 'mcp-hub'
                  ? 'http://<hub-host>:8000/mcp'
                  : 'https://…/mcp'
          }
          value={mTarget}
          onChange={(e) => setMTarget(e.target.value)}
        />
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setModal(null)}>Cancel</button>
          <button className="btn-primary" onClick={saveMcp} disabled={saving || !mName || !mTarget}>
            {saving && <Loader2 size={14} className="animate-spin" />} Register
          </button>
        </div>
      </Modal>

      <Modal open={modal === 'skill'} title="Register skill" onClose={() => setModal(null)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
        <input className="input" placeholder="e.g. code-review-checklist" value={sName} onChange={(e) => setSName(e.target.value)} />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Description</label>
        <input className="input" value={sDesc} onChange={(e) => setSDesc(e.target.value)} />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">SKILL.md</label>
        <textarea
          className="input h-52 font-mono text-xs"
          value={sMd}
          onChange={(e) => setSMd(e.target.value)}
        />
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setModal(null)}>Cancel</button>
          <button className="btn-primary" onClick={saveSkill} disabled={saving || !sName || !sMd}>
            {saving && <Loader2 size={14} className="animate-spin" />} Register
          </button>
        </div>
      </Modal>
    </div>
  )
}
