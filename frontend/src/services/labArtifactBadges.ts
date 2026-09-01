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
  storage_status?: string | null       // pending_upload | quarantined | released | delete_pending | deleted
}

export interface KindBadge { kind: string; label: string; icon: string; known: boolean }
export interface StatusBadge { key: string; label: string }
export type ArtifactBadgeLocale = 'zh-CN' | 'en'

const KIND_LABELS: Record<string, { zh: string; en: string; icon: string }> = {
  text: { zh: '文本', en: 'Text', icon: '📝' },
  file: { zh: '文件', en: 'File', icon: '📄' },
  link: { zh: '链接', en: 'Link', icon: '🔗' },
  image: { zh: '图片', en: 'Image', icon: '🖼️' },
  dataset: { zh: '数据集', en: 'Dataset', icon: '🗄️' },
  world_draft: { zh: '世界草案', en: 'World draft', icon: '🗺️' },
}

export function artifactKindBadge(kind: string, locale: ArtifactBadgeLocale = 'zh-CN'): KindBadge {
  const k = KIND_LABELS[kind]
  return k
    ? { kind, label: locale === 'en' ? k.en : k.zh, icon: k.icon, known: true }
    : { kind: kind || 'unknown', label: locale === 'en' ? 'Unknown type' : '未知类型', icon: '❔', known: false }
}

// The 7 status badges, resolved from the artifact's fields. An artifact can
// carry more than one (e.g. locked + scanning); order is severity-ish so the
// caller can pick the leading chip while still showing the rest.
export function artifactStatusBadges(a: ArtifactLike, locale: ArtifactBadgeLocale = 'zh-CN'): StatusBadge[] {
  const out: StatusBadge[] = []
  const label = (zh: string, en: string) => locale === 'en' ? en : zh
  if (a.verification_status === 'rejected') out.push({ key: 'rejected', label: label('已拒绝', 'Rejected') })
  if (a.scan_status === 'flagged') out.push({ key: 'quarantined', label: label('隔离', 'Quarantined') })
  if (a.storage_status === 'pending_upload') out.push({ key: 'uploading', label: label('上传中', 'Uploading') })
  if (a.scan_status === 'failed') out.push({ key: 'scan_failed', label: label('扫描失败', 'Scan failed') })
  if (!a.provenance) out.push({ key: 'provenance_missing', label: label('来源缺失', 'Source missing') })
  if (a.scan_status === 'pending' || a.scan_status === 'scanning') out.push({ key: 'scanning', label: label('扫描中', 'Scanning') })
  if (a.verification_status === 'verified') out.push({ key: 'verified', label: label('已验证', 'Verified') })
  if (a.storage_status === 'deleted') out.push({ key: 'deleted', label: label('已过期', 'Expired') })
  if (a.retention_hold) out.push({ key: 'retained', label: label('留存', 'Retained') })
  if (a.unlocked === false) out.push({ key: 'locked', label: label('锁定', 'Locked') })
  return out
}
