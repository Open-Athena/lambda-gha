import subprocess
import time
from dataclasses import dataclass, field
from os import environ

import requests
from gha_runner import gh
from gha_runner.helper.workflow_cmds import output

from lambda_gha.annotations import (
    emit_capacity_warning,
    emit_all_exhausted_error,
    format_launch_summary,
    write_summary,
)
from lambda_gha.defaults import (
    LAMBDA_API_BASE,
    RUNNER_REGISTRATION_TIMEOUT,
)
from lambda_gha.errors import (
    AllCapacityExhaustedError,
    CapacityError,
    ConfigurationError,
    LaunchAttempt,
    RateLimitError,
    classify_api_error,
)

INSTANCE_POLL_INTERVAL = 5
INSTANCE_POLL_TIMEOUT = 600  # Lambda instances can take 5+ minutes to boot


def resolve_ref_to_sha(ref: str) -> str:
    """Resolve a Git ref (branch/tag/SHA) to a commit SHA using local git."""
    subprocess.run(
        ['git', 'config', '--global', '--add', 'safe.directory', '/github/workspace'],
        capture_output=True,
        text=True,
        check=True,
    )

    try:
        result = subprocess.run(
            ['git', 'rev-parse', ref],
            capture_output=True,
            text=True,
            check=True,
        )
        sha = result.stdout.strip()
        if sha:
            if sha != ref:
                print(f"Resolved action_ref '{ref}' to SHA: {sha}")
            return sha
        else:
            raise RuntimeError(f"git rev-parse returned empty output for ref '{ref}'")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to resolve action_ref '{ref}' to SHA. "
            f"Error: {e.stderr or str(e)}"
        )


