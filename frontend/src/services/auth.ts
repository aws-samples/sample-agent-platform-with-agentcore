/** Auth: Cognito USER_PASSWORD_AUTH or generic OIDC (authorization code + PKCE).
 *
 * The backend's public /api/v1/config tells the frontend which auth mode is
 * active — no build-time configuration.
 *
 * - `cognito`: username/password form, ID token stored.
 * - `oidc` (enterprise SSO, e.g. the Keycloak realm from TeamAuthStack):
 *   redirect to the IdP, exchange the code with PKCE, store the **access
 *   token** — the same credential the backend forwards to JWT-protected
 *   AgentCore runtimes/gateways, so IdP claims like `team` propagate
 *   end to end. The refresh token is stored alongside it so a lapsing access
 *   token is renewed silently (see `refreshAccessToken`) rather than bouncing
 *   the user to the login page once every `accessTokenLifespan`.
 */

const TOKEN_KEY = 'agent-platform:id-token'
const USER_KEY = 'agent-platform:user'
const PKCE_KEY = 'agent-platform:pkce'
// OIDC only: kept solely as the `id_token_hint` for RP-initiated logout.
const LOGOUT_HINT_KEY = 'agent-platform:logout-hint'
// OIDC only: traded for a fresh access token when the current one runs out.
// Its own lifetime is bounded by the realm's SSO session, so this is what
// decides how long a user stays signed in across tabs and restarts.
const REFRESH_KEY = 'agent-platform:refresh-token'

/** Renew this many seconds before the access token's `exp`, so an in-flight
 *  request never races its own expiry (and tolerates small clock skew). */
const REFRESH_SKEW_SECONDS = 60

export interface PublicConfig {
  auth_mode: 'cognito' | 'oidc' | 'token' | 'open'
  cognito_region: string
  cognito_client_id: string
  oidc_issuer: string
  oidc_client_id: string
}

let cachedConfig: PublicConfig | null = null

export async function getPublicConfig(): Promise<PublicConfig> {
  if (cachedConfig) return cachedConfig
  const resp = await fetch('/api/v1/config')
  if (!resp.ok) throw new Error(`config fetch failed: ${resp.status}`)
  cachedConfig = (await resp.json()) as PublicConfig
  return cachedConfig
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getUser(): string | null {
  return localStorage.getItem(USER_KEY)
}

/** Drop every locally held credential. Used on token expiry, where a fresh
 *  sign-in (possibly silent, via the IdP session) is the wanted outcome. */
export function clearLocalSession(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(LOGOUT_HINT_KEY)
  localStorage.removeItem(REFRESH_KEY)
  sessionStorage.removeItem(PKCE_KEY)
}

/** Sign out.
 *
 * Clearing local tokens is only half of it: with an external IdP the browser
 * still holds the IdP's own SSO session cookie, so the next sign-in would be
 * granted silently as the same user. In OIDC mode we therefore end the session
 * at the IdP too (RP-initiated logout, OIDC Session Management), which leaves
 * the SPA — the caller does not need to navigate afterwards.
 *
 * Returns true when the browser is being redirected to the IdP.
 */
export async function signOut(): Promise<boolean> {
  const hint = localStorage.getItem(LOGOUT_HINT_KEY)
  clearLocalSession()

  let cfg: PublicConfig | null = null
  try {
    cfg = await getPublicConfig()
  } catch {
    /* offline / config unreachable: local sign-out is all we can do */
  }
  // Cognito mode here is USER_PASSWORD_AUTH (no hosted-UI cookie), so there is
  // no remote session to end — dropping the token is a complete sign-out.
  if (cfg?.auth_mode !== 'oidc' || !cfg.oidc_issuer) return false

  const params = new URLSearchParams({ post_logout_redirect_uri: `${window.location.origin}/login` })
  // id_token_hint identifies the session to end; client_id is the fallback the
  // spec allows when no ID token is at hand (e.g. a session from an older build).
  if (hint) params.set('id_token_hint', hint)
  else params.set('client_id', cfg.oidc_client_id)
  window.location.assign(`${cfg.oidc_issuer}/protocol/openid-connect/logout?${params}`)
  return true
}

// ------------------------------- Cognito ---------------------------------

export async function signIn(username: string, password: string): Promise<void> {
  const cfg = await getPublicConfig()
  const resp = await fetch(`https://cognito-idp.${cfg.cognito_region}.amazonaws.com/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-amz-json-1.1',
      'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
    },
    body: JSON.stringify({
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: cfg.cognito_client_id,
      AuthParameters: { USERNAME: username, PASSWORD: password },
    }),
  })
  const data = await resp.json()
  if (!resp.ok) {
    throw new Error(data.message || data.__type || 'Sign-in failed')
  }
  if (data.ChallengeName) {
    // Admin-created users must have a permanent password
    // (admin-set-user-password --permanent) — this sample UI doesn't
    // implement challenge flows.
    throw new Error(
      `Account requires ${data.ChallengeName}. Ask your operator to set a permanent password.`,
    )
  }
  const idToken = data.AuthenticationResult?.IdToken
  if (!idToken) throw new Error('No ID token in response')
  localStorage.setItem(TOKEN_KEY, idToken)
  localStorage.setItem(USER_KEY, username)
}

// ---------------------------- OIDC (PKCE) --------------------------------

function base64Url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

function redirectUri(): string {
  return `${window.location.origin}/login`
}

/** Start the authorization-code + PKCE flow: redirects to the IdP. */
export async function signInRedirect(): Promise<void> {
  const cfg = await getPublicConfig()
  const verifierBytes = crypto.getRandomValues(new Uint8Array(32))
  const verifier = base64Url(verifierBytes)
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  const challenge = base64Url(new Uint8Array(digest))
  const state = base64Url(crypto.getRandomValues(new Uint8Array(16)))
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state }))

  const params = new URLSearchParams({
    client_id: cfg.oidc_client_id,
    redirect_uri: redirectUri(),
    response_type: 'code',
    scope: 'openid profile',
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  })
  window.location.assign(`${cfg.oidc_issuer}/protocol/openid-connect/auth?${params}`)
}

/** Complete the flow on redirect back (?code=...&state=...). */
export async function completeOidcLogin(code: string, state: string): Promise<void> {
  const cfg = await getPublicConfig()
  const saved = sessionStorage.getItem(PKCE_KEY)
  if (!saved) throw new Error('No PKCE state — restart sign-in')
  const { verifier, state: expectedState } = JSON.parse(saved) as {
    verifier: string
    state: string
  }
  if (state !== expectedState) throw new Error('State mismatch — restart sign-in')
  sessionStorage.removeItem(PKCE_KEY)

  const resp = await fetch(`${cfg.oidc_issuer}/protocol/openid-connect/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: cfg.oidc_client_id,
      redirect_uri: redirectUri(),
      code,
      code_verifier: verifier,
    }),
  })
  const data = await resp.json()
  if (!resp.ok) throw new Error(data.error_description || data.error || 'Token exchange failed')
  const accessToken: string | undefined = data.access_token
  if (!accessToken) throw new Error('No access token in response')

  // display name from the (unverified — display only) token payload
  let username = 'user'
  try {
    const payload = JSON.parse(atob(accessToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
    username = payload.preferred_username || payload.sub || username
  } catch {
    /* display-only fallback */
  }
  localStorage.setItem(TOKEN_KEY, accessToken)
  localStorage.setItem(USER_KEY, username)
  if (data.id_token) localStorage.setItem(LOGOUT_HINT_KEY, data.id_token)
  // Keycloak issues a refresh token for the authorization-code grant without
  // the `offline_access` scope being asked for; it is what carries the session
  // past the (deliberately short) access-token lifespan.
  if (data.refresh_token) localStorage.setItem(REFRESH_KEY, data.refresh_token)
}

// --------------------------- Silent renewal ------------------------------

function accessTokenExpiry(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
    return typeof payload.exp === 'number' ? payload.exp : null
  } catch {
    return null
  }
}

