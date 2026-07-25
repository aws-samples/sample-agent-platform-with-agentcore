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
 *   end to end.
 */

const TOKEN_KEY = 'agent-platform:id-token'
const USER_KEY = 'agent-platform:user'
const PKCE_KEY = 'agent-platform:pkce'
// OIDC only: kept solely as the `id_token_hint` for RP-initiated logout.
const LOGOUT_HINT_KEY = 'agent-platform:logout-hint'

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
}
