#!/usr/bin/env bash
# One-command deploy of LoanTrack to a local minikube cluster.
#
#   ./scripts/deploy.sh            (or: bash scripts/deploy.sh)
#
# From an empty minikube to a working app: builds both images with the host's
# Docker, loads them into minikube (minikube image load), creates the namespace,
# config, migrations ConfigMap and DB Secret, applies the manifests in
# dependency order, waits for every rollout and prints how to open the app.
# Safe to run again (idempotent).
#
# This script is minikube-only. The cluster is always the 'minikube' context;
# KUBE_CONTEXT is not an override and any other value is rejected.
#
# Environment overrides:
#   IMAGE_TAG        tag for both images          (default: v1)
#   ROLLOUT_TIMEOUT  per-rollout wait             (default: 240s)

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-minikube}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-240s}"
NAMESPACE="loantrack"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "ERROR: deploy failed at line %s (command: %s)\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

# Every kubectl call names the context explicitly, so this script can never
# touch any other cluster configured on the machine.
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }

# ---------------------------------------------------------------- preflight
log "Checking prerequisites"
for tool in minikube kubectl docker; do
  command -v "$tool" >/dev/null 2>&1 || fail "'$tool' is not installed or not on PATH"
done
[ "$KUBE_CONTEXT" = "minikube" ] || fail "this script builds images with minikube; KUBE_CONTEXT must be 'minikube' (got '$KUBE_CONTEXT')"
for f in k8s/namespace.yaml k8s/configmap.yaml k8s/postgres.yaml k8s/backend.yaml k8s/frontend.yaml \
         db/migrations/001_create_loans.sql db/migrations/002_seed_loans.sql \
         backend/Dockerfile frontend/Dockerfile; do
  [ -f "$f" ] || fail "missing required file: $f"
done
docker info >/dev/null 2>&1 || fail "Docker is not running"

if [ "$(minikube status --format '{{.Host}}' 2>/dev/null || true)" != "Running" ]; then
  log "minikube is not running - starting it"
  minikube start --driver=docker
fi
minikube update-context >/dev/null
k get nodes >/dev/null || fail "cannot reach the cluster through context '$KUBE_CONTEXT'"

# ---------------------------------------------------------------- images
# Built with the host's Docker, then copied into minikube's container runtime,
# so the cluster uses them without any registry (imagePullPolicy: Never).
# (`minikube image build` was not used: with the containerd runtime it sent an
# empty build context from Windows and still exited 0.) The presence check
# below does not trust exit codes.
build_and_load() {
  local image="$1" context="$2"; shift 2
  docker build -t "$image" "$@" "$context"
  minikube image load "$image"
  minikube image ls | grep -q "$image\$" || fail "image $image was not loaded into minikube"
  echo "loaded into minikube: $image"
}
log "Building images and loading them into minikube (tag: $IMAGE_TAG)"
build_and_load "loantrack-backend:$IMAGE_TAG" backend --build-arg "APP_VERSION=$IMAGE_TAG"
build_and_load "loantrack-frontend:$IMAGE_TAG" frontend

# The manifests name the default tag (:v1). Substitute IMAGE_TAG at apply time so
# the cluster runs the tag that was just built. When the tag changes, Kubernetes
# performs the rolling update by itself. The files on disk are not changed.
apply_tagged() {
  sed -e "s|loantrack-backend:v1|loantrack-backend:$IMAGE_TAG|" \
      -e "s|loantrack-frontend:v1|loantrack-frontend:$IMAGE_TAG|" "$1" | k apply -f -
}

# ---------------------------------------------------------------- config
log "Namespace and configuration"
k apply -f k8s/namespace.yaml
k apply -f k8s/configmap.yaml

# Schema + seed SQL from db/migrations/ (single source of truth), mounted into
# the postgres init directory.
k -n "$NAMESPACE" create configmap postgres-migrations --from-file=db/migrations \
  --dry-run=client -o yaml | k apply -f -

log "Database Secret"
if [ -f k8s/secret.yaml ]; then
  echo "using k8s/secret.yaml (git-ignored)"
  k apply -f k8s/secret.yaml
elif k -n "$NAMESPACE" get secret loantrack-db >/dev/null 2>&1; then
  echo "Secret loantrack-db already exists - keeping it (the database was initialised with it)"
else
  echo "no k8s/secret.yaml and no existing Secret - generating a random password"
  # Piped through stdin: the password never appears on screen, in a file or in
  # the process list.
  password="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: loantrack-db\n  namespace: %s\ntype: Opaque\nstringData:\n  POSTGRES_USER: loantrack_user\n  POSTGRES_PASSWORD: "%s"\n' \
    "$NAMESPACE" "$password" | k apply -f -
  unset password
fi

# ---------------------------------------------------------------- workloads
log "PostgreSQL (StatefulSet)"
k apply -f k8s/postgres.yaml
k -n "$NAMESPACE" rollout status statefulset/postgres --timeout="$ROLLOUT_TIMEOUT"

log "Backend"
apply_tagged k8s/backend.yaml
k -n "$NAMESPACE" rollout status deployment/backend --timeout="$ROLLOUT_TIMEOUT"

log "Frontend"
apply_tagged k8s/frontend.yaml
k -n "$NAMESPACE" rollout status deployment/frontend --timeout="$ROLLOUT_TIMEOUT"

# ---------------------------------------------------------------- done
log "Deployed"
k -n "$NAMESPACE" get pods,svc -o wide

node_ip="$(minikube ip)"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*|Darwin*)
    # With the docker driver on Windows/macOS the node IP is not routable from
    # the host; minikube opens a tunnel and prints a localhost URL.
    printf '\nOpen the app (keep that terminal open while you use it):\n  minikube service frontend -n %s\n' "$NAMESPACE"
    printf 'or:\n  kubectl --context %s -n %s port-forward svc/frontend 8081:80   ->  http://localhost:8081\n' "$KUBE_CONTEXT" "$NAMESPACE"
    ;;
  *)
    printf '\nApp URL: http://%s:30080\n' "$node_ip"
    ;;
esac
