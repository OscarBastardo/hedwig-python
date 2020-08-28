import json
from collections import namedtuple
from distutils.version import StrictVersion
from typing import Any, Tuple, Union, Dict, Pattern

from hedwig.conf import settings
from hedwig.exceptions import ValidationError
from hedwig.models import Message, Metadata

MetaAttributes = namedtuple('MetaAttributes', ['timestamp', 'publisher', 'headers', 'id', 'schema', 'format_version'])


class HedwigBaseValidator:
    """
    Base class responsible for serializing / encoding and deserializing / decoding messages into / from format on the
    wire.
    """

    _schema_re: Pattern
    """
    A regex that matches encoded schema and matches 2 groups: message_type, message_version
    """

    _schema_fmt: str
    """
    A f-string that is used to encode schema that contains two placeholders: message_type, message_version
    """

    _current_format_version: StrictVersion

    def __init__(self, schema_fmt: str, schema_re: Pattern, current_format_version: StrictVersion):
        self._schema_fmt = schema_fmt
        self._schema_re = schema_re
        self._current_format_version = current_format_version

    def _extract_data(self, payload: Union[str, bytes], attributes: dict) -> Tuple[MetaAttributes, Any]:
        """
        Extracts data from the on-the-wire payload
        """
        raise NotImplementedError

    def _decode_data(
        self,
        meta_attrs: MetaAttributes,
        message_type: str,
        full_version: StrictVersion,
        data: Any,
        verify_known_minor_version: bool,
    ) -> Any:
        """
        Validates decoded data
        """
        raise NotImplementedError

    def _encode_message_type(self, message_type: str, version: StrictVersion) -> str:
        """
        Encodes message type in outgoing message attribute
        """
        return self._schema_fmt.format(message_type=message_type, message_version=version)

    def _decode_message_type(self, schema: str) -> Tuple[str, StrictVersion]:
        """
        Decode message type from meta attributes
        """
        try:
            m = self._schema_re.search(schema)
            if m is None:
                raise ValueError
            schema_groups = m.groups()
            message_type = schema_groups[0]
            full_version = StrictVersion(schema_groups[1])
        except (AttributeError, ValueError):
            raise ValidationError(f'Invalid schema found: {schema}')
        return message_type, full_version

    def deserialize(
        self,
        message_payload: Union[str, bytes],
        attributes: dict,
        provider_metadata: Any,
        verify_known_minor_version: bool = False,
    ) -> Message:
        """
        Deserialize a message from the on-the-wire format
        :param message_payload: Raw message payload as received from the backend
        :param provider_metadata: Provider specific metadata
        :param attributes: Message attributes from the transport backend
        :param verify_known_minor_version: If set to true, verifies that this minor version is known
        :returns: Message object if valid
        :raise: :class:`hedwig.ValidationError` if validation fails.
        """
        meta_attrs, extracted_data = self._extract_data(message_payload, attributes)
        message_type, version = self._decode_message_type(meta_attrs.schema)
        data = self._decode_data(meta_attrs, message_type, version, extracted_data, verify_known_minor_version)

        return Message(
            id=meta_attrs.id,
            metadata=Metadata(
                timestamp=meta_attrs.timestamp,
                headers=meta_attrs.headers,
                publisher=meta_attrs.publisher,
                provider_metadata=provider_metadata,
            ),
            data=data,
            type=message_type,
            version=version,
        )

    def _encode_payload(self, meta_attrs: MetaAttributes, data: Any) -> Tuple[Union[str, bytes], dict]:
        """
        Encodes on-the-wire payload
        """
        raise NotImplementedError

    def serialize(self, message: Message) -> Tuple[Union[str, bytes], dict]:
        """
        Serialize a message for appropriate on-the-wire format
        :return: Tuple of message payload and transport attributes
        """
        schema = self._encode_message_type(message.type, message.version)
        meta_attrs = MetaAttributes(
            message.timestamp, message.publisher, message.headers, message.id, schema, self._current_format_version,
        )
        message_payload, msg_attrs = self._encode_payload(meta_attrs, message.data)
        # validate payload from scratch before publishing
        self.deserialize(message_payload, msg_attrs, None, verify_known_minor_version=True)
        return message_payload, msg_attrs

    def _decode_meta_attributes(self, attributes: Dict[str, str]) -> MetaAttributes:
        """
        Decodes meta attributes from transport attributes
        :param attributes: Message attributes from the transport backend
        :return:
        """
        assert settings.HEDWIG_USE_TRANSPORT_MESSAGE_ATTRIBUTES

        for attr in (
            'hedwig_format_version',
            'hedwig_headers',
            'hedwig_id',
            'hedwig_message_timestamp',
            'hedwig_publisher',
            'hedwig_schema',
        ):
            value = attributes.get(attr)
            if not isinstance(value, str):
                raise ValidationError(f"Invalid message attribute: {attr} must be string, found: {value}")

        return MetaAttributes(
            int(attributes['hedwig_message_timestamp']),
            attributes['hedwig_publisher'],
            json.loads(attributes['hedwig_headers']),
            attributes['hedwig_id'],
            attributes['hedwig_schema'],
            StrictVersion(attributes['hedwig_format_version']),
        )

    def _encode_meta_attributes(self, meta_attrs: MetaAttributes) -> Dict[str, str]:
        """
        Encodes meta attributes as transport attributes
        :param meta_attrs:
        :return:
        """
        assert settings.HEDWIG_USE_TRANSPORT_MESSAGE_ATTRIBUTES

        return {
            'hedwig_format_version': str(meta_attrs.format_version),
            'hedwig_headers': json.dumps(meta_attrs.headers, allow_nan=False, separators=(',', ':'), indent=None),
            'hedwig_id': str(meta_attrs.id),
            'hedwig_message_timestamp': str(meta_attrs.timestamp),
            'hedwig_publisher': meta_attrs.publisher,
            'hedwig_schema': meta_attrs.schema,
        }