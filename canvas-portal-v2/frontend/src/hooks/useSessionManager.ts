import { useEffect, useRef, useState, useCallback } from 'react'
import { getTokenExpiry, getTokenIssuedAt, requestNewToken, renewAccessToken, logout } from '@/lib/auth'

// Show warning this many ms before the access token expires
const WARN_BEFORE_MS = 2 * 60 * 1000   // 2 minutes
// While the user is active, renew a token this old, so that the time before an
// idle logout stays close to the token's full lifetime whenever they stop working
const RENEW_AFTER_MS = 5 * 60 * 1000   // 5 minutes
// Also renew when this close to expiry, for tokens shorter than RENEW_AFTER_MS
const SILENT_REFRESH_MS = 60 * 1000    // 60 seconds
// How often the background check runs
const CHECK_INTERVAL_MS = 15_000
// Events that count as user activity
const ACTIVITY_EVENTS = ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll'] as const

export function useSessionManager() {
  const [showWarning, setShowWarning]   = useState(false)
  const [secondsLeft, setSecondsLeft]   = useState(120)
  const [continuing, setContinuing] = useState(false)
  const [continueError, setContinueError] = useState(false)

  const lastActivityRef  = useRef<number>(Date.now())
  const showWarningRef   = useRef(false)        // mirrors state without stale closure
  const countdownRef     = useRef<ReturnType<typeof setInterval> | null>(null)
  const refreshingRef    = useRef(false)

  // Keep ref in sync with state
  useEffect(() => { showWarningRef.current = showWarning }, [showWarning])

  // ── Activity tracking ──────────────────────────────────────────────────────
  const handleActivity = useCallback(() => {
    lastActivityRef.current = Date.now()
  }, [])

  useEffect(() => {
    ACTIVITY_EVENTS.forEach(e => window.addEventListener(e, handleActivity, { passive: true }))
    return () => ACTIVITY_EVENTS.forEach(e => window.removeEventListener(e, handleActivity))
  }, [handleActivity])

  // ── Countdown while warning is visible ────────────────────────────────────
  useEffect(() => {
    if (!showWarning) return
    countdownRef.current = setInterval(() => {
      if (refreshingRef.current) return
      setSecondsLeft(prev => {
        if (prev <= 1) { logout(); return 0 }
        return prev - 1
      })
    }, 1000)
    return () => {
      if (countdownRef.current) clearInterval(countdownRef.current)
    }
  }, [showWarning])

  // ── Background session check ───────────────────────────────────────────────
  useEffect(() => {
    const check = async () => {
      const exp = getTokenExpiry()
      if (!exp) return

      const now = Date.now()
      const msLeft = exp * 1000 - now

      if (msLeft <= 0 && !refreshingRef.current) { logout(); return }
      if (refreshingRef.current) return

      const isActive = (now - lastActivityRef.current) < 60_000

      const issuedAt = getTokenIssuedAt()
      const isOld = issuedAt !== null && now - issuedAt * 1000 >= RENEW_AFTER_MS

      // An open warning stays until the user answers it: its countdown ends in a logout regardless.
      if ((isOld || msLeft <= SILENT_REFRESH_MS) && isActive && !showWarningRef.current && !refreshingRef.current) {
        // User is active — renew silently, retrying on the next check after a temporary failure
        refreshingRef.current = true
        const ok = await renewAccessToken()
        refreshingRef.current = false
        if (!ok) logout()
        return
      }

      if (msLeft <= WARN_BEFORE_MS && !isActive && !showWarningRef.current) {
        // Inactive and token about to expire — show warning
        setSecondsLeft(Math.min(Math.floor(msLeft / 1000), 120))
        setShowWarning(true)
      }
    }

    const interval = setInterval(check, CHECK_INTERVAL_MS)
    check()
    return () => clearInterval(interval)
  }, []) // run once — refs handle stale closures

  // ── Public API ─────────────────────────────────────────────────────────────
  const continueSession = useCallback(async () => {
    if (refreshingRef.current) return
    refreshingRef.current = true
    setContinuing(true)
    setContinueError(false)
    const outcome = await requestNewToken()
    refreshingRef.current = false
    setContinuing(false)
    if (outcome === 'refreshed') {
      lastActivityRef.current = Date.now()
      setShowWarning(false)
    } else if (outcome === 'session-ended') {
      await logout()
    } else {
      setContinueError(true)
    }
  }, [])

  return { showWarning, secondsLeft, continuing, continueError, continueSession }
}
