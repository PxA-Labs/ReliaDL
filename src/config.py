"""
Configuration loader, parser, and unit conversion utilities for ReliaDL.
Supports YAML configuration hierarchies, environment variable overrides,
byte-string unit conversions, and validation into DownloadConfig models.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from src.exceptions import ConfigurationError
from src.models import DownloadConfig


# Unit multiplier lookup table for byte conversion
_BYTE_UNITS = {
    "": 1,
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
    "t": 1024 * 1024 * 1024 * 1024,
    "tb": 1024 * 1024 * 1024 * 1024,
    "tib": 1024 * 1024 * 1024 * 1024,
}


def parse_size(size_val: Union[str, int, float]) -> int:
    """
    Parse a human-readable size string into byte count integer.

    Supports units: B, KB, KiB, MB, MiB, GB, GiB, TB, TiB (case-insensitive).
    Examples:
        parse_size("8MB") -> 8388608
        parse_size("1.5 GB") -> 1610612736
        parse_size(1024) -> 1024

    Raises:
        ConfigurationError: If the string cannot be parsed or represents negative size.
    """
    if isinstance(size_val, (int, float)):
        if size_val < 0:
            raise ConfigurationError(
                f"Byte size cannot be negative, got {size_val}",
                parameter="size",
                value=size_val,
            )
        return int(size_val)

    if not isinstance(size_val, str) or not size_val.strip():
        raise ConfigurationError(
            f"Invalid size value '{size_val}'. Must be non-empty string or integer.",
            parameter="size",
            value=size_val,
        )

    cleaned = size_val.strip().lower()
    pattern = r"^([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)$"
    match = re.match(pattern, cleaned)

    if not match:
        raise ConfigurationError(
            f"Unable to parse size string: '{size_val}'. Expected format like '8MB' or '512KB'.",
            parameter="size",
            value=size_val,
        )

    number_str, unit_str = match.groups()
    try:
        number = float(number_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid numeric component in size '{size_val}': {e}",
            parameter="size",
            value=size_val,
        ) from e

    if unit_str not in _BYTE_UNITS:
        raise ConfigurationError(
            f"Unknown size unit '{unit_str}' in '{size_val}'. Supported units: B, KB, MB, GB, TB.",
            parameter="size",
            value=size_val,
        )

    byte_count = int(number * _BYTE_UNITS[unit_str])
    if byte_count < 0:
        raise ConfigurationError(
            f"Byte size cannot be negative, got {byte_count}",
            parameter="size",
            value=size_val,
        )

    return byte_count


def format_size(size_bytes: int) -> str:
    """
    Format byte count integer into a readable string (e.g. '8.00 MB').
    """
    if size_bytes < 0:
        raise ConfigurationError(f"Size cannot be negative, got {size_bytes}")

    units = [("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)]
    for unit_name, unit_val in units:
        if size_bytes >= unit_val:
            val = size_bytes / unit_val
            return f"{val:.2f} {unit_name}"
    return f"{size_bytes} B"


def load_yaml_file(file_path: Union[str, Path]) -> dict[str, Any]:
    """
    Safely load a YAML configuration file from disk.

    Raises:
        ConfigurationError: If file not found or invalid YAML syntax.
    """
    p = Path(file_path)
    if not p.is_file():
        raise ConfigurationError(
            f"Configuration file does not exist: {file_path}",
            parameter="config_path",
            value=str(file_path),
        )

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigurationError(
            f"YAML parsing error in {file_path}: {e}",
            parameter="config_path",
            value=str(file_path),
        ) from e

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"YAML root must be a mapping/dictionary, got {type(data).__name__} in {file_path}",
            parameter="config_path",
            value=str(file_path),
        )

    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge override dictionary into base dictionary without mutating base.
    """
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce_env_value(val_str: str) -> Any:
    """Coerce string from environment variable to appropriate Python type."""
    v_clean = val_str.strip()
    v_lower = v_clean.lower()

    if v_lower in ("true", "yes", "1", "on"):
        return True
    if v_lower in ("false", "no", "0", "off"):
        return False
    if v_lower in ("null", "none"):
        return None

    try:
        return int(v_clean)
    except ValueError:
        pass

    try:
        return float(v_clean)
    except ValueError:
        pass

    return v_clean


def apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """
    Apply environment variable overrides to a configuration dictionary.

    Supports two patterns:
    1. Hierarchical prefix:
       `RELIADL_<SECTION>__<KEY>` or `CHUNKGUARD_<SECTION>__<KEY>`
       e.g., `CHUNKGUARD_DOWNLOAD__CHUNK_SIZE="16MB"`
             `RELIADL_NETWORK__MAX_REDIRECTS="10"`
    2. Common shorthand variables:
       `RELIADL_CHUNK_SIZE`, `RELIADL_WORKERS`, `RELIADL_RETRIES`, `RELIADL_USER_AGENT`,
       `RELIADL_NO_VERIFY`, `RELIADL_HTTP2`, `RELIADL_VERIFY_SSL`, `RELIADL_LOG_LEVEL`, etc.
    """
    result = deep_merge({}, config)

    # 1. Process hierarchical overrides
    prefixes = ("RELIADL_", "CHUNKGUARD_")
    for env_k, env_v in os.environ.items():
        matched_prefix = None
        for pfx in prefixes:
            if env_k.startswith(pfx):
                matched_prefix = pfx
                break

        if matched_prefix and "__" in env_k:
            key_path = env_k[len(matched_prefix):].lower().split("__")
            coerced_val = _coerce_env_value(env_v)

            # Traverse and insert into nested dict
            curr = result
            for part in key_path[:-1]:
                if part not in curr or not isinstance(curr[part], dict):
                    curr[part] = {}
                curr = curr[part]
            curr[key_path[-1]] = coerced_val

    # 2. Process documented shorthand overrides
    shorthand_mappings = [
        (("RELIADL_CHUNK_SIZE", "CHUNKGUARD_CHUNK_SIZE"), ("download", "chunk_size")),
        (("RELIADL_WORKERS", "CHUNKGUARD_WORKERS"), ("download", "max_parallel_workers")),
        (("RELIADL_RETRIES", "CHUNKGUARD_RETRIES"), ("retry", "max_attempts")),
        (("RELIADL_LOG_LEVEL", "CHUNKGUARD_LOG_LEVEL"), ("logging", "level")),
        (("RELIADL_LOG_FORMAT", "CHUNKGUARD_LOG_FORMAT"), ("logging", "format")),
        (("RELIADL_HTTP2", "CHUNKGUARD_HTTP2"), ("network", "http2")),
        (("RELIADL_VERIFY_SSL", "CHUNKGUARD_VERIFY_SSL"), ("network", "verify_ssl")),
        (("RELIADL_USER_AGENT", "CHUNKGUARD_USER_AGENT"), ("network", "user_agent")),
    ]

    for env_vars, (section, key) in shorthand_mappings:
        for ev in env_vars:
            if ev in os.environ:
                if section not in result or not isinstance(result[section], dict):
                    result[section] = {}
                result[section][key] = _coerce_env_value(os.environ[ev])
                break

    # Boolean flags
    for ev in ("RELIADL_NO_VERIFY", "CHUNKGUARD_NO_VERIFY"):
        if ev in os.environ:
            val = _coerce_env_value(os.environ[ev])
            if "download" not in result or not isinstance(result["download"], dict):
                result["download"] = {}
            result["download"]["verify_on_complete"] = not bool(val)
            break

    return result


