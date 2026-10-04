# How Kubernetes Works: one concept at a time

A hands-on walkthrough that builds a tiny app up from plain Docker to a
Kubernetes setup with self-healing, Services, config, secrets, persistence and
network policy. Every step adds **one** idea and has a "break it" moment.

The app is a **visit counter**: a Flask web app that stores its count in Redis.
Two components is the smallest setup that still shows why Kubernetes exists.

```
                    localhost:8080
                          |
                  [ Service: web ]            <- stable address + load balancing
                   /      |      \
             [web Pod] [web Pod] [web Pod]    <- Deployment keeps 3 running
                   \      |      /
                  [ Service: redis ]          <- stable name for Redis
                          |
                    [redis Pod] --- [PVC]     <- data survives Pod restarts
         (NetworkPolicy: only web Pods may connect to redis)
```

I ran everything below on **Windows 11 + PowerShell + Docker Desktop + Kind**
(1 control plane, 2 workers). Commands are PowerShell; a bash cheat-sheet is at
the bottom. The "My notes" sections are what actually happened on my machine.

## Prerequisites

- Docker Desktop (running), [kind](https://kind.sigs.k8s.io/), `kubectl`
- Windows: `winget install Kubernetes.kind` and `winget install Kubernetes.kubectl`,
  then open a **new** PowerShell window so PATH refreshes
- In PowerShell use `curl.exe` (plain `curl` is an alias for something else)

## Setup

```powershell
kind create cluster --name k8s-demo --config kind-config.yaml
docker build -t k8s-visit-counter:v1 ./app
kind load docker-image k8s-visit-counter:v1 --name k8s-demo
kubectl get nodes        # expect 3 nodes, all Ready
```

Kind runs inside Docker, so it can't see your local images until you load them.
That is why the manifests use `imagePullPolicy: IfNotPresent` and a fixed tag
(never `:latest`).

> Running low on RAM? Delete one `- role: worker` line from `kind-config.yaml`.

---

## Step 0: The "before": plain Docker

```powershell
docker compose up --build -d
curl.exe localhost:5000                              # run it 3 times
docker kill $(docker compose ps -q web)
curl.exe localhost:5000                              # connection refused
docker compose ps                                    # only redis is left
docker compose down
```

**Why it matters:** nothing restarts the container, nothing runs a second copy,
and everything lives on one machine.

> **My notes:** `visits` went 1, 2, 3. After `docker kill`, curl failed with
> "Could not connect to server" and `docker compose ps` showed only Redis.
> Nothing brought the web container back.

## Step 1: A bare Pod

```powershell
kubectl apply -f 01-pod.yaml
kubectl get pods
```

`port-forward` keeps running, so use a **second PowerShell window** for curl:

```powershell
# window 1
kubectl port-forward pod/web-pod 5000:5000
# window 2
curl.exe localhost:5000/healthz      # {"status":"ok"}
curl.exe localhost:5000              # 503: cannot reach redis
kubectl delete pod web-pod
kubectl get pods                     # No resources found
```

**Why it matters:** a Pod is the smallest unit Kubernetes runs (one or more
containers sharing a network), but a bare Pod has no one managing it.

> **My notes:** `/healthz` was fine but `/` returned an error saying it could not
> reach Redis. That's expected: Redis doesn't exist yet. `served_by` showed the
> Pod name (`web-pod`). After deleting it, nothing recreated it.

## Step 2: Deployment: self-healing and scaling

```powershell
kubectl apply -f 02-deployment.yaml
kubectl get pods -o wide
```

Watch self-healing with two windows:

```powershell
# window 1
kubectl get pods -w
# window 2: delete the first web Pod
$pod = kubectl get pods -l app=web -o name | Select-Object -First 1
kubectl delete $pod
```

Then scale, and look at the layers behind it:

```powershell
kubectl scale deployment web --replicas=4
kubectl get pods
kubectl scale deployment web --replicas=2
kubectl get deployment,replicaset,pods
```

**Why it matters:** you declare the desired state ("2 replicas") and a
controller keeps reality matching it. The Deployment owns a ReplicaSet, which
owns the Pods (that's why Pod names look like `web-<hash>-<random>`).

> **My notes:** the replacement Pod was created at the same moment the old one
> started terminating (`Pending` at 0s), was `Running 0/1` at 1s, and
> `Running 1/1` after about 6s. The 0/1 phase is the readiness probe holding
> back traffic until the app is up. The two Pods landed on different workers.

## Step 3: Services and Redis

```powershell
kubectl apply -f 03-redis-and-services.yaml
kubectl get pods,svc
```

Wait until the `redis` Pod is `1/1 Running`, **then** send traffic:

```powershell
1..6 | ForEach-Object { curl.exe -s localhost:8080; "" }
```

`served_by` alternates between Pods while `visits` keeps counting.

```powershell
# Pod IPs change, Service IPs don't
kubectl get pods -o wide
kubectl get svc web
$pod = kubectl get pods -l app=web -o name | Select-Object -First 1
kubectl delete $pod
kubectl get pods -o wide          # new Pod, new IP
kubectl get svc web               # same CLUSTER-IP

# How does "redis" resolve?
kubectl exec deploy/web -- python -c "import socket; print(socket.gethostbyname('redis'))"

# More web Pods, same count: state lives in Redis, not in the Pods
kubectl scale deployment web --replicas=3
```

**Break it:** delete the Redis Pod.

```powershell
kubectl delete pod -l app=redis
kubectl get pods                  # wait for the new redis to be 1/1
1..3 | ForEach-Object { curl.exe -s localhost:8080; "" }   # visits is back to 1
```

**Why it matters:** a Service gives a set of changing Pods one stable address
and DNS name. The reset shows containers are ephemeral, which motivates Step 6.

> **My notes:** my first curl loop ran 1 second after `apply`, while Redis was
> still `ContainerCreating`, so every response was a 503 about Redis. The Service
> itself was fine (`served_by` alternated). Once Redis was `1/1`, the count ran
> 1 to 6. A deleted web Pod's IP changed (`10.244.1.4` to `10.244.1.5`) while the
> `web` Service kept `10.96.170.79`. `redis` resolved to the Service IP
> (`10.96.12.154`). Deleting Redis reset `visits` to 1.

### What broke: CrashLoopBackOff on new web Pods

When I scaled to 3 web replicas, the new Pods went `Error`, then
`CrashLoopBackOff`, while the original Pods kept working. Readiness probes kept
traffic off the broken Pods, so the app never visibly failed.

```powershell
kubectl logs <pod> --previous
```
```
ValueError: invalid literal for int() with base 10: 'tcp://10.96.12.154:6379'
```

**Cause:** Kubernetes injects environment variables for every Service that exists
when a Pod starts. My Service is named `redis`, so it created
`REDIS_PORT=tcp://10.96.12.154:6379`, which overrode the `REDIS_PORT` my app
reads as a number. Pods created **before** the Service existed never got the
variable, which is why only some Pods crashed.

**Fix:** `enableServiceLinks: false` in the Pod spec (already in the manifests).
The app finds Redis through DNS, so it doesn't need those variables.

```powershell
kubectl exec deploy/web -- env | findstr REDIS     # only REDIS_HOST now
```

(I first suspected memory pressure. `docker stats` showed plenty of headroom, so
the logs, not my guess, found the real cause.)

## Step 4: ConfigMap and Secret

Create the Secret first (it is deliberately not stored in git). Use any
throwaway password for the demo, not a real one:

```powershell
kubectl create secret generic redis-secret --from-literal=REDIS_PASSWORD='choose-something-here'
kubectl apply -f 04-config-secret.yaml
kubectl rollout status deployment/web
kubectl rollout status deployment/redis
1..4 | ForEach-Object { curl.exe -s localhost:8080; "" }
```

See where settings come from:

```powershell
kubectl get configmap web-config -o yaml
kubectl exec deploy/web -- env | findstr "REDIS_HOST REDIS_PORT APP_TITLE"
```

Redis now needs the password:

```powershell
kubectl exec deploy/redis -- redis-cli ping                                     # NOAUTH
kubectl exec deploy/redis -- sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'   # PONG
```

A Secret is **not encrypted**, only base64-encoded. Anyone who can read Secrets
can decode it (don't screenshot this):

```powershell
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((kubectl get secret redis-secret -o jsonpath='{.data.REDIS_PASSWORD}')))
```

**Why it matters:** config lives outside the image, so the same image runs
anywhere. Real clusters add RBAC, encryption at rest, or an external store
such as Azure Key Vault.

> **My notes:** applying the file replaced the Pods gradually (the rollout log
> counted 1 of 3, 2 of 3, then "old replicas pending termination"). The page
> title changed without rebuilding the image. Without the password Redis said
> `NOAUTH Authentication required`; with it, `PONG`. `visits` reset to 1 again
> because Redis was replaced and still had no storage.

## Step 5: Rolling updates and rollbacks

```powershell
docker build --build-arg APP_VERSION=v2 -t k8s-visit-counter:v2 ./app
kind load docker-image k8s-visit-counter:v2 --name k8s-demo
```

In a **second window**, keep requests flowing (`Ctrl+C` to stop):

```powershell
while ($true) { curl.exe -s localhost:8080; ""; Start-Sleep -Milliseconds 500 }
```

In the first window:

```powershell
kubectl set image deployment/web web=k8s-visit-counter:v2
kubectl rollout status deployment/web
kubectl get replicaset
kubectl rollout history deployment/web
kubectl rollout undo deployment/web
kubectl rollout status deployment/web
```

**Why it matters:** Pods are replaced gradually, and the readiness probe keeps
traffic away from Pods that aren't ready. Old ReplicaSets are kept (scaled to 0),
which is why a rollback is fast.

**Gotcha:** `kubectl set image` and `rollout undo` change the live object but not
your YAML. Kubernetes even warns that mixing them with `kubectl apply` "may cause
unexpected behavior". In real projects the YAML in git is the source of truth.

> **My notes:** the upgrade and the rollback both went 1 of 3, 2 of 3, then old
> replicas pending termination. `kubectl get replicaset` listed every previous
> version at 0 Pods, and the history showed 4 revisions with `CHANGE-CAUSE <none>`.
> _(Add: did the request loop show any errors during the upgrade? Only claim
> "zero failed requests" if it really didn't.)_

## Step 6: Persistence with a PVC

```powershell
kubectl apply -f 06-redis-pvc.yaml
kubectl get pvc,pv
kubectl get pods
1..5 | ForEach-Object { curl.exe -s localhost:8080; "" }
kubectl delete pod -l app=redis
kubectl get pods
1..3 | ForEach-Object { curl.exe -s localhost:8080; "" }       # count continues
```

**Why it matters:** the volume lives independently of the Pod. Compare with
Step 3, where the same deletion reset the count. (A StatefulSet is the
purpose-built object for stateful workloads; this keeps it simple.)

> **My notes:** the PVC was `Pending` for about a second, then `Bound` to a 1Gi
> volume from the `standard` StorageClass. After deleting the Redis Pod, the
> replacement was `Running` within 2 seconds and the count carried on instead of
> resetting. The volume's reclaim policy is `Delete`, so it disappears with the PVC.

## Step 7: NetworkPolicy

Before the policy, any Pod can reach Redis:

```powershell
kubectl run nettest --rm -it --image=busybox:1.36 --restart=Never -- nc -zv -w 3 redis 6379
# redis (10.96.12.154:6379) open
```

Apply the policy and try again:

```powershell
kubectl apply -f 07-networkpolicy.yaml
kubectl run nettest --rm -it --image=busybox:1.36 --restart=Never -- nc -zv -w 3 redis 6379
# nc: redis (...:6379): Connection timed out
kubectl run nettest --rm -it --image=busybox:1.36 --restart=Never --labels=app=web -- nc -zv -w 3 redis 6379
# open: allowed by label, not by name or IP
1..3 | ForEach-Object { curl.exe -s localhost:8080; "" }       # the app still works
```

**Why it matters:** by default every Pod can talk to every other Pod. A
NetworkPolicy flips Redis to "only the web Pods may connect".

**If the unlabelled test Pod still connects:** your cluster's network plugin may
not enforce NetworkPolicy. Check your kind version or install Calico.

> **My notes:** before the policy `nc` said `open`; after it, the unlabelled
> Pod timed out while the app kept working. The `terminated (Error)` line after a
> timeout is just `nc` exiting non-zero. The same test Pod with `--labels=app=web`
> connected (`open`), so the policy allows by label, not by name or IP.

## Step 8: Clean up

```powershell
kind delete cluster --name k8s-demo
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ErrImagePull` / `ImagePullBackOff` | Forgot `kind load docker-image ...` |
| `CrashLoopBackOff`, log says `invalid literal for int() ... 'tcp://10.x.x.x:6379'` | Service env-var injection overriding `REDIS_PORT`. Use `enableServiceLinks: false` (see Step 3) |
| Other `CrashLoopBackOff` | `kubectl logs <pod> --previous` and `kubectl describe pod <pod>` |
| `CreateContainerConfigError` | Secret `redis-secret` doesn't exist yet |
| PVC stuck `Pending` | `kubectl get storageclass`; Kind ships a default |
| `curl.exe localhost:8080` refused | Cluster wasn't created with `kind-config.yaml` |
| `The '<' operator is reserved` in PowerShell | You pasted a `<placeholder>` literally; use a real name |
| 503 right after `apply` | Dependency (Redis) isn't `1/1` yet; wait and retry |

## Bash cheat-sheet (macOS / Linux)

| PowerShell | bash |
|---|---|
| `curl.exe` | `curl` |
| `1..6 \| ForEach-Object { curl.exe -s localhost:8080; "" }` | `for i in $(seq 6); do curl -s localhost:8080; echo; done` |
| `$pod = kubectl get pods -l app=web -o name \| Select-Object -First 1; kubectl delete $pod` | `kubectl delete $(kubectl get pods -l app=web -o name \| head -1)` |
| `findstr REDIS` | `grep REDIS` |
| `[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(...))` | `... \| base64 -d` |
| `while ($true) { ...; Start-Sleep -Milliseconds 500 }` | `while true; do ...; sleep 0.5; done` |

## What I learned

- A Pod is disposable; a Deployment is what makes it self-healing.
- Pod IPs change; Services give stable names and load balancing.
- Probes decide who receives traffic, which hid a crashing Pod from users.
- Reading `logs --previous` beats guessing.
- Secrets are encoded, not encrypted.
- Persistence needs a volume; the Pod alone loses data.
- NetworkPolicy is allow-by-label, and worth testing from both sides.

## What next

- Ingress instead of NodePort
- HorizontalPodAutoscaler
- StatefulSet for Redis
- Package it with Helm
- Run the same manifests on AKS, with the cluster defined in Bicep and deployed
  by GitHub Actions
