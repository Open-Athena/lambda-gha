from os import environ

from gha_runner.gh import GitHubInstance
from gha_runner.helper.input import EnvVarBuilder, check_required

from lambda_gha.defaults import (
    DEFAULT_INSTANCE_TYPE,
    DEFAULT_REGION,
    INSTANCE_COUNT,
    MAX_INSTANCE_LIFETIME,
    RUNNER_GRACE_PERIOD,
    RUNNER_INITIAL_GRACE_PERIOD,
    RUNNER_POLL_INTERVAL,
    RUNNER_REGISTRATION_TIMEOUT,
)
from lambda_gha.start import StartLambdaLabs


def main():
    env = dict(environ)
    required = ["GH_PAT", "LAMBDA_API_KEY"]
    check_required(env, required)

    timeout_str = environ.get("INPUT_RUNNER_REGISTRATION_TIMEOUT", "").strip()
    timeout = int(timeout_str) if timeout_str else int(RUNNER_REGISTRATION_TIMEOUT)

    token = environ["GH_PAT"]
    api_key = environ["LAMBDA_API_KEY"]

    builder = (
        EnvVarBuilder(env)
        .update_state("INPUT_DEBUG", "debug")
        .update_state("INPUT_EXTRA_GH_LABELS", "labels")
        .update_state("INPUT_INSTANCE_COUNT", "instance_count", type_hint=int)
        .update_state("INPUT_INSTANCE_TYPE", "instance_type")
        .update_state("INPUT_MAX_INSTANCE_LIFETIME", "max_instance_lifetime")
        .update_state("INPUT_REGION", "region")
        .update_state("INPUT_RUNNER_GRACE_PERIOD", "runner_grace_period")
        .update_state("INPUT_RUNNER_INITIAL_GRACE_PERIOD", "runner_initial_grace_period")
        .update_state("INPUT_RUNNER_POLL_INTERVAL", "runner_poll_interval")
        .update_state("INPUT_SSH_KEY_NAMES", "ssh_key_names")
        .update_state("INPUT_USERDATA", "userdata")
        .update_state("GITHUB_REPOSITORY", "repo")
        .update_state("INPUT_REPO", "repo")
    )
    params = builder.params
    repo = params["repo"]
    if repo is None:
        raise ValueError("Repo cannot be empty")

    instance_count = params.pop("instance_count", INSTANCE_COUNT)

    # Apply defaults
    params.setdefault("max_instance_lifetime", MAX_INSTANCE_LIFETIME)
    params.setdefault("runner_grace_period", RUNNER_GRACE_PERIOD)
    params.setdefault("runner_initial_grace_period", RUNNER_INITIAL_GRACE_PERIOD)
    params.setdefault("runner_poll_interval", RUNNER_POLL_INTERVAL)
    params.setdefault("instance_type", DEFAULT_INSTANCE_TYPE)
    params.setdefault("region", DEFAULT_REGION)

    # Parse SSH key names (comma-separated)
    ssh_key_names_str = params.pop("ssh_key_names", None)
    if ssh_key_names_str:
        params["ssh_key_names"] = [k.strip() for k in ssh_key_names_str.split(",") if k.strip()]
    else:
        # Try vars fallback
        ssh_key_names_var = environ.get("LAMBDA_SSH_KEY_NAMES", "")
        if ssh_key_names_var:
            params["ssh_key_names"] = [k.strip() for k in ssh_key_names_var.split(",") if k.strip()]
        else:
            raise ValueError("SSH key names (ssh_key_names) must be provided")

    gh = GitHubInstance(token=token, repo=repo)

    # Get runner release (Lambda instances are Linux x64)
    runner_release = gh.get_latest_runner_release(platform="linux", architecture="x64")
    params["runner_release"] = runner_release

    # Generate runner tokens
    tokens = gh.create_runner_tokens(instance_count)

    # Create Lambda Labs starter
    starter = StartLambdaLabs(
        api_key=api_key,
        gh_runner_tokens=tokens,
        **params,
    )

    # Launch instances
    mapping = starter.create_instances()
    instance_ids = list(mapping.keys())

    # Wait for instances to be ready
    print(f"Waiting for {len(instance_ids)} instance(s) to be ready...")
    details = starter.wait_until_ready(instance_ids)

    # SSH into each instance and run setup
    for instance_id, meta in mapping.items():
        instance_details = details.get(instance_id, {})
        ip = instance_details.get("ip")
        if not ip:
            raise RuntimeError(f"No IP address for instance {instance_id}")

        print(f"Instance {instance_id}: IP={ip}, label={meta['labels']}")

        # Add instance IP to env vars
        env_vars = meta["env_vars"]
        env_vars["LAMBDA_INSTANCE_IP"] = ip

        # Execute setup via SSH
        starter.execute_setup_via_ssh(
            instance_id=instance_id,
            ip=ip,
            env_vars=env_vars,
            action_sha=meta["action_sha"],
        )

    # Output mapping for GitHub Actions
    starter.set_instance_mapping(mapping)

    # Wait for runners to register
    labels = [meta["labels"] for meta in mapping.values()]
    print(f"Waiting for runners to register: {labels}")
    for label in labels:
        gh.wait_for_runner(label, timeout=timeout)
        print(f"Runner {label} registered successfully")


if __name__ == "__main__":
    main()
