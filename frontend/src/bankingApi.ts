export type Operation = { id: string; type: string; tier: 'yellow' | 'red'; expires_at: string; details: Record<string, unknown> }

export class ApiError extends Error {
  readonly status: number | null
  readonly code: string
  readonly outcomeUnknown: boolean
  constructor(message: string, options: { status?: number; code: string; outcomeUnknown?: boolean }) {
    super(message)
    this.name = 'ApiError'
    this.status = options.status ?? null
    this.code = options.code
    this.outcomeUnknown = options.outcomeUnknown ?? false
  }
}

function errorCode(message: string, status: number): string {
  if (/模拟验证码已过期/.test(message)) return 'VERIFICATION_EXPIRED'
  if (/验证请求已作废|尝试次数用完/.test(message)) return 'VERIFICATION_INVALID'
  if (/先完成.*模拟强验证/.test(message)) return 'VERIFICATION_REQUIRED'
  if (/确认已过期|操作已失效/.test(message)) return 'ACTION_EXPIRED'
  return `HTTP_${status}`
}

function httpError(response: Response, result: unknown, mutation: boolean): ApiError {
  // Never retain raw responses, request bodies, tokens, or validation inputs on errors.
  const fallback = response.status >= 500 ? '服务暂时无法返回结果，请稍后重试原操作。' : '请求未通过校验，请核对输入。'
  if (response.status >= 500) return new ApiError(fallback, { status: response.status, code: `HTTP_${response.status}`, outcomeUnknown: mutation })
  const data = result && typeof result === 'object' ? result as Record<string, unknown> : {}
  const detail = data.detail && typeof data.detail === 'object' && !Array.isArray(data.detail) ? data.detail as Record<string, unknown> : data
  const candidate = typeof data.detail === 'string' ? data.detail : typeof detail.message === 'string' ? detail.message : fallback
  const message = candidate.length <= 500 ? candidate : fallback
  const explicitCode = typeof detail.code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(detail.code) ? detail.code : undefined
  return new ApiError(message, { status: response.status, code: explicitCode || errorCode(message, response.status), outcomeUnknown: mutation && response.status === 408 })
}

export async function requestJson<T>(path: string, body?: unknown, signal?: AbortSignal, options: { timeoutMs?: number } = {}): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) abort()
  else signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timeout = setTimeout(() => { timedOut = true; controller.abort() }, options.timeoutMs ?? 20_000)
  const mutation = body !== undefined
  try {
    const response = await fetch(path, { method: mutation ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' }, ...(mutation ? { body: JSON.stringify(body) } : {}), signal: controller.signal })
    let result: unknown
    try { result = await response.json() }
    catch (cause) {
      if (controller.signal.aborted) throw cause
      if (!response.ok) throw httpError(response, null, mutation)
      throw new ApiError('未能读取操作结果，请核对原操作回执。', { status: response.status, code: 'INVALID_RESPONSE', outcomeUnknown: mutation })
    }
    if (!response.ok) throw httpError(response, result, mutation)
    return result as T
  } catch (cause) {
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError')
    if (timedOut) throw new ApiError('等待结果超时，当前无法确认服务端是否已处理。', { code: 'REQUEST_TIMEOUT', outcomeUnknown: mutation })
    if (cause instanceof ApiError) throw cause
    throw new ApiError('网络连接中断，当前无法读取操作结果。', { code: 'NETWORK_ERROR', outcomeUnknown: mutation })
  } finally {
    clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}
