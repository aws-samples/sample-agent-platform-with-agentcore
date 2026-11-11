# Cluster B: the demo hub as a pod, reachable only through the NLB.
# Rendered by deploy.sh through render.py (the dollar-brace variables come from
# terraform output and are validated before anything is written). The hub's code
# and config arrive as ConfigMaps (hub-code is created by deploy.sh from
# services/mcp-hub-demo/hub); the image is the public python base image, so
# nothing has to be built or pushed for this test.
apiVersion: v1
kind: Namespace
metadata:
  name: mcp-hub
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: hub-config
  namespace: mcp-hub
data:
  config.yaml: |
    # Application-only, pod-to-pod: no IdP in this test, so no backends and
    # no user token. The issuer is never contacted (a user token would be
    # verified against it, and none is sent).
    resource_url: http://mcp-hub.internal/mcp
    issuer: http://idp.invalid/realms/none
    servers: []
    entry_identity:
      trust: network            # no shared secret: the NetworkPolicy below is the trust anchor
      require_user_token: false
      callers:
        - role_arn: ${FRONTEND_ROLE_ARN}
          actor: frontend-app
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hub
  namespace: mcp-hub
spec:
  replicas: 1
  selector:
    matchLabels: { app: hub }
  template:
    metadata:
      labels: { app: hub }
    spec:
      automountServiceAccountToken: false # the hub needs no Kubernetes or AWS identity
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: hub
          image: public.ecr.aws/docker/library/python:3.12-slim
          command: ["sh", "-c"]
          args:
            - pip install --quiet --no-cache-dir --target /tmp/deps -r /app/requirements.txt && exec python /app/app.py
          env:
            - { name: PYTHONPATH, value: /tmp/deps }
            - { name: HOME, value: /tmp }
            - { name: HUB_CONFIG, value: /etc/hub/config.yaml }
            - { name: PORT, value: "8000" }
          ports:
            - { containerPort: 8000, name: mcp }
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: code, mountPath: /app, readOnly: true }
            - { name: config, mountPath: /etc/hub, readOnly: true }
          resources:
            requests: { cpu: 100m, memory: 192Mi }
            limits: { memory: 512Mi }
      volumes:
        - name: code
          configMap: { name: hub-code }
        - name: config
          configMap: { name: hub-config }
---
# NodePort with externalTrafficPolicy Local: the NLB targets the node on
# the hub NodePort, and Local keeps the NLB's source address on the packet
# (Cluster would SNAT it to the node), which is what the NetworkPolicy keys on.
apiVersion: v1
kind: Service
metadata:
  name: hub
  namespace: mcp-hub
spec:
  type: NodePort
  externalTrafficPolicy: Local
  selector: { app: hub }
  ports:
    - { port: 8000, targetPort: 8000, nodePort: ${HUB_NODE_PORT}, protocol: TCP }
---
# The trust anchor of `trust: network`: the hub accepts connections from the
# NLB's subnets and nothing else - not from other pods, not from the node.
# Enforced by the VPC CNI network policy agent (enabled in the cluster module).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: hub-only-from-nlb
  namespace: mcp-hub
spec:
  podSelector:
    matchLabels: { app: hub }
  policyTypes: ["Ingress"]
  ingress:
    - from:
${HUB_LB_CIDR_BLOCKS}
      ports:
        - { port: 8000, protocol: TCP }
---
# Negative-test pod in the hub's own cluster: same VPC, same node, no way in.
apiVersion: v1
kind: Pod
metadata:
  name: neighbour
  namespace: mcp-hub
  labels: { app: neighbour }
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: shell
      image: public.ecr.aws/docker/library/python:3.12-slim
      command: ["sleep", "infinity"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: ["ALL"] }
      resources:
        requests: { cpu: 10m, memory: 32Mi }
