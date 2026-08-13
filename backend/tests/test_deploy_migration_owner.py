"""Production migrations and destructive roster maintenance have one owner."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_long_lived_image_does_not_run_alembic_on_start():
    dockerfile = (ROOT / "deploy" / "backend" / "Dockerfile").read_text()
    command_lines = [line for line in dockerfile.splitlines()
                     if line.lstrip().startswith(("CMD", "ENTRYPOINT"))]
    assert command_lines
    assert all("alembic" not in line for line in command_lines)


def test_bootstrap_is_the_only_compose_alembic_owner():
    import yaml

    compose = yaml.safe_load(
        (ROOT / "deploy" / "backend" / "docker-compose.yml").read_text()
    )
    owners = [
        name for name, service in compose["services"].items()
        if "alembic" in str(service.get("command") or "")
    ]
    assert owners == ["bootstrap"]
