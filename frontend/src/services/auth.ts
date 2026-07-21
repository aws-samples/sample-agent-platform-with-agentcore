/** Cognito auth: USER_PASSWORD_AUTH sign-in + token storage.
 *
 * The backend's public /api/v1/config tells the frontend which auth mode is
 * active and which Cognito app client to use — no build-time configuration.
 */

const TOKEN_KEY = 'agent-platform:id-token'
const USER_KEY = 'agent-platform:user'

export interface PublicConfig {
  auth_mode: 'cognito' | 'token' | 'open'
  cognito_region: string
  cognito_client_id: string
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

export function signOut(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

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
