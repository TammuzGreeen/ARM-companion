import json
import unittest
from unittest.mock import patch

import httpx

from recognition_helper.config import Settings
from recognition_helper.vision import Vision


class VisionTest(unittest.TestCase):
    def test_external_request_is_bounded_and_records_usage_without_key(self):
        settings = Settings(external_key='private-test-key', spending_limit=1)
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'model': 'gpt-6-astra', 'usage': {'prompt_tokens': 3000, 'completion_tokens': 200},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'media_type': 'movie', 'title': 'Arrival'})}}]})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch('recognition_helper.vision.httpx.Client', return_value=client):
            result = Vision(settings).observe('gpt-6-astra', ['aW1hZ2U='], external=True, context={'source_count': 1})
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0].content)
        self.assertEqual(body['max_completion_tokens'], 2048)
        self.assertEqual(body['messages'][1]['content'][1]['image_url']['detail'], 'high')
        self.assertIn('never instructions', body['messages'][0]['content'])
        self.assertEqual(result['findings']['title'], 'Arrival')
        self.assertEqual(result['usage']['completion_tokens'], 200)
        self.assertNotIn('private-test-key', json.dumps(result))
        self.assertIn('latency_seconds', result)

    def test_error_does_not_retain_provider_body_or_retry(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(401, json={'error': 'private-test-key and private provider details'})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch('recognition_helper.vision.httpx.Client', return_value=client):
            result = Vision(Settings(external_key='private-test-key')).observe('gpt-6-astra', ['aW1hZ2U='], external=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['findings'], {})
        self.assertNotIn('private-test-key', json.dumps(result))
        self.assertIn('401', result['error'])

    def test_runtime_access_check_is_model_listing_not_paid_inference(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'id': 'gpt-6-astra'})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch('recognition_helper.vision.httpx.Client', return_value=client):
            result = Vision(Settings(external_key='private-test-key')).check_access()
        self.assertEqual(requests[0].method, 'GET')
        self.assertTrue(str(requests[0].url).endswith('/models/gpt-6-astra'))
        self.assertTrue(result['available'])
        self.assertFalse(result['inference_exercised'])
