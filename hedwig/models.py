import copy
import dataclasses
import time
import uuid
from concurrent.futures import Future
from distutils.version import StrictVersion
from enum import Enum
from functools import lru_cache
from typing import Union, Optional, Any, cast, Tuple

from hedwig.backends.utils import get_consumer_backend
from hedwig.conf import settings
from hedwig.exceptions import ValidationError, CallbackNotFound


@lru_cache(maxsize=1)
def _validator():
    return settings.HEDWIG_DATA_VALIDATOR_CLASS()


@dataclasses.dataclass(frozen=True)
class Metadata:
    timestamp: int = dataclasses.field(default_factory=lambda: int(time.time() * 1000))
    """
    Timestamp of message creation in epoch milliseconds
    """

    publisher: str = dataclasses.field(default_factory=lambda: settings.HEDWIG_PUBLISHER)
    """
    Publisher of message
    """

    headers: dict = dataclasses.field(default_factory=dict)
    """
    Custom headers sent with the message
    """

    provider_metadata: Any = None
    """
    Provider specific metadata, such as SQS Receipt, or Google ack id. This may be used to extend message
    visibility if the task is running longer than expected using :meth:`Message.extend_visibility_timeout`
    """


@dataclasses.dataclass(frozen=True)
class Message:
    """
    Model for Hedwig messages.
    A Message object will always have known message schema and schema version even if the data _may_ not be valid.
    """

    data: Any
    """
    Message data
    """

    type: str = dataclasses.field()
    """
    Message type. May be none if message is invalid
    """

    version: StrictVersion = dataclasses.field()
    """
    `StrictVersion` object representing data schema version.
    """

    id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    """
    Message identifier
    """

    metadata: Metadata = dataclasses.field(default_factory=Metadata)
    """
    Message metadata
    """

    @staticmethod
    def deserialize(payload: str, attributes: dict, provider_metadata: Any) -> 'Message':
        """
        :raise: :class:`hedwig.ValidationError` if validation fails.
        """
        return _validator().deserialize(payload, attributes, provider_metadata)

    def exec_callback(self) -> None:
        """
        Call the callback with this message
        """
        self.callback.call(self)

    @classmethod
    def new(
        cls, msg_type: Union[str, Enum], version: StrictVersion, data: Any, msg_id: str = None, headers: dict = None,
    ) -> 'Message':
        """
        Creates Message object given type, data schema version and data. This is typically used by the publisher code.

        :param msg_type: message type (could be an enum, it's value will be used)
        :param version: StrictVersion representing data schema
        :param data: The dict to pass in `data` field of Message.
        :param msg_id: Custom message identifier. If not passed, a randomly generated uuid will be used.
        :param headers: Custom headers
        """
        assert isinstance(msg_type, (str, Enum))
        assert isinstance(version, StrictVersion)
        assert isinstance(data, dict)
        assert isinstance(msg_id, (type(None), str))
        assert isinstance(headers, (type(None), dict))

        if isinstance(msg_type, Enum):
            msg_type = msg_type.value

        return Message(
            id=msg_id or str(uuid.uuid4()),
            type=cast(str, msg_type),
            version=version,
            metadata=Metadata(headers=headers or {}),
            data=copy.deepcopy(data),
        )

    def publish(self) -> Union[str, Future]:
        """
        Publish this message on Hedwig infra
        :returns: for async publishers, returns a future that represents the publish api call, otherwise, returns
        the published message id
        """
        from hedwig.publisher import publish

        return publish(self)

    def extend_visibility_timeout(self, visibility_timeout_s: int) -> None:
        """
        Extends visibility timeout of a message for long running tasks.
        """
        consumer_backend = get_consumer_backend()
        consumer_backend.extend_visibility_timeout(visibility_timeout_s, self.provider_metadata)

    @property
    def callback(self):
        from hedwig.callback import Callback

        try:
            return Callback.find_by_message(self.type, self.major_version)
        except CallbackNotFound:
            raise ValidationError

    @property
    def major_version(self) -> int:
        return self.version.version[0]

    @property
    def timestamp(self) -> int:
        return self.metadata.timestamp

    @property
    def headers(self) -> dict:
        return self.metadata.headers

    @property
    def provider_metadata(self):
        return self.metadata.provider_metadata

    @property
    def publisher(self) -> Optional[str]:
        return self.metadata.publisher

    @property
    def topic(self) -> str:
        """
        The SNS topic name for routing the message
        """
        version_pattern = f'{self.major_version}.*'
        return settings.HEDWIG_MESSAGE_ROUTING[(self.type, version_pattern)]

    def serialize(self) -> Tuple[str, dict]:
        return _validator().serialize(self)

    def with_headers(self, new_headers: dict) -> 'Message':
        """
        Creates a copy of the message with different headers.
        :param new_headers:
        :return:
        """
        return dataclasses.replace(self, metadata=dataclasses.replace(self.metadata, headers=new_headers))

    def with_provider_metadata(self, new_provider_metadata: Any) -> 'Message':
        """
        Creates a copy of the message with different provider metadata.
        :param new_provider_metadata:
        :return:
        """
        return dataclasses.replace(
            self, metadata=dataclasses.replace(self.metadata, provider_metadata=new_provider_metadata)
        )