def find_default_config_path() -> Optional[Path]:
    """Find the default configuration file location."""
    candidates = [
        Path("config/default_config.yaml"),
        Path(__file__).resolve().parent.parent / "config" / "default_config.yaml",
        Path("./default_config.yaml"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def find_override_config_path(explicit_path: Optional[Union[str, Path]] = None) -> Optional[Path]:
    """Find local user override configuration path."""
    if explicit_path:
        return Path(explicit_path)

    for env_var in ("RELIADL_CONFIG", "CHUNKGUARD_CONFIG"):
        if env_var in os.environ and os.environ[env_var].strip():
            return Path(os.environ[env_var].strip())

    candidates = [
        Path("./ReliaDL.yaml"),
        Path("./reliadl.yaml"),
        Path("./chunkguard.yaml"),
        Path("./.reliadl.yaml"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(
    config_path: Optional[Union[str, Path]] = None,
    env_overrides: bool = True,
) -> dict[str, Any]:
    """
    Load merged configuration dictionary from default YAML, local override YAML,
    and active environment variables.
    """
    merged: dict[str, Any] = {}

    # 1. Load default config
    default_path = find_default_config_path()
    if default_path:
        merged = load_yaml_file(default_path)

    # 2. Load custom / local override
    override_path = find_override_config_path(config_path)
    if override_path:
        override_data = load_yaml_file(override_path)
        merged = deep_merge(merged, override_data)

    # 3. Apply environment variables
    if env_overrides:
        merged = apply_env_overrides(merged)

    return merged


def get_download_config(
    config_path: Optional[Union[str, Path]] = None,
    overrides: Optional[dict[str, Any]] = None,
    env_overrides: bool = True,
) -> DownloadConfig:
    """
    Load, merge, and validate configuration into a DownloadConfig instance.

    Transforms nested YAML configuration keys into flat DownloadConfig model parameters.
    """
    raw = load_config(config_path=config_path, env_overrides=env_overrides)
    if overrides:
        raw = deep_merge(raw, overrides)

    download_sec = raw.get("download", {})
    network_sec = raw.get("network", {})
    retry_sec = raw.get("retry", {})
    progress_sec = raw.get("progress", {})

    chunk_size_raw = download_sec.get("chunk_size", "8MB")
    chunk_size_bytes = parse_size(chunk_size_raw)

    proxy_sec = network_sec.get("proxy", {})
    proxy_url = (
        proxy_sec.get("https_proxy")
        or proxy_sec.get("http_proxy")
        or proxy_sec.get("socks_proxy")
    )

    model_kwargs: dict[str, Any] = {
        "chunk_size_bytes": chunk_size_bytes,
        "max_parallel_workers": download_sec.get("max_parallel_workers", 4),
        "verify_on_complete": download_sec.get("verify_on_complete", True),
        "cleanup_chunks_on_complete": download_sec.get("cleanup_chunks_on_complete", True),
        "pre_check_disk_space": download_sec.get("pre_check_disk_space", True),
        "direct_write": download_sec.get("direct_write", False),
        "connect_timeout_seconds": float(network_sec.get("connect_timeout", 30)),
        "read_timeout_seconds": float(network_sec.get("read_timeout", 300)),
        "max_redirects": int(network_sec.get("max_redirects", 5)),
        "user_agent": network_sec.get("user_agent", "ReliaDL/1.0"),
        "http2": network_sec.get("http2", True),
        "verify_ssl": network_sec.get("verify_ssl", True),
        "max_bandwidth_bytes_per_sec": parse_size(network_sec.get("max_bandwidth", 0)),
        "proxy_url": proxy_url,
        "max_retries_per_chunk": retry_sec.get("max_attempts", 3),
        "retry_base_delay_seconds": float(retry_sec.get("base_delay", 1.0)),
        "retry_max_delay_seconds": float(retry_sec.get("max_delay", 60.0)),
        "retry_backoff_factor": float(retry_sec.get("backoff_factor", 2.0)),
        "retry_jitter_factor": float(retry_sec.get("jitter_factor", 0.5)),
        "progress_update_interval_seconds": float(progress_sec.get("update_interval", 0.5)),
    }

    config = DownloadConfig(**model_kwargs)
    config.validate()
    return config
