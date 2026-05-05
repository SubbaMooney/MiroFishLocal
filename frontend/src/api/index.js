import axios from 'axios'
import i18n from '../i18n'
import { notify } from '../utils/notify'

// 创建axios实例
const service = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:5001',
  timeout: 300000, // 5分钟超时（本体生成可能需要较长时间）
  headers: {
    'Content-Type': 'application/json'
  }
})

// Fix M4: Backend-Error-Strings (potenziell mit Stack-Traces, vgl.
// Backend-C5-Audit) dürfen nicht roh in UI-Komponenten fließen, weil
// sie sonst über v-html (Step4/Step5) reflektiertes XSS auslösen
// können. Wir mappen daher HTTP-Statuscodes auf generische Texte.
// Der echte Server-Error landet auf err.serverError für Console-Logs,
// nicht in err.message.
//
// TODO(i18n): Sobald der i18n-Katalog generische HTTP-Fehlermeldungen
// in zh.json/en.json enthält, sollten die unten hartkodierten
// deutschen Strings durch i18n.global.t('http.<status>') ersetzt
// werden. Aktuell bewusst hartkodiert, damit der Fix unabhängig vom
// Locale-Stand greift.
const STATUS_MESSAGES = {
  400: 'Ungültige Anfrage',
  401: 'Nicht authentifiziert',
  403: 'Keine Berechtigung',
  404: 'Nicht gefunden',
  408: 'Anfrage-Zeitüberschreitung',
  409: 'Konflikt — bitte erneut versuchen',
  413: 'Datei zu groß',
  422: 'Ungültige Eingabedaten',
  429: 'Zu viele Anfragen — bitte kurz warten',
  500: 'Serverfehler — bitte erneut versuchen',
  502: 'Service nicht erreichbar',
  503: 'Service nicht verfügbar',
  504: 'Service-Zeitüberschreitung'
}

const buildSafeError = (statusCode, rawServerError) => {
  const message =
    (statusCode && STATUS_MESSAGES[statusCode]) || 'Unerwarteter Fehler'
  const err = new Error(message)
  err.statusCode = statusCode || null
  // serverError ist bewusst eine private Property: nur fürs Logging,
  // niemals direkt im UI rendern.
  err.serverError = rawServerError || null
  return err
}

// API-Key fuer Auth-Middleware (C1). Aus VITE_MIROFISH_API_KEY zur
// Build-Zeit gelesen. Ohne Key sendet der Axios-Interceptor keinen
// X-API-Key-Header — der Backend antwortet dann mit 401, was die UI
// als "Nicht authentifiziert" anzeigt.
const MIROFISH_API_KEY = import.meta.env.VITE_MIROFISH_API_KEY || ''

// 请求拦截器
service.interceptors.request.use(
  config => {
    config.headers['Accept-Language'] = i18n.global.locale.value
    if (MIROFISH_API_KEY) {
      config.headers['X-API-Key'] = MIROFISH_API_KEY
    }
    return config
  },
  error => {
    console.error('Request error:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器（容错重试机制）
service.interceptors.response.use(
  response => {
    const res = response.data

    // 如果返回的状态码不是success，则抛出错误
    if (!res.success && res.success !== undefined) {
      const rawServerError = res.error || res.message || null
      const err = buildSafeError(response?.status, rawServerError)
      // Logging weiterhin mit dem rohen Server-Error, aber explizit
      // als separater Parameter — kein Splicing in einen Render-Pfad.
      console.error('API Error:', err.statusCode, err.serverError)
      return Promise.reject(err)
    }

    return res
  },
  error => {
    // Server hat geantwortet, aber mit Fehler-Status?
    if (error.response) {
      const rawServerError =
        error.response.data?.error ||
        error.response.data?.message ||
        error.message ||
        null
      const safeErr = buildSafeError(error.response.status, rawServerError)

      // UX-429: Rate-Limit oder Simulations-Quota — User-freundliche
      // Toast-Notification anzeigen. Server liefert ggf. error_code
      // (z.B. "simulation_quota_exceeded") und Retry-After-Header.
      if (error.response.status === 429) {
        const errorCode = error.response.data?.error_code || null
        const retryAfter = parseInt(
          error.response.headers?.['retry-after'] || '0',
          10
        )
        const waitSec = Number.isFinite(retryAfter) && retryAfter > 0
          ? retryAfter
          : null
        let toastMsg
        if (errorCode === 'simulation_quota_exceeded') {
          toastMsg = 'Simulations-Limit erreicht — bitte warten, bis eine laufende Simulation endet.'
        } else if (waitSec) {
          toastMsg = `Zu viele Anfragen — bitte ${waitSec}s warten.`
        } else {
          toastMsg = 'Zu viele Anfragen — bitte kurz warten.'
        }
        notify({ level: 'warn', message: toastMsg, timeoutMs: 8000 })
        safeErr.errorCode = errorCode
        safeErr.retryAfterSec = waitSec
      }

      console.error('Response error:', safeErr.statusCode, safeErr.serverError)
      return Promise.reject(safeErr)
    }

    // Timeout (kein response).
    if (error.code === 'ECONNABORTED' && error.message?.includes('timeout')) {
      const safeErr = buildSafeError(408, error.message)
      console.error('Response error: timeout', safeErr.serverError)
      return Promise.reject(safeErr)
    }

    // Netzwerkfehler (kein response, kein Timeout).
    if (error.message === 'Network Error') {
      const safeErr = buildSafeError(null, error.message)
      safeErr.message = 'Netzwerkfehler — bitte Verbindung prüfen'
      console.error('Response error: network', safeErr.serverError)
      return Promise.reject(safeErr)
    }

    // Unbekannter Fehler ohne response — auf jeden Fall serverError
    // verstecken und generische Message liefern.
    const safeErr = buildSafeError(null, error.message)
    console.error('Response error: unknown', safeErr.serverError)
    return Promise.reject(safeErr)
  }
)

// 带重试的请求函数
export const requestWithRetry = async (requestFn, maxRetries = 3, delay = 1000) => {
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await requestFn()
    } catch (error) {
      if (i === maxRetries - 1) throw error
      
      console.warn(`Request failed, retrying (${i + 1}/${maxRetries})...`)
      await new Promise(resolve => setTimeout(resolve, delay * Math.pow(2, i)))
    }
  }
}

export default service
