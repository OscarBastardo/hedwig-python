import re
from copy import deepcopy
from distutils.version import StrictVersion
from importlib import import_module
from types import ModuleType
from typing import Tuple, Union

import funcy
from google.protobuf.any_pb2 import Any
from google.protobuf.message import DecodeError, Message as ProtoMessage

from hedwig import options_pb2
from hedwig.conf import settings
from hedwig.exceptions import ValidationError
from hedwig.validators.base import HedwigBaseValidator, MetaAttributes
from hedwig.container_pb2 import PayloadV1


class SchemaError(Exception):
    pass


class ProtobufValidator(HedwigBaseValidator):
    schema_module: ModuleType
    """
    The module that contains protoc compiled python classes - supplied by app
    """

    _version_pattern_re = re.compile(r"^([0-9]+)\.\*$")

    def __init__(self, schema_module: ModuleType = None) -> None:
        # schema encoding, eg: hedwig.automatic.com/schema#/schemas/trip.created/1.0
        schema_fmt = '{message_type}/{message_version}'
        schema_re = re.compile(r'([^/]+)/([^/]+)$')

        super().__init__(schema_fmt, schema_re, StrictVersion('1.0'))

        if schema_module is None:
            schema_module = settings.HEDWIG_PROTOBUF_SCHEMA_MODULE

        if isinstance(schema_module, str):
            schema_module = import_module(schema_module)

        self.schema_module = schema_module

        self._check_schema(schema_module)

    def _extract_data(self, message_payload: Union[bytes, str], attributes: dict) -> Tuple[MetaAttributes, bytes]:
        assert isinstance(message_payload, bytes)

        if not settings.HEDWIG_USE_TRANSPORT_MESSAGE_ATTRIBUTES:
            msg_payload = PayloadV1()
            try:
                msg_payload.ParseFromString(message_payload)
            except ValueError as e:
                raise ValidationError(f"Invalid data for message: PayloadV1: {e}")

            data = msg_payload.data
            meta_attrs = MetaAttributes(
                msg_payload.metadata.timestamp.ToMilliseconds(),
                msg_payload.metadata.publisher,
                dict(msg_payload.metadata.headers),
                msg_payload.id,
                msg_payload.schema,
                msg_payload.format_version,
            )
        else:
            data = message_payload
            meta_attrs = self._decode_meta_attributes(attributes)
            if meta_attrs.format_version != self._current_format_version:
                raise ValidationError(f"Invalid format version: {meta_attrs.format_version}")
        return meta_attrs, data

    def _decode_data(
        self,
        meta_attrs: MetaAttributes,
        message_type: str,
        full_version: StrictVersion,
        data: Union[Any, bytes],
        verify_known_minor_version: bool,
    ) -> ProtoMessage:
        assert isinstance(data, (Any, bytes))

        major_version = full_version.version[0]
        msg_class_name = self._msg_class_name(message_type, major_version)

        if not hasattr(self.schema_module, msg_class_name):
            raise ValidationError(
                f"Protobuf message class not found for '{message_type}' v{major_version}. "
                f"Must be named '{msg_class_name}'"
            )

        msg_class = getattr(self.schema_module, msg_class_name)
        if verify_known_minor_version:
            options = msg_class.DESCRIPTOR.GetOptions().Extensions[options_pb2.message_options]
            if options.minor_version < full_version.version[1]:
                raise ValidationError(
                    f'Unknown minor version: {full_version.version[1]}, last known minor version: '
                    f'{options.minor_version}'
                )

        data_msg = msg_class()
        try:
            if isinstance(data, Any):
                assert data.Is(data_msg.DESCRIPTOR)
                data.Unpack(data_msg)
            else:
                data_msg.ParseFromString(data)
        except (DecodeError, RuntimeError, AssertionError) as e:
            raise ValidationError(f"Invalid data for message: {msg_class_name}: {e}")
        return data_msg

    def _encode_payload(self, meta_attrs: MetaAttributes, data: ProtoMessage) -> Tuple[bytes, dict]:
        assert isinstance(data, ProtoMessage)

        if not settings.HEDWIG_USE_TRANSPORT_MESSAGE_ATTRIBUTES:
            msg = PayloadV1()
            msg.format_version = str(self._current_format_version)
            msg.id = str(meta_attrs.id)
            msg.metadata.publisher = meta_attrs.publisher
            msg.metadata.timestamp.FromMilliseconds(meta_attrs.timestamp)
            for k, v in meta_attrs.headers.items():
                msg.metadata.headers[k] = v
            msg.schema = meta_attrs.schema
            msg.data.Pack(data)
            payload = msg.SerializeToString()
            msg_attrs = deepcopy(meta_attrs.headers)
        else:
            payload = data.SerializeToString()
            msg_attrs = self._encode_meta_attributes(meta_attrs)
        return payload, msg_attrs

    @classmethod
    def _msg_class_name(cls, msg_type: str, major_version: int) -> str:
        normalized_type = msg_type
        for ch in ('.', '_', '-'):
            normalized_type = normalized_type.replace(ch, ' ')
        normalized_type = normalized_type.title().replace(' ', '')
        return f"{normalized_type}V{major_version}"

    @classmethod
    def _check_schema(cls, schema_module: ModuleType) -> None:
        msg_types_found = {k for k in funcy.chain(settings.HEDWIG_MESSAGE_ROUTING, settings.HEDWIG_CALLBACKS)}
        errors = []
        for message_type, version_pattern in msg_types_found:
            m = cls._version_pattern_re.match(version_pattern)
            if not m:
                errors.append(f"Invalid version '{version_pattern}' for message: '{message_type}'")
                continue
            major_version = int(m.group(1))
            if major_version == 0:
                errors.append(f"Invalid version '{major_version}' for message: '{message_type}'. Must not be 0.")
            msg_class_name = cls._msg_class_name(message_type, major_version)
            if not hasattr(schema_module, msg_class_name):
                errors.append(
                    f"Protobuf message class not found for '{message_type}' v{major_version}. "
                    f"Must be named '{msg_class_name}'"
                )
                continue
            msg_class = getattr(schema_module, msg_class_name)
            if options_pb2.message_options not in msg_class.DESCRIPTOR.GetOptions().Extensions:
                errors.append(f"Protobuf message class '{msg_class_name}' does not define option message_options")
            options = msg_class.DESCRIPTOR.GetOptions().Extensions[options_pb2.message_options]
            if not options.major_version:  # default is 0 which is invalid
                errors.append(
                    f"Protobuf message class '{msg_class_name}' does not define option message_options.major_version"
                )
            elif options.major_version != major_version:
                errors.append(
                    f"Protobuf message class '{msg_class_name}' option message_options.major_version isn't valid: "
                    f"{options.major_version}, expected: {major_version}"
                )
            # minor_version default value is 0, which is valid and type is already validated by protoc,
            # so nothing to do here for minor_version.

        if errors:
            raise SchemaError(str(errors))