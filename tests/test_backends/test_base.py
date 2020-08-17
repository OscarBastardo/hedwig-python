import json
import threading
from unittest import mock

import funcy
import pytest

from hedwig.backends import base
from hedwig.backends.base import HedwigConsumerBaseBackend, HedwigPublisherBaseBackend
from hedwig.backends.utils import get_consumer_backend, get_publisher_backend
from hedwig.models import ValidationError
from hedwig.exceptions import LoggingException, RetryException, IgnoreException
from tests.utils import mock_return_once


class MockBackend(HedwigConsumerBaseBackend, HedwigPublisherBaseBackend):
    pass


class TestBackends:
    def test_success_get_consumer_backend(self, settings):
        settings.HEDWIG_CONSUMER_BACKEND = "tests.test_backends.test_base.MockBackend"

        consumer_backend = get_consumer_backend()

        assert isinstance(consumer_backend, MockBackend)

    def test_success_get_publisher_backend(self, settings):
        settings.HEDWIG_PUBLISHER_BACKEND = "tests.test_backends.test_base.MockBackend"

        publisher_backend = get_publisher_backend()

        assert isinstance(publisher_backend, MockBackend)

    @pytest.mark.parametrize("get_backend_fn", [get_publisher_backend, get_consumer_backend])
    def test_failure(self, get_backend_fn, settings):
        settings.HEDWIG_PUBLISHER_BACKEND = settings.HEDWIG_CONSUMER_BACKEND = "hedwig.backends.invalid"

        with pytest.raises(ImportError):
            get_backend_fn()


@mock.patch('hedwig.backends.base.Message.exec_callback', autospec=True)
class TestMessageHandler:
    def test_success(self, mock_exec_callback, message, consumer_backend, use_transport_message_attrs):
        provider_metadata = mock.Mock()
        consumer_backend.message_handler(*message.serialize(), provider_metadata)
        mock_exec_callback.assert_called_once_with(message.with_provider_metadata(provider_metadata))

    @mock.patch('hedwig.validators.jsonschema.JSONSchemaValidator.deserialize', autospec=True)
    def test_fails_on_validation_error(self, mock_deserialize, mock_exec_callback, message, consumer_backend):
        error_message = 'Invalid message body'
        mock_deserialize.side_effect = ValidationError(error_message)
        with pytest.raises(ValidationError):
            consumer_backend.message_handler(*message.serialize(), None)
        mock_exec_callback.assert_not_called()

    def test_fails_on_task_failure(self, mock_exec_callback, message, consumer_backend):
        mock_exec_callback.side_effect = Exception
        with pytest.raises(mock_exec_callback.side_effect):
            consumer_backend.message_handler(*message.serialize(), None)


pre_process_hook = mock.MagicMock()
post_process_hook = mock.MagicMock()


