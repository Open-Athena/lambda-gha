#!/bin/bash
set -e

# This script is fetched and executed by the minimal userdata script
# All variables are already exported by the userdata script

# Lambda API configuration (defined early for error handling before shared functions load)
LAMBDA_API_HOST="${LAMBDA_API_HOST:-cloud.lambda.ai}"
LAMBDA_API_BASE="${LAMBDA_API_BASE:-https://$LAMBDA_API_HOST/api/v1}"

# Enable debug tracing to a file for troubleshooting
exec 2> >(tee -a /var/log/runner-debug.log >&2)

# Conditionally enable debug mode (set -x) for tracing
# Debug can be: true/True/trace (trace only), or a number (trace + sleep minutes)
if [ "$debug" = "true" ] || [ "$debug" = "True" ] || [ "$debug" = "trace" ] || [[ "$debug" =~ ^[0-9]+$ ]]; then
  set -x
fi

# Lambda instances are Ubuntu-based with ubuntu user
homedir="${homedir:-/home/ubuntu}"
export homedir

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using homedir: $homedir" | tee -a /var/log/runner-setup.log

# Set common paths
BIN_DIR=/usr/local/bin
RUNNER_STATE_DIR=/var/run/github-runner
mkdir -p $RUNNER_STATE_DIR