@dataclass
class StartLambdaLabs:
    """Start GitHub Actions runners on Lambda Labs GPU cloud.

    Parameters
    ----------
    api_key : str
        Lambda Labs API key for authentication.
    instance_types : list[str]
        Lambda instance types to try in order (e.g., ["gpu_1x_a10", "gpu_1x_a100"]).
        Falls back to next type on capacity failure.
    regions : list[str]
        Lambda regions to try in order (e.g., ["us-east-1", "us-west-1"]).
        For each instance type, tries each region before moving to next type.
    repo : str
        GitHub repository (owner/repo format).
    ssh_key_names : list[str]
        SSH key names registered in Lambda Labs.
    debug : str
        Debug mode: false=off, true/trace=set -x only, number=set -x + sleep N minutes.
    gh_runner_tokens : list[str]
        GitHub runner registration tokens.
    labels : str
        Extra labels for the runner (comma-separated).
    max_instance_lifetime : str
        Maximum instance lifetime in minutes before shutdown (default: 360).
    runner_grace_period : str
        Grace period in seconds after last job before termination (default: 60).
    runner_initial_grace_period : str
        Grace period in seconds before terminating if no jobs start (default: 180).
    runner_poll_interval : str
        Polling interval in seconds for termination check (default: 10).
    retry_count : int
        Number of retries per instance type/region combination (default: 1).
    retry_delay : float
        Initial delay between retries in seconds (default: 5.0).
        Uses exponential backoff: delay * 2^attempt.
    userdata : str
        Custom script to run before runner setup.
    """

    api_key: str
    instance_types: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    repo: str = ""
    ssh_key_names: list[str] = field(default_factory=list)
    debug: str = ""
    gh_runner_tokens: list[str] = field(default_factory=list)
    labels: str = ""
    max_instance_lifetime: str = "360"
    runner_grace_period: str = "60"
    runner_initial_grace_period: str = "180"
    runner_poll_interval: str = "10"
    retry_count: int = 1
    retry_delay: float = 5.0
    check_availability: bool = True
    runner_release: str = ""
    ssh_private_key: str = ""
    userdata: str = ""

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict = None,
        raise_classified: bool = False,
    ) -> dict:
        """Make an authenticated request to the Lambda Labs API.

        Parameters
        ----------
        method : str
            HTTP method (GET, POST, etc.).
        endpoint : str
            API endpoint path.
        json_data : dict, optional
            JSON body for the request.
        raise_classified : bool
            If True, classify API errors and raise appropriate exceptions
            (CapacityError, RateLimitError, etc.) instead of generic HTTPError.

        Returns
        -------
        dict
            JSON response from the API.

        Raises
        ------
        CapacityError
            If the request failed due to insufficient capacity (when raise_classified=True).
        RateLimitError
            If the request was rate limited (when raise_classified=True).
        ConfigurationError
            If the request failed due to invalid configuration (when raise_classified=True).
        requests.HTTPError
            If the request failed and raise_classified=False.
        """
        url = f"{LAMBDA_API_BASE}{endpoint}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        resp = requests.request(method, url, headers=headers, json=json_data)
        if not resp.ok:
            try:
                error_body = resp.json()
                print(f"Lambda API error: {error_body}")

                if raise_classified:
                    classified = classify_api_error(error_body)
                    raise classified
            except (ValueError, KeyError):
                print(f"Lambda API error (raw): {resp.text}")

            resp.raise_for_status()
        return resp.json()

    def get_availability(self) -> dict[str, list[str]]:
        """Get current capacity availability for all instance types.

        Returns
        -------
        dict[str, list[str]]
            Mapping of instance type names to list of regions with capacity.
            Empty list means no capacity available anywhere.
        """
        result = self._api_request("GET", "/instance-types")
        data = result.get("data", {})

        availability = {}
        for type_name, info in data.items():
            regions = info.get("regions_with_capacity_available", [])
            availability[type_name] = [r["name"] for r in regions]

        return availability

    def filter_available_options(
        self,
        instance_types: list[str],
        regions: list[str],
    ) -> list[tuple[str, str]]:
        """Filter instance type/region combinations to only those with capacity.

        Parameters
        ----------
        instance_types : list[str]
            Requested instance types in preference order.
        regions : list[str]
            Requested regions in preference order.

        Returns
        -------
        list[tuple[str, str]]
            List of (instance_type, region) tuples that have capacity,
            in preference order.
        """
        availability = self.get_availability()

        available_options = []
        skipped = []

        for instance_type in instance_types:
            available_regions = availability.get(instance_type, [])

            if not available_regions:
                skipped.append((instance_type, "all regions"))
                continue

            for region in regions:
                if region in available_regions:
                    available_options.append((instance_type, region))
                else:
                    skipped.append((instance_type, region))

        # Log what we're skipping
        if skipped:
            print(f"Skipping {len(skipped)} options with no capacity:")
            for instance_type, region in skipped[:5]:  # Show first 5
                print(f"  - {instance_type} in {region}")
            if len(skipped) > 5:
                print(f"  - ... and {len(skipped) - 5} more")

        if available_options:
            print(f"Found {len(available_options)} options with capacity")

        return available_options

    def _get_template_vars(self, idx: int = None) -> dict:
        """Build template variables for instance naming."""
        import re

        template_vars = {}

        if environ.get("GITHUB_REPOSITORY"):
            template_vars["repo"] = environ["GITHUB_REPOSITORY"].split("/")[-1]
        else:
            template_vars["repo"] = "unknown"

        template_vars["workflow"] = environ.get("GITHUB_WORKFLOW", "unknown")

        workflow_ref = environ.get("GITHUB_WORKFLOW_REF", "")
        if workflow_ref:
            m = re.search(r'/(?P<name>[^/@]+)\.(yml|yaml)@(?P<ref>[^@]+)$', workflow_ref)
            if m:
                template_vars["name"] = m['name']
                ref = m['ref']
                if ref.startswith('refs/heads/'):
                    ref = ref[11:]
                elif ref.startswith('refs/tags/'):
                    ref = ref[10:]
                template_vars["ref"] = ref
            else:
                template_vars["name"] = "unknown"
                template_vars["ref"] = "unknown"
        else:
            template_vars["name"] = "unknown"
            template_vars["ref"] = "unknown"

        run_num = environ.get("GITHUB_RUN_NUMBER", "unknown")
        template_vars["run"] = run_num

        if idx is not None:
            template_vars["idx"] = str(idx)

        return template_vars

    def _launch_single_instance(
        self,
        instance_type: str,
        region: str,
        instance_name: str,
    ) -> str:
        """Attempt to launch a single instance.

        Parameters
        ----------
        instance_type : str
            The instance type to launch.
        region : str
            The region to launch in.
        instance_name : str
            Name for the instance (visible in Lambda dashboard).

        Returns
        -------
        str
            The instance ID if successful.

        Raises
        ------
        CapacityError
            If launch failed due to insufficient capacity.
        RateLimitError
            If launch was rate limited.
        ConfigurationError
            If launch failed due to invalid configuration.
        RuntimeError
            If launch failed for other reasons.
        """
        payload = {
            "instance_type_name": instance_type,
            "region_name": region,
            "ssh_key_names": self.ssh_key_names,
            "quantity": 1,
            "name": instance_name,
        }

        print(f"Launching Lambda instance: {instance_type} in {region}")
        result = self._api_request(
            "POST",
            "/instance-operations/launch",
            payload,
            raise_classified=True,
        )

        if "data" not in result or "instance_ids" not in result["data"]:
            raise RuntimeError(f"Unexpected API response: {result}")

        instance_ids = result["data"]["instance_ids"]
        if not instance_ids:
            error_msg = result.get("error", {}).get("message", "Unknown error")
            # Check if this looks like a capacity error
            if "capacity" in error_msg.lower():
                raise CapacityError(instance_type, region, error_msg)
            raise RuntimeError(f"Failed to launch instance: {error_msg}")

        return instance_ids[0]

    def create_instances(self) -> dict[str, dict]:
        """Create instances on Lambda Labs with fallback support.

        Tries each instance type in order, and for each type tries each region.
        On capacity failures, moves to the next option. Retries with exponential
        backoff for rate limit errors.

        Returns
        -------
        dict[str, dict]
            Mapping of instance IDs to runner metadata (label, labels, env_vars, action_sha).

        Raises
        ------
        AllCapacityExhaustedError
            If all instance type/region combinations fail due to capacity.
        ConfigurationError
            If launch fails due to invalid configuration (non-retryable).
        """
        from lambda_gha.log_constants import (
            LOG_PREFIX_JOB_COMPLETED,
            LOG_PREFIX_JOB_STARTED,
        )

        if not self.gh_runner_tokens:
            raise ValueError("No GitHub runner tokens provided")
        if not self.runner_release:
            raise ValueError("No runner release provided")
        if not self.instance_types:
            raise ValueError("No instance types provided")
        if not self.regions:
            raise ValueError("No regions provided")
        if not self.ssh_key_names:
            raise ValueError("No SSH key names provided")

        # Resolve action ref once (same for all instances)
        action_ref = environ.get("INPUT_ACTION_REF")
        if not action_ref:
            raise ValueError("action_ref is required")
        action_sha = resolve_ref_to_sha(action_ref)

        id_dict = {}
        all_attempts: list[LaunchAttempt] = []

        # Pre-filter to available options if enabled
        if self.check_availability:
            print("Checking instance availability...")
            available_options = self.filter_available_options(
                self.instance_types, self.regions
            )
            if not available_options:
                # No capacity anywhere - fail fast
                for instance_type in self.instance_types:
                    for region in self.regions:
                        all_attempts.append(LaunchAttempt(
                            instance_type=instance_type,
                            region=region,
                            attempt=0,
                            error="No capacity (pre-check)",
                        ))
                summary = format_launch_summary(all_attempts, success=False)
                write_summary(summary)
                emit_all_exhausted_error(all_attempts)
                raise AllCapacityExhaustedError(all_attempts)
        else:
            # Try all combinations without pre-check
            available_options = [
                (t, r) for t in self.instance_types for r in self.regions
            ]

        for idx, token in enumerate(self.gh_runner_tokens):
            label = gh.GitHubInstance.generate_random_label()
            labels = f"{self.labels},{label}" if self.labels else label

            # Lambda Labs instance name (visible in dashboard)
            template_vars = self._get_template_vars(idx)
            instance_name = f"gha-{template_vars.get('repo', 'unknown')}-{template_vars.get('run', '0')}"
            if len(self.gh_runner_tokens) > 1:
                instance_name = f"{instance_name}-{idx}"

            # Try each available instance type/region combo with retries
            instance_id = None
            successful_type = None
            successful_region = None
            token_attempts: list[LaunchAttempt] = []

            for instance_type, region in available_options:
                if instance_id:
                    break

                for retry in range(self.retry_count):
                    attempt = LaunchAttempt(
                        instance_type=instance_type,
                        region=region,
                        attempt=retry + 1,
                    )

                    try:
                        instance_id = self._launch_single_instance(
                            instance_type=instance_type,
                            region=region,
                            instance_name=instance_name,
                        )
                        attempt.success = True
                        attempt.instance_id = instance_id
                        token_attempts.append(attempt)
                        successful_type = instance_type
                        successful_region = region
                        print(f"Launched instance {instance_id}")
                        break

                    except CapacityError as e:
                        attempt.error = str(e)
                        token_attempts.append(attempt)

                        # Determine what we'll try next for the warning message
                        next_option = self._get_next_option_from_list(
                            available_options, instance_type, region
                        )
                        emit_capacity_warning(instance_type, region, next_option)

                        # Don't retry same type+region for capacity errors
                        break

                    except RateLimitError as e:
                        attempt.error = str(e)
                        token_attempts.append(attempt)

                        if retry < self.retry_count - 1:
                            delay = self.retry_delay * (2 ** retry)
                            if e.retry_after:
                                delay = max(delay, e.retry_after)
                            print(f"Rate limited, waiting {delay:.1f}s...")
                            time.sleep(delay)
                        else:
                            # Move to next option after exhausting retries
                            break

                    except ConfigurationError as e:
                        # Non-retryable - fail immediately
                        attempt.error = str(e)
                        token_attempts.append(attempt)
                        all_attempts.extend(token_attempts)
                        raise

            all_attempts.extend(token_attempts)

            if not instance_id:
                # Failed to launch for this token
                continue

            # Build full labels including instance type, region, and run info
            # Format: lambda,<instance_type>,<region>,GPU,run-N,<repo>,<user_labels>,<random_label>
            run_num = template_vars.get("run", "")
            repo_name = template_vars.get("repo", "")
            auto_labels = ["lambda", successful_type, successful_region, "GPU"]
            if run_num:
                auto_labels.append(f"run-{run_num}")
            if repo_name:
                auto_labels.append(repo_name)
            if self.labels:
                all_labels = auto_labels + [self.labels] + [label]
            else:
                all_labels = auto_labels + [label]
            full_labels = ",".join(all_labels)

            # Build env vars for SSH setup (will be set on instance)
            env_vars = {
                "action_sha": action_sha,
                "debug": self.debug or "",
                "LAMBDA_API_KEY": self.api_key,
                "LAMBDA_INSTANCE_ID": instance_id,
                "log_prefix_job_started": LOG_PREFIX_JOB_STARTED,
                "log_prefix_job_completed": LOG_PREFIX_JOB_COMPLETED,
                "max_instance_lifetime": self.max_instance_lifetime,
                "repo": self.repo,
                "runner_grace_period": self.runner_grace_period,
                "runner_initial_grace_period": self.runner_initial_grace_period,
                "runner_labels": full_labels,
                "runner_poll_interval": self.runner_poll_interval,
                "runner_registration_timeout": environ.get("INPUT_RUNNER_REGISTRATION_TIMEOUT", "").strip() or RUNNER_REGISTRATION_TIMEOUT,
                "runner_release": self.runner_release,
                "runner_token": token,
                "userdata": self.userdata or "",
            }

            id_dict[instance_id] = {
                "label": label,
                "labels": full_labels,
                "env_vars": env_vars,
                "action_sha": action_sha,
                "instance_type": successful_type,
                "region": successful_region,
            }

        # Write summary for all attempts
        if id_dict:
            # At least one instance launched successfully
            first_id = list(id_dict.keys())[0]
            summary = format_launch_summary(
                all_attempts,
                success=True,
                instance_id=first_id,
            )
            write_summary(summary)
        else:
            # All launches failed
            summary = format_launch_summary(all_attempts, success=False)
            write_summary(summary)
            emit_all_exhausted_error(all_attempts)
            raise AllCapacityExhaustedError(all_attempts)

        return id_dict

    def _get_next_option_from_list(
        self,
        options: list[tuple[str, str]],
        current_type: str,
        current_region: str,
    ) -> str:
        """Get a description of what will be tried next from a filtered options list.

        Parameters
        ----------
        options : list[tuple[str, str]]
            List of (instance_type, region) tuples to try.
        current_type : str
            The instance type that just failed.
        current_region : str
            The region that just failed.

        Returns
        -------
        str
            Description of the next option to try, or empty string if exhausted.
        """
        try:
            current_idx = options.index((current_type, current_region))
            if current_idx + 1 < len(options):
                next_type, next_region = options[current_idx + 1]
                if next_type == current_type:
                    return f"{next_type} in {next_region}"
                return f"{next_type}"
        except ValueError:
            pass
        return ""

    def _get_next_option(
        self,
        current_type: str,
        current_region: str,
        current_retry: int,
    ) -> str:
        """Get a description of what will be tried next after a failure.

        Parameters
        ----------
        current_type : str
            The instance type that just failed.
        current_region : str
            The region that just failed.
        current_retry : int
            The retry number (0-indexed) that just failed.

        Returns
        -------
        str
            Description of the next option to try, or empty string if exhausted.
        """
        type_idx = self.instance_types.index(current_type)
        region_idx = self.regions.index(current_region)

        # Next region for this type?
        if region_idx + 1 < len(self.regions):
            next_region = self.regions[region_idx + 1]
            return f"{current_type} in {next_region}"

        # Next instance type?
        if type_idx + 1 < len(self.instance_types):
            next_type = self.instance_types[type_idx + 1]
            return f"{next_type}"

        return ""

    def wait_until_ready(self, ids: list[str], timeout: int = INSTANCE_POLL_TIMEOUT) -> dict[str, dict]:
        """Wait until instances are running and return their details.

        Parameters
        ----------
        ids : list[str]
            Instance IDs to wait for.
        timeout : int
            Maximum seconds to wait.

        Returns
        -------
        dict[str, dict]
            Instance details including IP addresses.
        """
        start_time = time.time()
        pending = set(ids)
        details = {}
        last_log_time = {}  # Track last log time per instance to reduce spam

        while pending and (time.time() - start_time) < timeout:
            elapsed = int(time.time() - start_time)
            for instance_id in list(pending):
                try:
                    result = self._api_request("GET", f"/instances/{instance_id}")
                    instance = result.get("data", {})
                    status = instance.get("status")

                    if status == "active":
                        details[instance_id] = {
                            "ip": instance.get("ip"),
                            "hostname": instance.get("hostname"),
                            "status": status,
                        }
                        pending.remove(instance_id)
                        print(f"[{elapsed}s] Instance {instance_id[:12]}... is ready: {instance.get('ip')}")
                    elif status in ("terminated", "terminating"):
                        raise RuntimeError(f"Instance {instance_id} terminated unexpectedly")
                    else:
                        # Log every 30s to reduce spam, but always log first status
                        last_log = last_log_time.get(instance_id, 0)
                        if elapsed - last_log >= 30 or last_log == 0:
                            print(f"[{elapsed}s] Instance {instance_id[:12]}... status: {status}")
                            last_log_time[instance_id] = elapsed
                except requests.HTTPError as e:
                    if e.response.status_code == 404:
                        last_log = last_log_time.get(instance_id, 0)
                        if elapsed - last_log >= 30 or last_log == 0:
                            print(f"[{elapsed}s] Instance {instance_id[:12]}... not found yet, retrying...")
                            last_log_time[instance_id] = elapsed
                    else:
                        raise

            if pending:
                time.sleep(INSTANCE_POLL_INTERVAL)

        if pending:
            elapsed = int(time.time() - start_time)
            raise TimeoutError(f"[{elapsed}s] Instances did not become ready within {timeout}s: {pending}")

        return details

    def terminate_instances(self, ids: list[str]):
        """Terminate instances.

        Parameters
        ----------
        ids : list[str]
            Instance IDs to terminate.
        """
        if not ids:
            return

        payload = {"instance_ids": ids}
        result = self._api_request("POST", "/instance-operations/terminate", payload)
        print(f"Terminated instances: {ids}")
        return result

    def execute_setup_via_ssh(
        self,
        instance_id: str,
        ip: str,
        env_vars: dict[str, str],
        action_sha: str,
        ssh_user: str = "ubuntu",
        max_retries: int = 30,
        retry_delay: int = 10,
    ):
        """Execute setup script on instance via SSH.

        SSH in, export env vars, then curl and run the setup script from GitHub.

        Parameters
        ----------
        instance_id : str
            Lambda instance ID.
        ip : str
            Instance IP address.
        env_vars : dict[str, str]
            Environment variables to export before running setup.
        action_sha : str
            Git SHA for fetching scripts from GitHub.
        ssh_user : str
            SSH username (default: ubuntu for Lambda instances).
        max_retries : int
            Maximum SSH connection attempts.
        retry_delay : int
            Seconds between retry attempts.
        """
        import os
        import stat
        import tempfile

        print(f"Connecting to {ssh_user}@{ip} to execute setup...")

        # Write SSH private key to temporary file if provided
        key_file = None
        if self.ssh_private_key:
            key_file = tempfile.NamedTemporaryFile(mode='w', suffix='_key', delete=False)
            key_file.write(self.ssh_private_key)
            if not self.ssh_private_key.endswith('\n'):
                key_file.write('\n')
            key_file.close()
            os.chmod(key_file.name, stat.S_IRUSR)  # 0400
            print(f"Using SSH key from secret")

        # SSH options for non-interactive, key-based auth
        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
        ]
        if key_file:
            ssh_opts.extend(["-i", key_file.name])

        # Wait for SSH to be available
        for attempt in range(1, max_retries + 1):
            try:
                result = subprocess.run(
                    ["ssh"] + ssh_opts + [f"{ssh_user}@{ip}", "echo", "SSH ready"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    print(f"SSH connection established (attempt {attempt})")
                    break
            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                print(f"SSH attempt {attempt} failed: {e}")

            if attempt < max_retries:
                print(f"Waiting for SSH... (attempt {attempt}/{max_retries})")
                time.sleep(retry_delay)
            else:
                raise RuntimeError(f"Failed to connect to {ip} via SSH after {max_retries} attempts")

        # Read all required scripts from package (can't curl from private repo)
        from importlib.resources import files
        scripts_dir = files("lambda_gha.scripts")
        templates_dir = files("lambda_gha.templates")

        # Scripts to copy: (source, dest_name)
        scripts_to_copy = [
            (scripts_dir / "runner-setup.sh", "runner-setup.sh"),
            (scripts_dir / "check-runner-termination.sh", "check-runner-termination.sh"),
            (scripts_dir / "job-started-hook.sh", "job-started-hook.sh"),
            (scripts_dir / "job-completed-hook.sh", "job-completed-hook.sh"),
            (templates_dir / "shared-functions.sh", "shared-functions.sh"),
        ]

        # SCP options
        scp_opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        if key_file:
            scp_opts.extend(["-i", key_file.name])

        # Create scripts directory on instance
        print(f"Creating scripts directory on instance...")
        mkdir_result = subprocess.run(
            ["ssh"] + ssh_opts + [f"{ssh_user}@{ip}", "mkdir -p /tmp/lambda-gha-scripts"],
            capture_output=True,
            text=True,
        )
        if mkdir_result.returncode != 0:
            raise RuntimeError(f"Failed to create scripts dir: {mkdir_result.stderr}")

        # Copy all scripts
        print(f"Copying {len(scripts_to_copy)} scripts to instance...")
        for src_file, dest_name in scripts_to_copy:
            content = src_file.read_text()
            local_file = tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False)
            local_file.write(content)
            local_file.close()
            os.chmod(local_file.name, stat.S_IRUSR | stat.S_IXUSR)

            scp_result = subprocess.run(
                ["scp"] + scp_opts + [local_file.name, f"{ssh_user}@{ip}:/tmp/lambda-gha-scripts/{dest_name}"],
                capture_output=True,
                text=True,
            )
            os.unlink(local_file.name)
            if scp_result.returncode != 0:
                raise RuntimeError(f"Failed to SCP {dest_name}: {scp_result.stderr}")

        # Add SCRIPTS_DIR for local script access
        env_vars["SCRIPTS_DIR"] = "/tmp/lambda-gha-scripts"

        # Write env vars to a file on the instance (more reliable than sudo -E)
        env_file_content = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())
        write_env_cmd = f"cat > /tmp/lambda-gha-scripts/env.sh << 'ENVEOF'\n{env_file_content}\nENVEOF"

        print(f"Writing environment file to instance...")
        env_result = subprocess.run(
            ["ssh"] + ssh_opts + [f"{ssh_user}@{ip}", write_env_cmd],
            capture_output=True,
            text=True,
        )
        if env_result.returncode != 0:
            raise RuntimeError(f"Failed to write env file: {env_result.stderr}")

        # Build the setup command: source env file, then run script
        setup_cmd = '''
chmod +x /tmp/lambda-gha-scripts/*.sh
sudo bash -c 'source /tmp/lambda-gha-scripts/env.sh && nohup /tmp/lambda-gha-scripts/runner-setup.sh > /var/log/runner-setup.log 2>&1 &'
'''

        print(f"Executing setup script...")
        exec_result = subprocess.run(
            ["ssh"] + ssh_opts + [f"{ssh_user}@{ip}", setup_cmd],
            capture_output=True,
            text=True,
        )
        if exec_result.returncode != 0:
            raise RuntimeError(f"Failed to execute setup: {exec_result.stderr}")

        print(f"Setup script started on {ip}")

    def set_instance_mapping(self, mapping: dict[str, dict]):
        """Output instance mapping for downstream jobs.

        Parameters
        ----------
        mapping : dict[str, dict]
            Mapping of instance IDs to their metadata (label, labels, user_data).
        """
        import json

        matrix_objects = []
        for idx, (instance_id, meta) in enumerate(mapping.items()):
            matrix_objects.append({
                "idx": idx,
                "id": meta["labels"],
                "instance_id": instance_id,
            })

        output("mtx", json.dumps(matrix_objects))

        # For single instance, output simplified values
        if len(mapping) == 1:
            instance_id = list(mapping.keys())[0]
            meta = list(mapping.values())[0]
            output("instance-id", instance_id)
            output("label", meta["labels"])
