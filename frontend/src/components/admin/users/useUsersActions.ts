import { useEffect, useState, useCallback } from 'react'
import { useGameStore } from '../../../stores/gameStore'
import {
  getAdminUsers,
  adminPatchUser,
  type AdminUserListItem,
} from '../../../services/api'

// ─── Users Panel State & Actions ──────────────────────────────────

export function useUsersActions() {
  const token = useGameStore((s) => s.token)
  const [users, setUsers] = useState<AdminUserListItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [perPage] = useState(20)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [adjustTarget, setAdjustTarget] = useState<AdminUserListItem | null>(null)
  const [actionLoading, setActionLoading] = useState<string | null>(null)

  const totalPages = Math.max(1, Math.ceil(total / perPage))

  const fetchUsers = useCallback(async () => {
    if (!token) return
    setLoading(true)
    setError(null)
    try {
      const result = await getAdminUsers(token, { page, per_page: perPage, search: search || undefined })
      setUsers(result.items)
      setTotal(result.total)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [token, page, perPage, search])

  useEffect(() => {
    void fetchUsers()
  }, [fetchUsers])

  const handleSearch = () => {
    setSearch(searchInput)
    setPage(1)
  }

  const handleClearSearch = () => {
    setSearch('')
    setSearchInput('')
    setPage(1)
  }

  const handleAdjustCoinSuccess = (userId: string, newBalance: number) => {
    setUsers((prev) =>
      prev.map((u) => (u.id === userId ? { ...u, soul_coin_balance: newBalance } : u))
    )
    setAdjustTarget(null)
  }

  const handleToggleBan = async (user: AdminUserListItem) => {
    if (!token) return
    setActionLoading(user.id)
    try {
      const updated = await adminPatchUser(token, user.id, { is_banned: !user.is_banned })
      setUsers((prev) => prev.map((u) => (u.id === user.id ? { ...u, is_banned: updated.is_banned } : u)))
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setActionLoading(null)
    }
  }

  const handleToggleAdmin = async (user: AdminUserListItem) => {
    if (!token) return
    setActionLoading(user.id)
    try {
      const updated = await adminPatchUser(token, user.id, { is_admin: !user.is_admin })
      setUsers((prev) => prev.map((u) => (u.id === user.id ? { ...u, is_admin: updated.is_admin } : u)))
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setActionLoading(null)
    }
  }

  return {
    users,
    total,
    page,
    setPage,
    perPage,
    totalPages,
    search,
    searchInput,
    setSearchInput,
    loading,
    error,
    adjustTarget,
    setAdjustTarget,
    actionLoading,
    fetchUsers,
    handleSearch,
    handleClearSearch,
    handleAdjustCoinSuccess,
    handleToggleBan,
    handleToggleAdmin,
  }
}
