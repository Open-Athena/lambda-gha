# CLAUDE.md - Implementation Guide

## Project Goal

GitHub Action to provision ephemeral Lambda Labs GPU instances as self-hosted GitHub Actions runners.

## Architecture Overview

Based on patterns from `ec2-gha` (available as `ec2` remote), adapted for Lambda Labs API.

### Key Components

```
lambda-gha/
├── .github/workflows/
│   └── runner.yml          # Reusable workflow (main entry point)
├── action.yml              # Composite action definition
├── src/lambda_gha/
│   ├── __main__.py         # Entry point: parse inputs, orchestrate
│   ├── start.py            # StartLambdaLabs class (API calls)
│   ├── defaults.py         # Default config values
│   └── templates/
│       └── user-script.sh  # Userdata template
├── scripts/
│   ├── runner-setup.sh     # Main setup (fetched at runtime)
│   ├── check-runner-termination.sh  # Polling-based termination
│   └── shared-functions.sh # Utility functions
├── Dockerfile              # Container for action execution
└── pyproject.toml
```

## Implementation Roadmap

### Phase 1: Core Infrastructure

1. **Lambda Labs API client** (`src/lambda_gha/start.py`)
   - [ ] `create_instances()` - POST to `/instance-operations/launch`
   - [ ] `terminate_instances()` - POST to `/instance-operations/terminate`
   - [ ] `get_instance_status()` - GET `/instances/{id}`
   - [ ] `wait_until_ready()` - Poll until instance is running

2. **Entry point** (`src/lambda_gha/__main__.py`)
   - [ ] Parse environment variables (INPUT_*)
   - [ ] Generate GitHub runner token via API
   - [ ] Call StartLambdaLabs to provision instance
   - [ ] Output runner label for downstream jobs

3. **action.yml**
   - [ ] Define inputs (lambda_api_key, instance_type, region, ssh_key_name)
   - [ ] Define outputs (label, instance_id)
   - [ ] Run Python in Docker container

### Phase 2: Runner Lifecycle

4. **Userdata / runner setup** (`scripts/runner-setup.sh`)
   - [ ] Download and configure GitHub Actions runner
   - [ ] Register runner with generated token
   - [ ] Set up termination polling

5. **Self-termination** (`scripts/check-runner-termination.sh`)
   - Cherry-pick from ec2-gha, adapt for Lambda:
   - [ ] Poll for job completion
   - [ ] Grace periods (initial, between jobs)
   - [ ] Deregister runner before termination
   - [ ] Call Lambda API to terminate (vs `shutdown -h now`)

6. **Runner hooks** (job-started-hook.sh, job-completed-hook.sh)
   - Can likely cherry-pick directly from ec2-gha
   - [ ] Track job lifecycle via file markers
   - [ ] Update last-activity timestamp

### Phase 3: Workflow Integration

7. **Reusable workflow** (`.github/workflows/runner.yml`)
   - [ ] Wrap action.yml for easier consumption
   - [ ] Output matrix-compatible labels

8. **Testing**
   - [ ] Demo workflow that runs `nvidia-smi`
   - [ ] Integration test with real Lambda instance

## Lambda Labs API Reference

### Authentication
```bash
curl -u $LAMBDA_API_KEY: https://cloud.lambda.ai/api/v1/instances
# or
curl -H "Authorization: Bearer $LAMBDA_API_KEY" ...
```

### Launch Instance
```bash
POST https://cloud.lambda.ai/api/v1/instance-operations/launch
{
  "instance_type_name": "gpu_1x_a10",
  "region_name": "us-south-1",
  "ssh_key_names": ["my-key"],
  "quantity": 1
}
```

### Terminate Instance
```bash
POST https://cloud.lambda.ai/api/v1/instance-operations/terminate
{
  "instance_ids": ["<instance-id>"]
}
```

### Get Instance
```bash
GET https://cloud.lambda.ai/api/v1/instances/{id}
```

### List Instance Types
```bash
GET https://cloud.lambda.ai/api/v1/instance-types
```

## Key Differences from ec2-gha

| Aspect | ec2-gha | lambda-gha |
|--------|---------|------------|
| Auth | AWS OIDC / IAM | API key (simpler) |
| Instance types | `g4dn.xlarge` etc | `gpu_1x_a10` etc |
| Metadata service | IMDSv2 (169.254.169.254) | None - must pass at launch |
| Termination | `shutdown -h now` | API call required |
| Networking | VPC, Security Groups | SSH keys only |
| Regions | 30+ AWS regions | ~2-3 Lambda regions |

## Cherry-pick Candidates from ec2-gha

These commits/files are likely portable:

- `scripts/check-runner-termination.sh` - Core polling logic
- `scripts/shared-functions.sh` - Logging, deregister functions
- `scripts/job-started-hook.sh` / `job-completed-hook.sh` - Runner hooks
- Userdata template structure (minimal script + fetch pattern)
- GitHub token generation logic

To find relevant commits:
```bash
git log ec2/main --oneline -- scripts/
git log ec2/main --oneline -- src/ec2_gha/templates/
```

## Testing Locally

```bash
# Set up environment
export LAMBDA_API_KEY="your-key"
export GITHUB_TOKEN="ghp_..."
export INPUT_INSTANCE_TYPE="gpu_1x_a10"
export INPUT_REGION="us-south-1"
export INPUT_SSH_KEY_NAME="my-key"

# Run
python -m lambda_gha
```

## Resources

- [Lambda Cloud API Docs](https://docs.lambda.ai/public-cloud/cloud-api/)
- [lambda-cloud-client PyPI](https://pypi.org/project/lambda-cloud-client/)
- [ec2-gha source](../ec2-gha) (local remote)
