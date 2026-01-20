from unittest.mock import patch, mock_open, Mock

import pytest
import responses

from lambda_gha.start import StartLambdaLabs, resolve_ref_to_sha
from lambda_gha.defaults import LAMBDA_API_BASE


@pytest.fixture(scope="function")
def base_lambda_params():
    """Base parameters for StartLambdaLabs initialization"""
    return {
        "api_key": "test-api-key",
        "gh_runner_tokens": ["test-token"],
        "instance_type": "gpu_1x_a10",
        "region": "us-south-1",
        "repo": "Open-Athena/lambda-gha",
        "runner_grace_period": "60",
        "runner_release": "https://example.com/runner.tar.gz",
        "ssh_key_names": ["test-key"],
    }


@pytest.fixture(scope="function")
def mock_git_commands():
    """Mock git commands for action ref resolution"""
    def mock_subprocess_run(cmd, *args, **kwargs):
        if cmd[0] == 'git' and cmd[1] == 'config':
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            return mock_result
        elif cmd[0] == 'git' and cmd[1] == 'rev-parse':
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "abc123def456789012345678901234567890abcd\n"
            mock_result.stderr = ""
            return mock_result
        else:
            raise ValueError(f"Unexpected subprocess call: {cmd}")
    return mock_subprocess_run


@pytest.fixture(scope="function")
def lambda_starter(base_lambda_params, mock_git_commands, monkeypatch):
    """Create a StartLambdaLabs instance with mocked dependencies"""
    monkeypatch.setenv("INPUT_ACTION_REF", "main")
    with patch("lambda_gha.start.subprocess.run", side_effect=mock_git_commands):
        yield StartLambdaLabs(**base_lambda_params)


def test_resolve_ref_to_sha(mock_git_commands):
    """Test that git ref is resolved to SHA"""
    with patch("lambda_gha.start.subprocess.run", side_effect=mock_git_commands):
        sha = resolve_ref_to_sha("main")
        assert sha == "abc123def456789012345678901234567890abcd"


@responses.activate
def test_create_instances(lambda_starter, monkeypatch):
    """Test instance creation via Lambda API"""
    monkeypatch.setenv("INPUT_ACTION_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Open-Athena/lambda-gha")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "42")

    # Mock Lambda API launch endpoint
    responses.add(
        responses.POST,
        f"{LAMBDA_API_BASE}/instance-operations/launch",
        json={"data": {"instance_ids": ["i-test-123"]}},
        status=200,
    )

    result = lambda_starter.create_instances()

    assert "i-test-123" in result
    assert "labels" in result["i-test-123"]
    assert "env_vars" in result["i-test-123"]
    assert "action_sha" in result["i-test-123"]


@responses.activate
def test_create_instances_api_error(lambda_starter, monkeypatch):
    """Test handling of API error during instance creation"""
    monkeypatch.setenv("INPUT_ACTION_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Open-Athena/lambda-gha")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "42")

    # Mock Lambda API with empty instance_ids (error case)
    responses.add(
        responses.POST,
        f"{LAMBDA_API_BASE}/instance-operations/launch",
        json={"data": {"instance_ids": []}, "error": {"message": "No capacity"}},
        status=200,
    )

    with pytest.raises(RuntimeError, match="Failed to launch instance"):
        lambda_starter.create_instances()


def test_create_instances_missing_tokens(lambda_starter):
    """Test that missing tokens raises an error"""
    lambda_starter.gh_runner_tokens = []
    with pytest.raises(ValueError, match="No GitHub runner tokens provided"):
        lambda_starter.create_instances()


def test_create_instances_missing_instance_type(lambda_starter):
    """Test that missing instance type raises an error"""
    lambda_starter.instance_type = ""
    with pytest.raises(ValueError, match="No instance type provided"):
        lambda_starter.create_instances()


def test_create_instances_missing_region(lambda_starter):
    """Test that missing region raises an error"""
    lambda_starter.region = ""
    with pytest.raises(ValueError, match="No region provided"):
        lambda_starter.create_instances()


