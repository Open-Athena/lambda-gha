import subprocess
import time
from dataclasses import dataclass, field
from os import environ

import requests
from gha_runner import gh
from gha_runner.helper.workflow_cmds import output

from lambda_gha.defaults import (
    LAMBDA_API_BASE,
    RUNNER_REGISTRATION_TIMEOUT,
)

INSTANCE_POLL_INTERVAL = 5
INSTANCE_POLL_TIMEOUT = 300


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
    instance_type : str
        Lambda instance type (e.g., "gpu_1x_a10", "gpu_8x_a100_80gb_sxm4").
    region : str
        Lambda region (e.g., "us-south-1", "us-west-1").
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
    userdata : str
        Custom script to run before runner setup.
    """

    api_key: str
    instance_type: str
    region: str
    repo: str
    ssh_key_names: list[str] = field(default_factory=list)
    debug: str = ""
    gh_runner_tokens: list[str] = field(default_factory=list)
    labels: str = ""
    max_instance_lifetime: str = "360"
    runner_grace_period: str = "60"
    runner_initial_grace_period: str = "180"
    runner_poll_interval: str = "10"
    runner_release: str = ""
    ssh_private_key: str = ""
    userdata: str = ""

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict = None,
    ) -> dict:
        """Make an authenticated request to the Lambda Labs API."""
        url = f"{LAMBDA_API_BASE}{endpoint}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        resp = requests.request(method, url, headers=headers, json=json_data)
        if not resp.ok:
            # Log the actual error body before raising
            try:
                error_body = resp.json()
                print(f"Lambda API error: {error_body}")
            except Exception:
                print(f"Lambda API error (raw): {resp.text}")
            resp.raise_for_status()
        return resp.json()

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

    def create_instances(self) -> dict[str, dict]:
        """Create instances on Lambda Labs.

        Returns
        -------
        dict[str, dict]
            Mapping of instance IDs to runner metadata (label, labels, env_vars, action_sha).
        """
        from lambda_gha.log_constants import (
            LOG_PREFIX_JOB_COMPLETED,
            LOG_PREFIX_JOB_STARTED,
        )

        if not self.gh_runner_tokens:
            raise ValueError("No GitHub runner tokens provided")
        if not self.runner_release:
            raise ValueError("No runner release provided")
        if not self.instance_type:
            raise ValueError("No instance type provided")
        if not self.region:
            raise ValueError("No region provided")
        if not self.ssh_key_names:
            raise ValueError("No SSH key names provided")

        # Resolve action ref once (same for all instances)
        action_ref = environ.get("INPUT_ACTION_REF")
        if not action_ref:
            raise ValueError("action_ref is required")
        action_sha = resolve_ref_to_sha(action_ref)

        id_dict = {}

        for idx, token in enumerate(self.gh_runner_tokens):
            label = gh.GitHubInstance.generate_random_label()
            labels = f"{self.labels},{label}" if self.labels else label

            # Lambda Labs instance name (visible in dashboard)
            template_vars = self._get_template_vars(idx)
            instance_name = f"gha-{template_vars.get('repo', 'unknown')}-{template_vars.get('run', '0')}"
            if len(self.gh_runner_tokens) > 1:
                instance_name = f"{instance_name}-{idx}"

            # Launch instance via Lambda API
            payload = {
                "instance_type_name": self.instance_type,
                "region_name": self.region,
                "ssh_key_names": self.ssh_key_names,
                "quantity": 1,
                "name": instance_name,
            }

            print(f"Launching Lambda instance: {self.instance_type} in {self.region}")
            result = self._api_request("POST", "/instance-operations/launch", payload)

            if "data" not in result or "instance_ids" not in result["data"]:
                raise RuntimeError(f"Unexpected API response: {result}")

            instance_ids = result["data"]["instance_ids"]
            if not instance_ids:
                error_msg = result.get("error", {}).get("message", "Unknown error")
                raise RuntimeError(f"Failed to launch instance: {error_msg}")

            instance_id = instance_ids[0]
            print(f"Launched instance {instance_id}")

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
                "runner_labels": labels,
                "runner_poll_interval": self.runner_poll_interval,
                "runner_registration_timeout": environ.get("INPUT_RUNNER_REGISTRATION_TIMEOUT", "").strip() or RUNNER_REGISTRATION_TIMEOUT,
                "runner_release": self.runner_release,
                "runner_token": token,
                "userdata": self.userdata or "",
            }

            id_dict[instance_id] = {
                "label": label,
                "labels": labels,
                "env_vars": env_vars,
                "action_sha": action_sha,
            }

        return id_dict

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

        while pending and (time.time() - start_time) < timeout:
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
                        print(f"Instance {instance_id} is ready: {instance.get('ip')}")
                    elif status in ("terminated", "terminating"):
                        raise RuntimeError(f"Instance {instance_id} terminated unexpectedly")
                    else:
                        print(f"Instance {instance_id} status: {status}")
                except requests.HTTPError as e:
                    if e.response.status_code == 404:
                        print(f"Instance {instance_id} not found yet, retrying...")
                    else:
                        raise

            if pending:
                time.sleep(INSTANCE_POLL_INTERVAL)

        if pending:
            raise TimeoutError(f"Instances did not become ready within {timeout}s: {pending}")

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

        # Build env export commands
        env_exports = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())

        # Script URL from GitHub
        script_url = f"https://raw.githubusercontent.com/Open-Athena/lambda-gha/{action_sha}/src/lambda_gha/scripts/runner-setup.sh"

        # Build the setup command: export vars, fetch script, run it
        setup_cmd = f'''
{env_exports}
curl -sSL "{script_url}" -o /tmp/runner-setup.sh
chmod +x /tmp/runner-setup.sh
sudo -E nohup /tmp/runner-setup.sh > /var/log/runner-setup.log 2>&1 &
'''

        print(f"Executing setup script from {script_url}...")
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