/** True when a stored access token is at, or close to, its expiry.
 *
 * False when there is no token at all — that case is a sign-in, not a renewal.
 * Also false when `exp` cannot be read: rejecting such a token here would sign
 * the user out on every request, so it is left for the server to judge.
 */
export function accessTokenNeedsRefresh(): boolean {
  const token = getToken()
  if (!token) return false
  const exp = accessTokenExpiry(token)
  if (exp === null) return false
  return exp - Date.now() / 1000 <= REFRESH_SKEW_SECONDS
}

let refreshInFlight: Promise<string | null> | null = null

/** Trade the stored refresh token for a fresh access token.
 *
 * Concurrent callers share one exchange: a page load fires several requests at
 * once and each would otherwise post its own grant, which hammers the IdP and,
 * should the realm ever turn on refresh-token rotation, would invalidate the
 * token its siblings are still holding.
 *
 * Resolves to the new access token, or to null when no silent renewal is
 * possible (no refresh token, non-OIDC mode, ended IdP session, network
 * failure). Null means the caller must fall back to interactive sign-in.
 */
export function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = exchangeRefreshToken().finally(() => {
      refreshInFlight = null
    })
  }
  return refreshInFlight
}

async function exchangeRefreshToken(): Promise<string | null> {
  const refreshToken = localStorage.getItem(REFRESH_KEY)
  if (!refreshToken) return null

  // Cognito mode (USER_PASSWORD_AUTH) stores no refresh token, so this path is
  // OIDC-only; bail out rather than post to the wrong token endpoint.
  let cfg: PublicConfig
  try {
    cfg = await getPublicConfig()
  } catch {
    return null
  }
  if (cfg.auth_mode !== 'oidc' || !cfg.oidc_issuer) return null

  let data: Record<string, unknown>
  try {
    const resp = await fetch(`${cfg.oidc_issuer}/protocol/openid-connect/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: cfg.oidc_client_id,
        refresh_token: refreshToken,
      }),
    })
    data = (await resp.json()) as Record<string, unknown>
    if (!resp.ok) {
      // The IdP session has ended (expired, revoked, or signed out elsewhere).
      // Drop the dead token so later requests stop retrying a lost cause.
      localStorage.removeItem(REFRESH_KEY)
      return null
    }
  } catch {
    // A network or CORS failure says nothing about the token's validity: keep
    // it, since the next attempt may succeed once connectivity is back.
    return null
  }

  if (typeof data.access_token !== 'string') return null
  localStorage.setItem(TOKEN_KEY, data.access_token)
  // Keycloak reissues the siblings on every exchange; storing them keeps the
  // session renewable and the logout hint current.
  if (typeof data.refresh_token === 'string') localStorage.setItem(REFRESH_KEY, data.refresh_token)
  if (typeof data.id_token === 'string') localStorage.setItem(LOGOUT_HINT_KEY, data.id_token)
  return data.access_token
}
