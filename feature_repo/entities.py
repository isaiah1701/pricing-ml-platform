"""Entity declarations for the Airbnb feature repository."""

from feast import Entity
from feast.value_type import ValueType

listing: Entity = Entity(
    name="listing",
    join_keys=["id"],
    value_type=ValueType.INT64,
    description="A unique NYC Airbnb listing.",
)