# Get shared functions - from local dir if available (private repo), else fetch from GitHub
SCRIPTS_DIR="${SCRIPTS_DIR:-}"
if [ -n "$SCRIPTS_DIR" ] && [ -f "$SCRIPTS_DIR/shared-functions.sh" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using local shared functions from $SCRIPTS_DIR" | tee -a /var/log/runner-setup.log
  cp "$SCRIPTS_DIR/shared-functions.sh" /tmp/shared-functions.sh
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Fetching shared functions from GitHub (SHA: ${action_sha})" | tee -a /var/log/runner-setup.log
  FUNCTIONS_URL="https://raw.githubusercontent.com/Open-Athena/lambda-gha/${action_sha}/src/lambda_gha/templates/shared-functions.sh"
  if ! curl -sSL "$FUNCTIONS_URL" -o /tmp/shared-functions.sh && ! wget -q "$FUNCTIONS_URL" -O /tmp/shared-functions.sh; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: Failed to download shared functions" | tee -a /var/log/runner-setup.log
    # Terminate via Lambda API
    curl -s -X POST -H "Authorization: Bearer $LAMBDA_API_KEY" -H "Content-Type: application/json" \
      -d "{\"instance_ids\": [\"$LAMBDA_INSTANCE_ID\"]}" \
      "$LAMBDA_API_BASE/instance-operations/terminate" || true
    exit 1
  fi
fi

# Write shared functions that will be used by multiple scripts
cat > $BIN_DIR/runner-common.sh << EOSF
# Auto-generated shared functions and variables
homedir="$homedir"
debug="$debug"
RUNNER_STATE_DIR="$RUNNER_STATE_DIR"
LAMBDA_API_KEY="$LAMBDA_API_KEY"
LAMBDA_INSTANCE_ID="$LAMBDA_INSTANCE_ID"
LAMBDA_INSTANCE_IP="$LAMBDA_INSTANCE_IP"
export homedir debug RUNNER_STATE_DIR LAMBDA_API_KEY LAMBDA_INSTANCE_ID LAMBDA_INSTANCE_IP

EOSF

# Append the downloaded shared functions
cat /tmp/shared-functions.sh >> $BIN_DIR/runner-common.sh

chmod +x $BIN_DIR/runner-common.sh
source $BIN_DIR/runner-common.sh

logger "lambda-gha: Starting userdata script"
trap 'logger "lambda-gha: Script failed at line $LINENO with exit code $?"' ERR
trap 'terminate_instance "Setup script failed with error on line $LINENO"' ERR
# Handle watchdog termination signal
trap 'if [ -f $RUNNER_STATE_DIR/watchdog-terminate ]; then terminate_instance "No runners registered within timeout"; else terminate_instance "Script terminated"; fi' TERM

# Set up registration timeout failsafe - terminate if runner doesn't register in time
REGISTRATION_TIMEOUT="$runner_registration_timeout"
if ! [[ "$REGISTRATION_TIMEOUT" =~ ^[0-9]+$ ]]; then
  REGISTRATION_TIMEOUT=300
fi
# Create a marker file for watchdog termination request
touch $RUNNER_STATE_DIR/watchdog-active
(
  sleep $REGISTRATION_TIMEOUT
  if [ ! -f $RUNNER_STATE_DIR/registered ]; then
    touch $RUNNER_STATE_DIR/watchdog-terminate
    kill -TERM $$ 2>/dev/null || true
  fi
  rm -f $RUNNER_STATE_DIR/watchdog-active
) &
REGISTRATION_WATCHDOG_PID=$!
echo $REGISTRATION_WATCHDOG_PID > $RUNNER_STATE_DIR/watchdog.pid

# Run any custom user data script provided by the user
if [ -n "$userdata" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running custom userdata" | tee -a /var/log/runner-setup.log
  eval "$userdata"
fi

exec >> /var/log/runner-setup.log 2>&1
log "Starting runner setup"

# Lambda instance info from environment (no metadata service)
INSTANCE_ID="${LAMBDA_INSTANCE_ID:-unknown}"
INSTANCE_IP="${LAMBDA_INSTANCE_IP:-unknown}"
log "Lambda instance: ID=${INSTANCE_ID} IP=${INSTANCE_IP}"

# Set up maximum lifetime timeout - instance will terminate after this time regardless of job status
MAX_LIFETIME_MINUTES=$max_instance_lifetime
log "Setting up maximum lifetime timeout: ${MAX_LIFETIME_MINUTES} minutes"
nohup bash -c "
  sleep ${MAX_LIFETIME_MINUTES}m
  echo '[$(date)] Maximum lifetime reached' 2>/dev/null || true
  # Terminate via Lambda API
  curl -s -X POST -H 'Authorization: Bearer $LAMBDA_API_KEY' -H 'Content-Type: application/json' \
    -d '{\"instance_ids\": [\"$LAMBDA_INSTANCE_ID\"]}' \
    '$LAMBDA_API_BASE/instance-operations/terminate' || true
" > /var/log/max-lifetime.log 2>&1 &

log "Working directory: $homedir"
cd "$homedir"

export RUNNER_ALLOW_RUNASROOT=1

# Download GitHub Actions runner binary
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
  RUNNER_URL=$(echo "$runner_release" | sed 's/x64/arm64/g')
  log "ARM detected, using: $RUNNER_URL"
else
  RUNNER_URL="$runner_release"
  log "x64 detected, using: $RUNNER_URL"
fi

if command -v curl >/dev/null 2>&1; then
  curl -L $RUNNER_URL -o /tmp/runner.tar.gz
elif command -v wget >/dev/null 2>&1; then
  wget -q $RUNNER_URL -O /tmp/runner.tar.gz
else
  log_error "Neither curl nor wget found. Cannot download runner."
  terminate_instance "No download tool available"
fi
log "Downloaded runner binary"

# Helper function to fetch scripts - uses local copy if available, else downloads
fetch_script() {
  local script_name="$1"
  local dest="${BIN_DIR}/${script_name}"

  # Check for local copy first (private repo support)
  if [ -n "$SCRIPTS_DIR" ] && [ -f "$SCRIPTS_DIR/$script_name" ]; then
    cp "$SCRIPTS_DIR/$script_name" "$dest"
    return 0
  fi

  # Fall back to downloading from GitHub
  local url="${BASE_URL}/${script_name}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest" || {
      log_error "Failed to fetch $script_name"
      terminate_instance "Failed to download $script_name"
    }
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$dest" || {
      log_error "Failed to fetch $script_name"
      terminate_instance "Failed to download $script_name"
    }
  else
    log_error "Neither curl nor wget found. Cannot download scripts."
    terminate_instance "No download tool available"
  fi
}

# Fetch job tracking scripts - from local if available (private repo), else GitHub
if [ -n "$SCRIPTS_DIR" ]; then
  log "Copying runner hook scripts from local $SCRIPTS_DIR"
else
  log "Fetching runner hook scripts from GitHub"
fi
BASE_URL="https://raw.githubusercontent.com/Open-Athena/lambda-gha/${action_sha}/src/lambda_gha/scripts"

fetch_script "job-started-hook.sh"
fetch_script "job-completed-hook.sh"
fetch_script "check-runner-termination.sh"

# Replace log prefix placeholders with actual values
sed -i "s/LOG_PREFIX_JOB_STARTED/${log_prefix_job_started}/g" $BIN_DIR/job-started-hook.sh
sed -i "s/LOG_PREFIX_JOB_COMPLETED/${log_prefix_job_completed}/g" $BIN_DIR/job-completed-hook.sh

chmod +x $BIN_DIR/job-started-hook.sh $BIN_DIR/job-completed-hook.sh $BIN_DIR/check-runner-termination.sh

# Set up job tracking directory
mkdir -p $RUNNER_STATE_DIR/jobs
touch $RUNNER_STATE_DIR/last-activity

# Set up periodic termination check using systemd
cat > /etc/systemd/system/runner-termination-check.service << EOF
[Unit]
Description=Check GitHub runner termination conditions
After=network.target
[Service]
Type=oneshot
Environment="RUNNER_GRACE_PERIOD=$runner_grace_period"
Environment="RUNNER_INITIAL_GRACE_PERIOD=$runner_initial_grace_period"
Environment="RUNNER_POLL_INTERVAL=$runner_poll_interval"
Environment="LAMBDA_API_KEY=$LAMBDA_API_KEY"
Environment="LAMBDA_INSTANCE_ID=$LAMBDA_INSTANCE_ID"
Environment="LAMBDA_INSTANCE_IP=$LAMBDA_INSTANCE_IP"
ExecStart=$BIN_DIR/check-runner-termination.sh
EOF

cat > /etc/systemd/system/runner-termination-check.timer << EOF
[Unit]
Description=Periodic GitHub runner termination check
Requires=runner-termination-check.service
[Timer]
OnBootSec=60s
OnUnitActiveSec=${runner_poll_interval}s
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable runner-termination-check.timer
systemctl start runner-termination-check.timer

# Build metadata labels
METADATA_LABELS=",${INSTANCE_ID}"

log "Setting up runner"

# Export functions for subprocesses
export -f configure_runner
export -f log
export -f log_error
export -f deregister_all_runners
export -f debug_sleep_and_shutdown
export -f terminate_lambda_instance
export -f wait_for_dpkg_lock

# Single runner setup (Lambda doesn't need multi-runner complexity for now)
token="$runner_token"
labels="$runner_labels"

if [ -z "$token" ]; then
  log_error "No runner token provided"
  terminate_instance "No runner token"
fi

configure_runner 0 "$token" "${labels}${METADATA_LABELS}" "$homedir" "$repo" "$INSTANCE_ID" "$runner_grace_period" "$runner_initial_grace_period"
result=$?

if [ $result -ne 0 ]; then
  terminate_instance "Runner failed to register"
fi

log "Runner registered and started successfully"
touch $RUNNER_STATE_DIR/registered

# Kill registration watchdog now that runners are registered
if [ -f $RUNNER_STATE_DIR/watchdog.pid ]; then
  WATCHDOG_PID=$(cat $RUNNER_STATE_DIR/watchdog.pid)
  kill $WATCHDOG_PID 2>/dev/null || true
  rm -f $RUNNER_STATE_DIR/watchdog.pid
fi

# Final setup - ensure runner directories are accessible for debugging
touch $RUNNER_STATE_DIR/started
chmod o+x $homedir
for RUNNER_DIR in $homedir/runner-*; do
  [ -d "$RUNNER_DIR/_diag" ] && chmod 755 "$RUNNER_DIR/_diag"
done

log "Runner setup complete"
