# Keycloak IdP for the team-auth demo (test-grade: dev mode, in-memory H2).
# The realm (groups team-a/team-b, users alice/bob, portal-web client with a
# `team` claim mapper) is baked in and imported at boot. User passwords are
# NOT in the image — scripts/seed_team_idp.py sets them after deploy and
# stores them in Secrets Manager.
#
# Runtime env (set by TeamAuthStack): KC_BOOTSTRAP_ADMIN_USERNAME,
# KC_BOOTSTRAP_ADMIN_PASSWORD, KC_HOSTNAME (https CloudFront URL),
# KC_HTTP_ENABLED, KC_PROXY_HEADERS, KC_HEALTH_ENABLED.
# nosemgrep: dockerfile-source-not-pinned
FROM quay.io/keycloak/keycloak:26.3
# checkov:skip=CKV_DOCKER_2: health is the ALB target-group check on /realms/master.
# checkov:skip=CKV_DOCKER_3: official Keycloak image already runs as user 1000.

COPY realm-agent-platform.json /opt/keycloak/data/import/realm-agent-platform.json

# Dev mode is deliberate: this IdP exists to demonstrate the auth chain in a
# test environment. Production would use `start` + external DB + replicas.
CMD ["start-dev", "--import-realm"]
