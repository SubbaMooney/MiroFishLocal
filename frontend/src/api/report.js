import service, { requestWithRetry } from './index'

/**
 * 开始报告生成
 * @param {Object} data - { simulation_id, force_regenerate? }
 */
export const generateReport = (data) => {
  return requestWithRetry(() => service.post('/api/report/generate', data), 3, 1000)
}

/**
 * 获取报告生成状态
 * @param {string} reportId
 */
export const getReportStatus = (reportId) => {
  return service.get(`/api/report/generate/status`, { params: { report_id: reportId } })
}

/**
 * 获取 Agent 日志（增量）
 * @param {string} reportId
 * @param {number} fromLine - 从第几行开始获取
 */
export const getAgentLog = (reportId, fromLine = 0) => {
  return service.get(`/api/report/${reportId}/agent-log`, { params: { from_line: fromLine } })
}

/**
 * 获取控制台日志（增量）
 * @param {string} reportId
 * @param {number} fromLine - 从第几行开始获取
 */
export const getConsoleLog = (reportId, fromLine = 0) => {
  return service.get(`/api/report/${reportId}/console-log`, { params: { from_line: fromLine } })
}

/**
 * 获取报告详情
 * @param {string} reportId
 */
export const getReport = (reportId) => {
  return service.get(`/api/report/${reportId}`)
}

/**
 * 与 Report Agent 对话
 *
 * H4-Fix: chat_history wird seit der server-side Chat-Session-Migration
 * NICHT mehr vom Client geliefert -- der Server haelt die kanonische
 * Historie pro simulation_id. Der Body enthaelt nur noch die neue
 * User-Message; die Antwort enthaelt die volle persistierte History
 * unter ``data.history``.
 *
 * @param {Object} data - { simulation_id, message }
 */
export const chatWithReport = (data) => {
  // Defense-in-Depth: falls der Caller versehentlich chat_history
  // mitschickt, wird sie hier herausgefiltert. Der Server ignoriert
  // sie ebenfalls -- aber so ist das API-Vertrags-Versprechen
  // auch lokal sichtbar.
  const safeBody = {
    simulation_id: data.simulation_id,
    message: data.message,
  }
  return requestWithRetry(() => service.post('/api/report/chat', safeBody), 3, 1000)
}

/**
 * Liefert die persistierte Chat-Historie fuer eine Simulation.
 * Wird beim Oeffnen von Step5 geladen, damit der Client State-Free bleibt.
 */
export const getChatHistory = (simulationId) => {
  return service.get(`/api/report/chat/history/${simulationId}`)
}

/**
 * Loescht die persistierte Chat-Historie.
 */
export const clearChatHistory = (simulationId) => {
  return service.delete(`/api/report/chat/history/${simulationId}`)
}
