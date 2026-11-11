# Cluster A: the calling application and a negative-case neighbour, each with
# its own IRSA role. Rendered by deploy.sh. The client code arrives as the
# frontend-client ConfigMap (created by deploy.sh from client/).
apiVersion: v1
kind: Namespace
metadata:
  name: ${FRONTEND_NAMESPACE}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${FRONTEND_SA}
  namespace: ${FRONTEND_NAMESPACE}
  annotations:
    eks.amazonaws.com/role-arn: ${FRONTEND_ROLE_ARN}
    eks.amazonaws.com/sts-regional-endpoints: "true"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${INTRUDER_SA}
  namespace: ${FRONTEND_NAMESPACE}
  annotations:
    eks.amazonaws.com/role-arn: ${INTRUDER_ROLE_ARN}
    eks.amazonaws.com/sts-regional-endpoints: "true"
---
apiVersion: v1
kind: Pod
metadata:
  name: frontend
  namespace: ${FRONTEND_NAMESPACE}
  labels: { app: frontend }
spec:
  serviceAccountName: ${FRONTEND_SA}
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
        - { name: ENTRY_URL, value: "${ENTRY_URL}" }
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
      configMap: { name: frontend-client }
---
apiVersion: v1
kind: Pod
metadata:
  name: intruder
  namespace: ${FRONTEND_NAMESPACE}
  labels: { app: intruder }
spec:
  serviceAccountName: ${INTRUDER_SA}
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
        - { name: ENTRY_URL, value: "${ENTRY_URL}" }
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
      configMap: { name: frontend-client }
