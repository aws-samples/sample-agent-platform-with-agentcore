# Keycloak IdP for the team-auth demo: production mode against RDS PostgreSQL.
#
# The realm (groups team-a/b/c + platform-admin, the four demo users, the
# portal-web / gateway-delegate / robot-order-service clients and their `team`
# claim mappers) is baked in and imported on first boot. Credentials are NOT in
# the image: scripts/seed_team_idp.py sets user passwords, the confidential
# clients' secrets and the deployed portal's redirect URI, and stores them in
# Secrets Manager.
#
# Runtime env (set by the team_auth Terraform module):
#   KC_BOOTSTRAP_ADMIN_USERNAME / KC_BOOTSTRAP_ADMIN_PASSWORD
#   KC_HOSTNAME (the https CloudFront URL), KC_HTTP_ENABLED, KC_PROXY_HEADERS
#   KC_DB_URL_HOST / KC_DB_URL_PORT / KC_DB_URL_DATABASE / KC_DB_URL_PROPERTIES
#   KC_DB_USERNAME / KC_DB_PASSWORD
#
# Why production mode and an external database (changed 2026-08-19): dev mode
# keeps everything in an in-memory H2 database, so a task replacement silently
# reset the realm to this file. AWS retired the Fargate task on its own after
# nine days and SSO broke for four hours: the portal's redirect URI and the
# robot client's secret were gone, and every user session with them. On
# PostgreSQL that state is durable, and Keycloak 26 leaves
# `persistent-user-session` enabled by default, so sessions are stored in the
# database and a restart no longer signs everyone out.
#
# Realm-file caveats (Keycloak 26.3.x imports are STRICT — any unknown JSON
# field aborts the boot, so keep commentary here, not in the realm file):
# - portal-web redirectUris ship as localhost only; Keycloak matches wildcards
#   at the END of a URI (https://*.cloudfront.net/* does NOT work), so
#   scripts/seed_team_idp.py registers the deployed portal's real origin.
# - the robot-order-service service-account user's group membership is also
#   applied by the seed script — a realm import cannot express it.
# - sessions run 7 days (ssoSessionIdleTimeout/ssoSessionMaxLifespan = 604800)
#   while accessTokenLifespan stays at 1 hour. The portal renews the short
#   access token from its refresh token, so the long value only sets how long a
#   signed-in user goes without re-authenticating, not how long a leaked bearer
#   token stays usable downstream.
# - the import runs with Keycloak's default IGNORE_EXISTING strategy. Against a
#   persistent database that means it applies once, on the first boot; later
#   edits to the realm file do NOT reach an existing realm. Apply those through
#   the admin API (as the seed script does) or a partial import.

# ---------------------------------- build ----------------------------------
# Production mode wants an optimized image: the build-time options below are
# resolved once by `kc.sh build` here instead of on every container start.
# nosemgrep: dockerfile-source-not-pinned
FROM quay.io/keycloak/keycloak:26.3 AS builder

ENV KC_DB=postgres
ENV KC_HEALTH_ENABLED=true
ENV KC_METRICS_ENABLED=true

RUN /opt/keycloak/bin/kc.sh build

# ----------------------------------- run -----------------------------------
# nosemgrep: dockerfile-source-not-pinned
FROM quay.io/keycloak/keycloak:26.3
# checkov:skip=CKV_DOCKER_2: health is the ALB target-group check on /realms/master.
# checkov:skip=CKV_DOCKER_3: official Keycloak image already runs as user 1000.

COPY --from=builder /opt/keycloak/ /opt/keycloak/
COPY realm-agent-platform.json /opt/keycloak/data/import/realm-agent-platform.json

# --optimized skips the augmentation step at startup: the build above already
# did it, and re-running it would only slow every boot down.
CMD ["start", "--optimized", "--import-realm"]
