# Keycloak's database.
#
# Keycloak ran in dev mode on an in-memory H2 database until 2026-08-19. That
# made every container replacement a silent SSO outage: the ECS task was
# retired after nine days, the fresh one re-imported the realm from the image,
# and everything applied *after* the import was gone — the portal's redirect
# URI, the confidential clients' secrets, the user passwords, all of it. Login
# failed with `invalid_redirect_uri` and the robot client with
# `invalid_client_credentials` until scripts/seed_team_idp.py was re-run by hand.
#
# With an external database that state is durable, and so are user sessions:
# Keycloak 26 keeps `persistent-user-session` on by default, so sessions live in
# the database rather than only in the local Infinispan cache. A pod
# replacement no longer signs everyone out, which is what makes the realm's
# 7-day SSO session mean anything.

resource "aws_db_subnet_group" "keycloak" {
  name       = "agent-platform-keycloak${var.name_suffix}"
  subnet_ids = var.private_subnet_ids
}

# A client SG of its own rather than reusing aws_security_group.service: that
# one is shared with the three team APIs, and they have no business reaching
# the database. The Keycloak pod carries both SGs (its SecurityGroupPolicy).
resource "aws_security_group" "keycloak_db_client" {
  name        = "agent-platform-keycloak-db-client${var.name_suffix}"
  description = "Marks the Keycloak task as a client of its database"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "keycloak_db" {
  name        = "agent-platform-keycloak-db${var.name_suffix}"
  description = "Keycloak database - reachable only from the Keycloak task"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from the Keycloak task"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.keycloak_db_client.id]
  }
}

# Alphanumeric: the value travels through a container environment variable into
# a JDBC URL, and RDS rejects '/', '@', '"' and '\'' in a master password
# anyway. 40 characters of base62 is ~238 bits, so dropping punctuation costs
# nothing that matters.
resource "random_password" "keycloak_db" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "keycloak_db" {
  name        = "agent-platform/keycloak-db${var.name_suffix}"
  description = "Keycloak PostgreSQL credentials (team-auth demo IdP)"
}

resource "aws_secretsmanager_secret_version" "keycloak_db" {
  secret_id = aws_secretsmanager_secret.keycloak_db.id
  secret_string = jsonencode({
    username = local.db_username
    password = random_password.keycloak_db.result
    dbname   = local.db_name
    host     = aws_db_instance.keycloak.address
    port     = aws_db_instance.keycloak.port
  })
}

# RDS would create this itself on first log export, but without a retention
# policy — i.e. it would keep Postgres logs forever. Creating it here pins the
# same 7 days the container log groups use.
resource "aws_cloudwatch_log_group" "keycloak_db" {
  name              = "/aws/rds/instance/agent-platform-keycloak${var.name_suffix}/postgresql"
  retention_in_days = 7
}

resource "aws_db_instance" "keycloak" {
  identifier     = "agent-platform-keycloak${var.name_suffix}"
  engine         = "postgres"
  instance_class = "db.t4g.micro"

  # Major version only, so AWS picks the current default minor and
  # auto_minor_version_upgrade can move it during the maintenance window
  # without Terraform reporting a drift every time.
  engine_version             = "17"
  auto_minor_version_upgrade = true

  db_name  = local.db_name
  username = local.db_username
  password = random_password.keycloak_db.result

  db_subnet_group_name   = aws_db_subnet_group.keycloak.name
  vpc_security_group_ids = [aws_security_group.keycloak_db.id]
  publicly_accessible    = false

  # Single-AZ is deliberate for a demo IdP: it halves the bill, and the failure
  # it exposes (a few minutes of downtime during a maintenance window) is both
  # rarer and milder than the one being fixed here, since sessions now survive
  # a restart. Set multi_az = true if the demo needs to ride out an AZ event.
  multi_az = false

  storage_type      = "gp3"
  allocated_storage = 20
  # Autoscale rather than let a full disk take the IdP down; nothing in this
  # workload should ever approach it.
  max_allocated_storage = 100
  storage_encrypted     = true

  backup_retention_period = 7
  backup_window           = "17:00-18:00" # 02:00-03:00 JST
  maintenance_window      = "Mon:18:00-Mon:19:00"
  copy_tags_to_snapshot   = true

  # The realm structure re-imports from the image, but the seeded credentials
  # and client secrets do not: losing this volume means another manual re-seed.
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "agent-platform-keycloak-final${var.name_suffix}"

  enabled_cloudwatch_logs_exports = ["postgresql"]

  depends_on = [aws_cloudwatch_log_group.keycloak_db]
}

locals {
  db_name     = "keycloak"
  db_username = "keycloak"

  # rds.force_ssl is 1 in the default postgres17 parameter group, so an
  # unencrypted connection is refused outright. `require` encrypts without
  # pinning the CA; verify-full would additionally need the RDS CA bundle in
  # the image truststore and a plan for rotating it.
  db_url_properties = "?sslmode=require"
}
