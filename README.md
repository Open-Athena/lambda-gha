# lambda-gha

Run GitHub Actions on ephemeral Lambda Labs GPU instances.

## Quick Start

Call [`runner.yml`] as a [reusable workflow]:

```yaml
name: GPU Tests
on: [push]
jobs:
  lambda:
    uses: Open-Athena/lambda-gha/.github/workflows/runner.yml@main
    secrets: inherit
    with:
      instance_type: gpu_1x_a10
      region: us-south-1
  gpu-test:
    needs: lambda
    runs-on: ${{ needs.lambda.outputs.id }}
    steps:
      - run: nvidia-smi  # GPU node!
```

## Setup

### Required Secrets

#### `LAMBDA_API_KEY`
Get your API key from [Lambda Labs Cloud Dashboard](https://cloud.lambdalabs.com/api-keys) and add it as a repository secret:

```bash
gh secret set LAMBDA_API_KEY --body "your_api_key_here"
```

#### `GH_SA_TOKEN`
Create a GitHub Personal Access Token with `repo` scope and admin access, and add it as a repository secret:

```bash
gh secret set GH_SA_TOKEN --body "your_personal_access_token_here"
```

### Required Variables

#### `LAMBDA_SSH_KEY_NAMES`
Register an SSH key in your [Lambda Labs account](https://cloud.lambdalabs.com/ssh-keys), then set the key name(s):

```bash
gh variable set LAMBDA_SSH_KEY_NAMES --body "my-ssh-key"
```

## Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `instance_type` | Lambda instance type (e.g., `gpu_1x_a10`, `gpu_8x_a100_80gb_sxm4`) | `gpu_1x_a10` |
| `region` | Lambda region (e.g., `us-south-1`, `us-west-1`) | `us-south-1` |
| `instance_count` | Number of instances for parallel jobs | `1` |
| `debug` | Debug mode: `false`=off, `true`=tracing, number=sleep N minutes before shutdown | `false` |
| `extra_gh_labels` | Extra GitHub labels for the runner (comma-separated) | |
| `max_instance_lifetime` | Max lifetime in minutes before shutdown | `360` |
| `runner_grace_period` | Seconds before terminating after last job | `60` |
| `runner_initial_grace_period` | Seconds before terminating if no jobs start | `180` |
| `userdata` | Additional script to run before runner setup | |

## Outputs

| Output | Description |
|--------|-------------|
| `id` | Runner label for `runs-on` (single instance) |
| `mtx` | JSON array for matrix strategies |

## Lambda Labs Instance Types

Common GPU instance types:

| Type | GPUs | Description |
|------|------|-------------|
| `gpu_1x_a10` | 1x A10 | Entry-level GPU |
| `gpu_1x_a100_sxm4` | 1x A100 40GB | High-end single GPU |
| `gpu_8x_a100_80gb_sxm4` | 8x A100 80GB | Multi-GPU workloads |

See [Lambda Labs pricing](https://lambdalabs.com/service/gpu-cloud#pricing) for full list.

## Parallel Jobs

Create multiple instances for parallel execution:

```yaml
jobs:
  lambda:
    uses: Open-Athena/lambda-gha/.github/workflows/runner.yml@main
    secrets: inherit
    with:
      instance_count: "3"

  parallel-jobs:
    needs: lambda
    strategy:
      matrix:
        runner: ${{ fromJson(needs.lambda.outputs.mtx) }}
    runs-on: ${{ matrix.runner.id }}
    steps:
      - run: echo "Running on instance ${{ matrix.runner.idx }}"
```

## Key Differences from ec2-gha

| Aspect | ec2-gha | lambda-gha |
|--------|---------|------------|
| Auth | AWS OIDC / IAM | API key |
| Instance types | `g4dn.xlarge` etc | `gpu_1x_a10` etc |
| Metadata service | IMDSv2 | None |
| Termination | `shutdown -h now` | API call |
| Networking | VPC, Security Groups | SSH keys only |

## Debugging

Enable debug mode to keep the instance alive for SSH access:

```yaml
with:
  debug: "30"  # Sleep 30 minutes before termination
```

Then SSH to the instance IP shown in the workflow logs:

```bash
ssh ubuntu@<instance-ip>
```

Log files:
- `/var/log/runner-setup.log` - Runner installation
- `/tmp/termination-check.log` - Termination checks
- `~/runner-*/` - GitHub Actions runner directories

## Acknowledgements

Based on [ec2-gha](https://github.com/Open-Athena/ec2-gha), adapted for Lambda Labs.

[`runner.yml`]: .github/workflows/runner.yml
[reusable workflow]: https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows
