// T6 — artifact type + status badges (art-spec §Artifact 美术). Pure mapping so
// the panel renders type / producer / provenance / lock / scan / verification
// without inferring status from colour alone. 6 kinds + 7 status badges.

export interface ArtifactLike {
  kind: string
  unlocked?: boolean
  scan_status?: string | null        // skipped | pending | clean | flagged
  verification_status?: string | null // unverified | verified | rejected
  provenance?: string | null          // runtime | verifier | system | (missing)
  retention_hold?: boolean
}

export interface KindBadge { kind: string; label: string; icon: string; known: boolean }
export interface StatusBadge { key: string; label: string }

const KIND_LABELS: Record<string, { label: string; icon: string }> = {
  text: { label: '文本', icon: '📝' },
  file: { label: '文件', icon: '📄' },
  link: { label: '链接', icon: '🔗' },
  image: { label: '图片', icon: '🖼️' },
  dataset: { label: '数据集', icon: '🗄️' },
  world_draft: { label: '世界草案', icon: '🗺️' },
}

export function artifactKindBadge(kind: string): KindBadge {
  const k = KIND_LABELS[kind]
  return k
    ? { kind, label: k.label, icon: k.icon, known: true }
    : { kind: kind || 'unknown', label: '未知类型', icon: '❔', known: false }
}

// The 7 status badges, resolved from the artifact's fields. An artifact can
// carry more than one (e.g. locked + scanning); order is severity-ish so the
// caller can pick the leading chip while still showing the rest.
export function artifactStatusBadges(a: ArtifactLike): StatusBadge[] {
  const out: StatusBadge[] = []
  if (a.verification_status === 'rejected') out.push({ key: 'rejected', label: '已拒绝' })
  if (a.scan_status === 'flagged') out.push({ key: 'quarantined', label: '隔离' })
  if (!a.provenance) out.push({ key: 'provenance_missing', label: '来源缺失' })
  if (a.scan_status === 'pending') out.push({ key: 'scanning', label: '扫描中' })
  if (a.verification_status === 'verified') out.push({ key: 'verified', label: '已验证' })
  if (a.retention_hold) out.push({ key: 'retained', label: '留存' })
  if (a.unlocked === false) out.push({ key: 'locked', label: '锁定' })
  return out
}
