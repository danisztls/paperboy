"""Config file loading (YAML or JSON, with `!secret` support)."""

import json
import pathlib


def _make_secret_loader(secrets: dict | None, secrets_path: pathlib.Path) -> type:
    import yaml

    class _SecretLoader(yaml.SafeLoader):
        pass

    def _secret_constructor(loader, node):
        key = loader.construct_scalar(node)
        if secrets is None:
            raise ValueError(f"!secret {key!r} used in config but {secrets_path} does not exist")
        if key not in secrets:
            raise ValueError(f"!secret {key!r} not found in {secrets_path}")
        return secrets[key]

    _SecretLoader.add_constructor("!secret", _secret_constructor)
    return _SecretLoader


def load_config(path: pathlib.Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        secrets_path = path.parent / "secrets.yaml"
        secrets = yaml.safe_load(secrets_path.read_text()) or {} if secrets_path.exists() else None
        return yaml.load(text, Loader=_make_secret_loader(secrets, secrets_path))
    return json.loads(text)
