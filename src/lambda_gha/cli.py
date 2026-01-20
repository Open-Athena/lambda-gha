#!/usr/bin/env python
"""Lambda Labs API CLI."""
from json import dumps
from os import environ

from click import argument, group, option

from lambda_gha.defaults import LAMBDA_API_BASE
from lambda_gha.start import StartLambdaLabs


def get_api_key():
    key = environ.get("LAMBDA_API_KEY")
    if not key:
        raise SystemExit("LAMBDA_API_KEY environment variable not set")
    return key


def api_request(method, endpoint, json=None):
    """Make an API request and return the response."""
    import requests
    url = f"{LAMBDA_API_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {get_api_key()}"}
    resp = requests.request(method, url, headers=headers, json=json)
    return resp.json()


def pj(data):
    """Print JSON data."""
    print(dumps(data, indent=2))


@group()
def cli():
    """Lambda Labs API CLI."""
    pass


@cli.command("ls")
def list_instances():
    """List running instances."""
    pj(api_request("GET", "/instances"))


@cli.command("types")
@option('-a', '--available', is_flag=True, help="Only show available types")
def list_types(available):
    """List instance types."""
    data = api_request("GET", "/instance-types")
    if available:
        data = {"data": {k: v for k, v in data.get("data", {}).items() if v.get("regions_with_capacity_available")}}
    pj(data)


@cli.command("get")
@argument("instance_id")
def get_instance(instance_id):
    """Get instance details."""
    pj(api_request("GET", f"/instances/{instance_id}"))


@cli.command("launch")
@option('-n', '--name', help="Instance name")
@option('-q', '--quantity', default=1, help="Number of instances")
@option('-r', '--region', default="us-south-1", help="Region name")
@option('-t', '--type', 'instance_type', default="gpu_1x_a10", help="Instance type")
@argument("ssh_key_names", nargs=-1, required=True)
def launch(instance_type, name, quantity, region, ssh_key_names):
    """Launch instance(s). Pass SSH key name(s) as arguments."""
    payload = {
        "instance_type_name": instance_type,
        "region_name": region,
        "ssh_key_names": list(ssh_key_names),
        "quantity": quantity,
    }
    if name:
        payload["name"] = name
    pj(api_request("POST", "/instance-operations/launch", json=payload))


@cli.command("term")
@argument("instance_ids", nargs=-1, required=True)
def terminate(instance_ids):
    """Terminate instance(s)."""
    pj(api_request("POST", "/instance-operations/terminate", json={"instance_ids": list(instance_ids)}))


@cli.command("ssh-keys")
def list_ssh_keys():
    """List SSH keys."""
    pj(api_request("GET", "/ssh-keys"))


@cli.command("add-ssh-key")
@option('-n', '--name', required=True, help="Key name")
@argument("public_key_file")
def add_ssh_key(name, public_key_file):
    """Add an SSH key from a file."""
    from pathlib import Path
    pub_key = Path(public_key_file).expanduser().read_text().strip()
    pj(api_request("POST", "/ssh-keys", json={"name": name, "public_key": pub_key}))


if __name__ == "__main__":
    cli()