class TestFetchAndProcessMessages:
    def test_success(self, consumer_backend):
        num_messages = 3
        visibility_timeout = 4
        shutdown_event = threading.Event()

        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [mock.MagicMock(), mock.MagicMock()], [], shutdown_event)
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.ack_message = mock.MagicMock()

        consumer_backend.fetch_and_process_messages(num_messages, visibility_timeout, shutdown_event)

        consumer_backend.pull_messages.assert_called_with(
            num_messages=num_messages, visibility_timeout=visibility_timeout, shutdown_event=shutdown_event
        )
        consumer_backend.process_message.assert_has_calls(
            [mock.call(x) for x in consumer_backend.pull_messages.return_value]
        )
        consumer_backend.ack_message.assert_has_calls(
            [mock.call(x) for x in consumer_backend.pull_messages.return_value]
        )

    def test_preserves_messages(self, consumer_backend):
        consumer_backend.pull_messages = mock.MagicMock()
        shutdown_event = threading.Event()
        mock_return_once(consumer_backend.pull_messages, [mock.MagicMock()], [], shutdown_event)
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.process_message.side_effect = Exception

        consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

        consumer_backend.pull_messages.return_value[0].delete.assert_not_called()

    def test_ignore_delete_error(self, consumer_backend):
        queue_message = mock.MagicMock()
        shutdown_event = threading.Event()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [queue_message], [], shutdown_event)
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.ack_message = mock.MagicMock(side_effect=Exception)

        with mock.patch.object(base.logger, 'exception') as logging_mock:
            consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

            logging_mock.assert_called_once()

        consumer_backend.ack_message.assert_called_once_with(queue_message)

    def test_pre_process_hook(self, consumer_backend, settings):
        shutdown_event = threading.Event()
        pre_process_hook.reset_mock()
        settings.HEDWIG_PRE_PROCESS_HOOK = 'tests.test_backends.test_base.pre_process_hook'
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [mock.MagicMock(), mock.MagicMock()], [], shutdown_event)

        consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

        pre_process_hook.assert_has_calls(
            [
                mock.call(**consumer_backend.pre_process_hook_kwargs(x))
                for x in consumer_backend.pull_messages.return_value
            ]
        )

    def test_pre_process_hook_exception(self, consumer_backend, settings):
        shutdown_event = threading.Event()
        pre_process_hook.reset_mock()
        pre_process_hook.side_effect = RuntimeError('fail')
        queue_message = mock.MagicMock()
        settings.HEDWIG_PRE_PROCESS_HOOK = 'tests.test_backends.test_base.pre_process_hook'
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [queue_message], [], shutdown_event)

        with mock.patch.object(base.logger, 'exception') as logging_mock:
            consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

            logging_mock.assert_called_once_with(
                'Exception in pre process hook for message', extra={'queue_message': queue_message}
            )

        pre_process_hook.assert_called_once_with(**consumer_backend.pre_process_hook_kwargs(queue_message))
        queue_message.delete.assert_not_called()

    def test_post_process_hook(self, consumer_backend, settings):
        shutdown_event = threading.Event()
        post_process_hook.reset_mock()
        settings.HEDWIG_POST_PROCESS_HOOK = 'tests.test_backends.test_base.post_process_hook'
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [mock.MagicMock(), mock.MagicMock()], [], shutdown_event)

        consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

        post_process_hook.assert_has_calls(
            [
                mock.call(**consumer_backend.post_process_hook_kwargs(x))
                for x in consumer_backend.pull_messages.return_value
            ]
        )

    def test_post_process_hook_exception(self, consumer_backend, settings):
        shutdown_event = threading.Event()
        settings.HEDWIG_POST_PROCESS_HOOK = 'tests.test_backends.test_base.post_process_hook'
        consumer_backend.process_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        queue_message = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [queue_message], [], shutdown_event)
        post_process_hook.reset_mock()
        post_process_hook.side_effect = RuntimeError('fail')

        with mock.patch.object(base.logger, 'exception') as logging_mock:
            consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

            logging_mock.assert_called_once_with(
                'Exception in post process hook for message', extra={'queue_message': queue_message}
            )

        post_process_hook.assert_called_once_with(**consumer_backend.pre_process_hook_kwargs(queue_message))
        queue_message.delete.assert_not_called()

    def test_special_handling_logging_error(self, consumer_backend):
        shutdown_event = threading.Event()
        queue_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [queue_message], [], shutdown_event)
        consumer_backend.process_message = mock.MagicMock(
            side_effect=LoggingException('foo', extra={'mickey': 'mouse'})
        )

        with mock.patch.object(base.logger, 'exception') as logging_mock:
            consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

            logging_mock.assert_called_once_with('foo', extra={'mickey': 'mouse'})

    def test_special_handling_retry_error(self, consumer_backend):
        shutdown_event = threading.Event()
        queue_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [queue_message], [], shutdown_event)
        consumer_backend.process_message = mock.MagicMock(side_effect=RetryException)

        with mock.patch.object(base.logger, 'info') as logging_mock:
            consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

            logging_mock.assert_called_once()

    def test_special_handling_ignore_exception(self, consumer_backend):
        shutdown_event = threading.Event()
        queue_message = mock.MagicMock()
        consumer_backend.pull_messages = mock.MagicMock()
        mock_return_once(consumer_backend.pull_messages, [queue_message], [], shutdown_event)
        consumer_backend.process_message = mock.MagicMock(side_effect=IgnoreException)

        with mock.patch.object(base.logger, 'info') as logging_mock:
            consumer_backend.fetch_and_process_messages(shutdown_event=shutdown_event)

            logging_mock.assert_called_once()


default_headers = mock.MagicMock(return_value={'mickey': 'mouse'})


@pytest.fixture(name='default_headers_hook')
def _default_headers_hook(settings):
    settings.HEDWIG_DEFAULT_HEADERS = 'tests.test_backends.test_base.default_headers'
    yield default_headers
    default_headers.reset_mock()


def pre_serialize_hook(message_data):
    # clear headers to make sure we are not able to destroy message attributes
    message_data['metadata']['headers'].clear()


class TestPublisher:
    def test_publish(self, message, mock_publisher_backend, use_transport_message_attrs):
        mock_publisher_backend.publish(message)

        mock_publisher_backend._publish.assert_called_once_with(message, *message.serialize())

    def test_default_headers_hook(
        self, message, mock_publisher_backend, default_headers_hook, use_transport_message_attrs
    ):
        mock_publisher_backend.publish(message)

        default_headers_hook.assert_called_once_with(message=message)

        payload, attributes = message.with_headers(
            funcy.merge(message.headers, default_headers_hook.return_value)
        ).serialize()
        headers = {**message.headers, **default_headers_hook.return_value}

        mock_publisher_backend._publish.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY)
        assert json.loads(mock_publisher_backend._publish.call_args[0][1]) == json.loads(payload)
        if not use_transport_message_attrs:
            assert mock_publisher_backend._publish.call_args[0][2] == headers
        else:
            assert json.loads(mock_publisher_backend._publish.call_args[0][2].pop('hedwig_headers')) == {
                **json.loads(attributes.pop('hedwig_headers')),
                **default_headers_hook.return_value,
            }
            assert mock_publisher_backend._publish.call_args[0][2] == attributes
