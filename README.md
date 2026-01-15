# lambda-gha

GitHub Action to provision ephemeral Lambda Labs GPU instances as self-hosted runners.

> **Status:** Planning / Not yet implemented

## Overview

Spin up Lambda Labs GPU instances on-demand for GitHub Actions workflows, then automatically terminate them when jobs complete.

```yaml
jobs:
  start-runner:
    runs-on: ubuntu-latest
    outputs:
      label: ${{ steps.runner.outputs.label }}
    steps:
      - uses: Open-Athena/lambda-gha@main
        id: runner
        with:
          lambda_api_key: ${{ secrets.LAMBDA_API_KEY }}
          instance_type: gpu_1x_a10
          region: us-south-1
          ssh_key_name: my-key

  gpu-job:
    needs: start-runner
    runs-on: ${{ needs.start-runner.outputs.label }}
    steps:
      - run: nvidia-smi
      # Instance self-terminates after job completes
```

## Features (Planned)

- **Self-terminating instances** - No separate "stop" job needed
- **GPU access** - H100, A100, A10, RTX 6000, etc.
- **Simple auth** - Just an API key (no IAM/OIDC complexity)
- **Minimal config** - Only Lambda-relevant inputs

## Instance Types

| Type | GPU | VRAM | Price/hr |
|------|-----|------|----------|
| `gpu_1x_h100_sxm` | H100 SXM | 80GB | $2.49 |
| `gpu_1x_a100_sxm` | A100 SXM | 80GB | $1.79 |
| `gpu_1x_a100` | A100 PCIe | 40GB | $1.29 |
| `gpu_1x_a10` | A10 | 24GB | $0.75 |
| `gpu_1x_rtx6000` | RTX 6000 | 24GB | $0.50 |

## Inputs

| Input | Required | Description |
|-------|----------|-------------|
| `lambda_api_key` | Yes | Lambda Labs API key |
| `instance_type` | Yes | Instance type (e.g., `gpu_1x_a10`) |
| `region` | No | Region (default: `us-south-1`) |
| `ssh_key_name` | Yes | SSH key name registered with Lambda |
| `runner_timeout` | No | Max runner lifetime in minutes (default: 360) |
| `github_token` | No | GitHub token for runner registration (default: `github.token`) |

## Related Projects

- [Open-Athena/ec2-gha] - Similar action for AWS EC2 (different implementation, shared patterns)

## Development

This repo shares some architectural patterns with `ec2-gha` via cherry-picking, but implementations are independent.

```bash
# Local development setup
git remote add ec2 ../ec2-gha  # or path to ec2-gha clone
git fetch ec2

# Cherry-pick useful commits from ec2-gha
git cherry-pick <commit-sha>
```

[Open-Athena/ec2-gha]: https://github.com/Open-Athena/ec2-gha
