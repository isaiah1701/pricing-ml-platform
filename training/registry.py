"""Safe helpers for publishing governed MLflow Registry aliases."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

LOGGER = logging.getLogger(__name__)


class RegistryAliasClient(Protocol):
    """Describe the Registry mutations needed for governed alias publication."""

    def set_model_version_tag(
        self,
        name: str,
        version: str,
        key: str,
        value: str,
    ) -> None:
        """Set one model-version metadata value."""
        ...

    def set_registered_model_alias(
        self,
        name: str,
        alias: str,
        version: str,
    ) -> None:
        """Point an alias at one concrete model version."""
        ...


def tag_version_before_alias(
    client: RegistryAliasClient,
    model_name: str,
    version: str,
    alias: str,
    tags: Mapping[str, str],
) -> None:
    """Write all governance metadata before exposing a version by alias."""
    invalid = sorted(
        key for key, value in tags.items() if not key or not value.strip()
    )
    if invalid or not tags:
        LOGGER.error("Registry alias publication has invalid tags: %s", invalid)
        raise ValueError(
            "Registry alias publication requires non-empty governance tags"
        )
    for key, value in tags.items():
        client.set_model_version_tag(model_name, version, key, value)
    client.set_registered_model_alias(model_name, alias, version)
    LOGGER.info(
        "Published Registry alias %s for governed model %s version %s",
        alias,
        model_name,
        version,
    )
