import type { Kernel, PlatformVersion } from '@/services/api'

// AgentCore Runtime platform versions, named as two ways of deploying (not
// an old and a new release): V1 is the Standard Runtime, booting each
// session from the image; V2 the Fast Runtime, restoring it from a snapshot
// (fast, steady cold start) at a higher unit price. The UI says Standard /
// Fast; AgentCore's V1 / V2 stay in the hints, to match the AWS docs.
export const PLATFORM_VERSION_LABEL: Record<PlatformVersion, string> = {
  V1: 'Standard',
  V2: 'Fast',
}

const HINT: Record<PlatformVersion, string> = {
  V1: 'Standard Runtime (AgentCore platform V1): boots each session from the image. Slower cold start, lower unit price.',
  V2: 'Fast Runtime (AgentCore platform V2): restores each session from a snapshot, ~2 s cold start regardless of image size, higher unit price, idle memory reclaimed.',
}

/** Version picker for a kernel. Renders nothing on single-runtime
 * deployments. ``value`` '' means "deployment default". */
export function PlatformVersionSelect({
  kernel,
  value,
  onChange,
  allowDefault = false,
}: {
  kernel: Kernel | undefined
  value: PlatformVersion | ''
  onChange: (v: PlatformVersion | '') => void
  // offer "Platform default" (follows later default changes) instead of
  // pinning a version; used for published agents
  allowDefault?: boolean
}) {
  const versions = kernel?.platform_versions ?? []
  if (versions.length < 2) return null
  const def = kernel?.default_platform_version
  const shown = value || def || versions[0].version
  return (
    <>
      <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">Startup</label>
      <select
        className="input"
        value={allowDefault ? value : shown}
        onChange={(e) => onChange(e.target.value as PlatformVersion | '')}
      >
        {allowDefault && (
          <option value="">Platform default{def ? ` · ${PLATFORM_VERSION_LABEL[def]}` : ''}</option>
        )}
        {versions.map((v) => (
          <option key={v.version} value={v.version} disabled={!v.available}>
            {PLATFORM_VERSION_LABEL[v.version]}
            {!allowDefault && v.version === def ? ' · default' : ''}
            {v.available ? '' : ` (${v.status})`}
          </option>
        ))}
      </select>
      <p className="mt-1 text-xs text-slate-400">{HINT[shown]}</p>
    </>
  )
}

/** Small tag for cards; nothing for records without a version. */
export function PlatformVersionTag({ version }: { version: PlatformVersion | '' | undefined }) {
  if (!version) return null
  return (
    <span
      className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-medium ${
        version === 'V2' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600'
      }`}
      title={PLATFORM_VERSION_LABEL[version]}
    >
      {version}
    </span>
  )
}
