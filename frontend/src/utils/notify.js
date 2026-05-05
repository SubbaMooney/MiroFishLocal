/**
 * Minimaler Notification/Toast-Bus.
 *
 * Fix UX-429 (Audit-Folge nach H6-Rate-Limits): Wenn das Backend mit 429
 * (Rate-Limit oder Simulations-Quota) antwortet, soll der User eine kurze
 * generische Information sehen — KEINE rohe Server-Message (vgl. M4),
 * sondern eine Erklaerung samt Wartezeit.
 *
 * Bewusst keine UI-Lib (kein element-plus / naive-ui) — wir schicken
 * Custom-Events ueber `window`, eine kleine `<ToastHost />` in App.vue
 * abonniert sie und rendert.
 */

const EVENT_NAME = 'mirofish:notify'

/**
 * @param {Object} payload
 * @param {string} payload.level - 'info' | 'warn' | 'error'
 * @param {string} payload.message - Anzuzeigender Text
 * @param {number} [payload.timeoutMs] - 0 = sticky; default 6000
 */
export function notify({ level = 'info', message, timeoutMs = 6000 }) {
  if (!message) return
  if (typeof window === 'undefined') return
  window.dispatchEvent(
    new CustomEvent(EVENT_NAME, {
      detail: { level, message, timeoutMs }
    })
  )
}

export const NOTIFY_EVENT = EVENT_NAME
