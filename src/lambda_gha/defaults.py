"""Default values for lambda-gha configuration."""

# Lambda Labs API
LAMBDA_API_BASE = "https://cloud.lambdalabs.com/api/v1"

# Instance lifetime and timing defaults
MAX_INSTANCE_LIFETIME = "120"  # 2 hours (in minutes)
RUNNER_GRACE_PERIOD = "60"     # 1 minute (in seconds)
RUNNER_INITIAL_GRACE_PERIOD = "180"  # 3 minutes (in seconds)
RUNNER_POLL_INTERVAL = "10"    # 10 seconds
RUNNER_REGISTRATION_TIMEOUT = "300"  # 5 minutes (in seconds)

# Lambda instance defaults
DEFAULT_INSTANCE_TYPE = "gpu_1x_a10"
DEFAULT_REGION = "us-south-1"

# Instance naming default template
INSTANCE_NAME = "$repo/$name#$run"

# Default instance count
INSTANCE_COUNT = 1
