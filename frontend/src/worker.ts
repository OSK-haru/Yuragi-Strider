type AssetsBinding = {
  fetch(input: Request | string, init?: RequestInit): Promise<Response>
}

type Env = {
  ASSETS: AssetsBinding
  /** Origin of the API server for the same-origin attack-simulator proxy. */
  ATTACK_API_ORIGIN?: string
}

/**
 * The attack simulator authenticates with SameSite=Strict, HttpOnly cookies, so
 * the browser must talk to it from the page's own origin. This Worker forwards
 * only /api/attack/* (HTTP and WebSocket) to the API server; every other path is
 * still static assets. The browser's Origin header is forwarded untouched, so
 * the server's origin/CSRF checks keep working.
 */
const DEFAULT_ATTACK_API_ORIGIN = 'https://quantsim-vjul.onrender.com'
const ATTACK_PREFIX = '/api/attack/'
const MAX_PROXIED_BODY_BYTES = 70 * 1024

const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "base-uri 'none'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "form-action 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com",
  "img-src 'self' data: blob:",
  "connect-src 'self' https://quantsim-vjul.onrender.com",
].join('; ')

function withSecurityHeaders(response: Response): Response {
  const headers = new Headers(response.headers)
  headers.set('Content-Security-Policy', CONTENT_SECURITY_POLICY)
  headers.set('X-Content-Type-Options', 'nosniff')
  headers.set('X-Frame-Options', 'DENY')
  headers.set('Referrer-Policy', 'strict-origin-when-cross-origin')
  headers.set('Permissions-Policy', 'camera=(), geolocation=(), microphone=()')
  headers.set('Cross-Origin-Opener-Policy', 'same-origin')

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
}

export function attackProxyRequest(request: Request, apiOrigin: string): Request | null {
  const url = new URL(request.url)
  if (!url.pathname.startsWith(ATTACK_PREFIX)) return null
  const target = new URL(url.pathname + url.search, apiOrigin)
  const headers = new Headers(request.headers)
  // Never forward client-asserted proxy identity.
  for (const name of ['x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto', 'forwarded', 'x-real-ip']) {
    headers.delete(name)
  }
  // A same-origin GET carries no Origin; the server's public catalog check needs one.
  if (request.method === 'GET' && !headers.has('origin') && headers.get('sec-fetch-site') === 'same-origin') {
    headers.set('origin', url.origin)
  }
  const init: RequestInit & { duplex?: 'half' } = { method: request.method, headers, body: request.body, redirect: 'manual' }
  if (request.body) init.duplex = 'half' // Streaming a request body; Node requires it, Workers accepts it.
  return new Request(target, init)
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const proxied = attackProxyRequest(request, env.ATTACK_API_ORIGIN ?? DEFAULT_ATTACK_API_ORIGIN)
    if (proxied) {
      const declared = Number(request.headers.get('content-length') ?? '0')
      if (!Number.isFinite(declared) || declared > MAX_PROXIED_BODY_BYTES) {
        return withSecurityHeaders(new Response(null, { status: 413 }))
      }
      const upstream = await fetch(proxied)
      // A WebSocket upgrade (101) must be returned untouched.
      if (upstream.status === 101) return upstream
      const headers = new Headers(upstream.headers)
      headers.set('Cache-Control', 'no-store')
      return withSecurityHeaders(new Response(upstream.body, { status: upstream.status, headers }))
    }
    return withSecurityHeaders(await env.ASSETS.fetch(request))
  },
}