def test_create_instances_missing_ssh_keys(lambda_starter):
    """Test that missing SSH keys raises an error"""
    lambda_starter.ssh_key_names = []
    with pytest.raises(ValueError, match="No SSH key names provided"):
        lambda_starter.create_instances()


def test_create_instances_missing_runner_release(lambda_starter):
    """Test that missing runner release raises an error"""
    lambda_starter.runner_release = ""
    with pytest.raises(ValueError, match="No runner release provided"):
        lambda_starter.create_instances()


@responses.activate
def test_wait_until_ready(lambda_starter):
    """Test waiting for instance to become ready"""
    instance_id = "i-test-123"

    # First call: instance is booting
    responses.add(
        responses.GET,
        f"{LAMBDA_API_BASE}/instances/{instance_id}",
        json={"data": {"status": "booting"}},
        status=200,
    )
    # Second call: instance is active
    responses.add(
        responses.GET,
        f"{LAMBDA_API_BASE}/instances/{instance_id}",
        json={"data": {"status": "active", "ip": "1.2.3.4", "hostname": "test.lambda"}},
        status=200,
    )

    result = lambda_starter.wait_until_ready([instance_id], timeout=30)

    assert instance_id in result
    assert result[instance_id]["ip"] == "1.2.3.4"
    assert result[instance_id]["status"] == "active"


@responses.activate
def test_wait_until_ready_terminated(lambda_starter):
    """Test that terminated instance raises an error"""
    instance_id = "i-test-123"

    responses.add(
        responses.GET,
        f"{LAMBDA_API_BASE}/instances/{instance_id}",
        json={"data": {"status": "terminated"}},
        status=200,
    )

    with pytest.raises(RuntimeError, match="terminated unexpectedly"):
        lambda_starter.wait_until_ready([instance_id], timeout=10)


@responses.activate
def test_terminate_instances(lambda_starter):
    """Test instance termination via Lambda API"""
    responses.add(
        responses.POST,
        f"{LAMBDA_API_BASE}/instance-operations/terminate",
        json={"data": {"terminated_instances": [{"id": "i-test-123"}]}},
        status=200,
    )

    result = lambda_starter.terminate_instances(["i-test-123"])

    assert "data" in result


def test_set_instance_mapping(lambda_starter, monkeypatch):
    """Test setting GitHub Actions output for instance mapping"""
    monkeypatch.setenv("GITHUB_OUTPUT", "mock_output_file")
    mapping = {
        "i-test-123": {
            "label": "random-label",
            "labels": "gpu,random-label",
            "env_vars": {},
            "action_sha": "abc123",
        }
    }
    mock_file = mock_open()

    with patch("builtins.open", mock_file):
        lambda_starter.set_instance_mapping(mapping)

    # Should be called 3 times for single instance (mtx, instance-id, label)
    assert mock_file.call_count == 3


def test_set_instance_mapping_multiple(lambda_starter, monkeypatch):
    """Test setting GitHub Actions output for multiple instances"""
    monkeypatch.setenv("GITHUB_OUTPUT", "mock_output_file")
    mapping = {
        "i-test-123": {
            "label": "label1",
            "labels": "gpu,label1",
            "env_vars": {},
            "action_sha": "abc123",
        },
        "i-test-456": {
            "label": "label2",
            "labels": "gpu,label2",
            "env_vars": {},
            "action_sha": "abc123",
        },
    }
    mock_file = mock_open()

    with patch("builtins.open", mock_file):
        lambda_starter.set_instance_mapping(mapping)

    # Should be called 1 time for multiple instances (mtx only)
    assert mock_file.call_count == 1


@responses.activate
def test_api_request_auth_header(lambda_starter):
    """Test that API requests include proper authorization header"""
    responses.add(
        responses.GET,
        f"{LAMBDA_API_BASE}/instances/i-test",
        json={"data": {}},
        status=200,
    )

    lambda_starter._api_request("GET", "/instances/i-test")

    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["Authorization"] == "Bearer test-api-key"
