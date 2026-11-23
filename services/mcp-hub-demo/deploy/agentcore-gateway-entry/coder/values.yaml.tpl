# Helm values of the Coder server (chart coder-v2/coder), rendered by
# deploy.sh from terraform output. No secret here: the database URL and the
# Keycloak client secret come from Kubernetes Secrets deploy.sh creates.
coder:
  env:
    - name: CODER_ACCESS_URL
      value: "${CODER_URL}"
    - name: CODER_PG_CONNECTION_URL
      valueFrom: { secretKeyRef: { name: coder-db-url, key: url } }
    - name: CODER_TELEMETRY_ENABLE
      value: "false"
    - name: CODER_OAUTH2_GITHUB_DEFAULT_PROVIDER_ENABLE
      value: "false"
    # Sign-in: Keycloak only. deploy.sh opens password sign-in once, on its
    # first run, to mint the owner's deploy token (see there).
    - name: CODER_DISABLE_PASSWORD_AUTH
      value: "${DISABLE_PASSWORD_AUTH}"
    - name: CODER_OIDC_ISSUER_URL
      value: "${OIDC_ISSUER_URL}"
    - name: CODER_OIDC_CLIENT_ID
      value: "${CODER_OIDC_CLIENT}"
    - name: CODER_OIDC_CLIENT_SECRET
      valueFrom: { secretKeyRef: { name: coder-oidc, key: client-secret } }
    - name: CODER_OIDC_SIGN_IN_TEXT
      value: "Sign in with Keycloak"
    - name: CODER_OIDC_ICON_URL
      value: "/icon/key.svg"
    # Coder's sign-out ends the Coder session only; the Keycloak session
    # stays, and the next "Sign in with Keycloak" would come back as the same
    # user. prompt=login makes Keycloak ask every time, so a demo can switch
    # users (alice, then bob). access_type=offline is Coder's default, kept.
    - name: CODER_OIDC_AUTH_URL_PARAMS
      value: '{"access_type": "offline", "prompt": "login"}'
    # The same Keycloak client again, as an external auth provider: Coder
    # keeps each user's refresh token and gives their workspace a fresh access
    # token (`coder external-auth access-token keycloak`). That token is what
    # the JWT gateway checks, so it has the gateway's audience and team claim.
    - name: CODER_EXTERNAL_AUTH_0_ID
      value: "keycloak"
    - name: CODER_EXTERNAL_AUTH_0_TYPE
      value: "keycloak"
    - name: CODER_EXTERNAL_AUTH_0_DISPLAY_NAME
      value: "Keycloak (MCP hub)"
    - name: CODER_EXTERNAL_AUTH_0_DISPLAY_ICON
      value: "/icon/key.svg"
    - name: CODER_EXTERNAL_AUTH_0_CLIENT_ID
      value: "${CODER_OIDC_CLIENT}"
    - name: CODER_EXTERNAL_AUTH_0_CLIENT_SECRET
      valueFrom: { secretKeyRef: { name: coder-oidc, key: client-secret } }
    - name: CODER_EXTERNAL_AUTH_0_AUTH_URL
      value: "${OIDC_ISSUER_URL}/protocol/openid-connect/auth"
    - name: CODER_EXTERNAL_AUTH_0_TOKEN_URL
      value: "${OIDC_ISSUER_URL}/protocol/openid-connect/token"
    - name: CODER_EXTERNAL_AUTH_0_VALIDATE_URL
      value: "${OIDC_ISSUER_URL}/protocol/openid-connect/userinfo"
    - name: CODER_EXTERNAL_AUTH_0_REVOKE_URL
      value: "${OIDC_ISSUER_URL}/protocol/openid-connect/revoke"
    - name: CODER_EXTERNAL_AUTH_0_SCOPES
      value: "openid"
    - name: CODER_EXTERNAL_AUTH_0_PKCE_METHODS
      value: "S256"

  service:
    type: NodePort
    httpNodePort: "${CODER_NODE_PORT}"
    sessionAffinity: None

  serviceAccount:
    # Workspaces are plain pods in the demo's caller namespace, under its
    # `coder` service account (IRSA: Bedrock only). The server may manage
    # pods there and nothing else.
    workspacePerms: true
    enableDeployments: false
    workspaceNamespaces:
      - name: "${CALLER_NAMESPACE}"
        enableDeployments: false

  securityContext:
    capabilities: { drop: ["ALL"] }

  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits: { memory: 1536Mi }

  # The owner's password, read by deploy.sh inside the pod on its first run.
  volumes:
    - name: admin
      secret: { secretName: coder-admin }
  volumeMounts:
    - name: admin
      mountPath: /etc/coder-admin
      readOnly: true
