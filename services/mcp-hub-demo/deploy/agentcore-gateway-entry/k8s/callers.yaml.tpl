# Caller cluster: the application pods of the demo, each with its own IRSA
# role, and the service account the Coder workspaces run under.
#
#   static-app  an application with no user: SigV4 with its role to the IAM
#               gateway.
#   intruder    same IAM grant as static-app, no Cedar permit.
#   ws-probe    stands in for a Coder workspace in verify.sh: a pod on the
#               workspace pool (the subnet the JWT gateway's endpoint admits)
#               that holds no credential of its own. Its service account has
#               no IAM role; verify.sh pipes it a user's token on stdin.
#   coder       (service account only) the Coder workspaces' identity, made
#               by coder/ from its template: Claude Code calls Amazon Bedrock
#               with this role; tools go through the JWT gateway with the token
#               of whoever signed in to Coder. The role has no grant on either
#               gateway.
#
# static-app and intruder run on the app pool and ws-probe on the workspace
# pool: a node group each, in a subnet of its own, the range one gateway's VPC
# endpoint admits (../terraform/endpoints.tf).
#
# Rendered by deploy.sh. Client code arrives as the gw-client ConfigMap
# (client/).
apiVersion: v1
kind: Namespace
metadata:
  name: ${CALLER_NAMESPACE}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: static-app
  namespace: ${CALLER_NAMESPACE}
  annotations:
    eks.amazonaws.com/role-arn: ${STATIC_APP_ROLE_ARN}
    eks.amazonaws.com/sts-regional-endpoints: "true"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: intruder
  namespace: ${CALLER_NAMESPACE}
  annotations:
    eks.amazonaws.com/role-arn: ${INTRUDER_ROLE_ARN}
    eks.amazonaws.com/sts-regional-endpoints: "true"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: coder
  namespace: ${CALLER_NAMESPACE}
  annotations:
    eks.amazonaws.com/role-arn: ${CODER_ROLE_ARN}
    eks.amazonaws.com/sts-regional-endpoints: "true"
---
apiVersion: v1
kind: Pod
metadata:
  name: static-app
  namespace: ${CALLER_NAMESPACE}
  labels: { app: static-app }
spec:
  serviceAccountName: static-app
  nodeSelector: { mcp-hub-demo/pool: app }
  tolerations:
    - { key: mcp-hub-demo/pool, operator: Equal, value: app, effect: NoSchedule }
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: client
      image: public.ecr.aws/docker/library/python:3.12-slim
      command: ["sh", "-c"]
      args:
        - pip install --quiet --no-cache-dir --target /tmp/deps "boto3>=1.40" && touch /tmp/ready && exec sleep infinity
      env:
        - { name: PYTHONPATH, value: /tmp/deps }
        - { name: HOME, value: /tmp }
        - { name: AWS_REGION, value: "${REGION}" }
        - { name: IAM_GATEWAY_URL, value: "${IAM_GATEWAY_URL}" }
        - { name: JWT_GATEWAY_URL, value: "${JWT_GATEWAY_URL}" }
      readinessProbe:
        exec: { command: ["test", "-f", "/tmp/ready"] }
        periodSeconds: 3
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: ["ALL"] }
      volumeMounts:
        - { name: client, mountPath: /client, readOnly: true }
      resources:
        requests: { cpu: 50m, memory: 128Mi }
  volumes:
    - name: client
      configMap: { name: gw-client }
---
apiVersion: v1
kind: Pod
metadata:
  name: intruder
  namespace: ${CALLER_NAMESPACE}
  labels: { app: intruder }
spec:
  serviceAccountName: intruder
  nodeSelector: { mcp-hub-demo/pool: app }
  tolerations:
    - { key: mcp-hub-demo/pool, operator: Equal, value: app, effect: NoSchedule }
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: client
      image: public.ecr.aws/docker/library/python:3.12-slim
      command: ["sh", "-c"]
      args:
        - pip install --quiet --no-cache-dir --target /tmp/deps "boto3>=1.40" && touch /tmp/ready && exec sleep infinity
      env:
        - { name: PYTHONPATH, value: /tmp/deps }
        - { name: HOME, value: /tmp }
        - { name: AWS_REGION, value: "${REGION}" }
        - { name: IAM_GATEWAY_URL, value: "${IAM_GATEWAY_URL}" }
        - { name: JWT_GATEWAY_URL, value: "${JWT_GATEWAY_URL}" }
      readinessProbe:
        exec: { command: ["test", "-f", "/tmp/ready"] }
        periodSeconds: 3
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: ["ALL"] }
      volumeMounts:
        - { name: client, mountPath: /client, readOnly: true }
      resources:
        requests: { cpu: 50m, memory: 128Mi }
  volumes:
    - name: client
      configMap: { name: gw-client }
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ws-probe
  namespace: ${CALLER_NAMESPACE}
automountServiceAccountToken: false
---
apiVersion: v1
kind: Pod
metadata:
  name: ws-probe
  namespace: ${CALLER_NAMESPACE}
  labels: { app: ws-probe }
spec:
  serviceAccountName: ws-probe
  automountServiceAccountToken: false
  nodeSelector: { mcp-hub-demo/pool: workspace }
  tolerations:
    - { key: mcp-hub-demo/pool, operator: Equal, value: workspace, effect: NoSchedule }
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: client
      image: public.ecr.aws/docker/library/python:3.12-slim
      command: ["sh", "-c"]
      args:
        - touch /tmp/ready && exec sleep infinity
      env:
        - { name: HOME, value: /tmp }
        - { name: JWT_GATEWAY_URL, value: "${JWT_GATEWAY_URL}" }
      readinessProbe:
        exec: { command: ["test", "-f", "/tmp/ready"] }
        periodSeconds: 3
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: ["ALL"] }
      volumeMounts:
        - { name: client, mountPath: /client, readOnly: true }
      resources:
        requests: { cpu: 50m, memory: 64Mi }
  volumes:
    - name: client
      configMap: { name: gw-client }
