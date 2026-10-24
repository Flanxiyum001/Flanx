"""Security regression tests for PR #23 attachment handling."""

import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import multimodal


class MultimodalPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_analysis_uses_source_data_policy_and_png_payload(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image_buffer, format="JPEG")
        image_bytes = image_buffer.getvalue()

        processor = multimodal.MultimodalProcessor()
        reply = SimpleNamespace(text="Image looks fine.")
        processor.router.generate = AsyncMock(return_value=reply)

        result = await processor.analyze_image(image_bytes, "screenshot.jpg")

        self.assertEqual(result.input_type, "image")
        kwargs = processor.router.generate.await_args.kwargs
        self.assertEqual(kwargs["data_class"], "source")
        self.assertEqual(kwargs["provider"], "local")
        self.assertEqual(kwargs["task"], "vision")
        self.assertIsInstance(kwargs["messages"][1]["content"], str)
        self.assertIsInstance(kwargs["messages"][1]["images"], list)
        self.assertEqual(len(kwargs["messages"][1]["images"]), 1)
        import base64
        self.assertTrue(base64.b64decode(kwargs["messages"][1]["images"][0]).startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_attachment_uses_type_specific_size_limit_and_actual_bytes(self):
        processor = multimodal.MultimodalProcessor()
        attachment = SimpleNamespace(
            filename="sample.py",
            size=1,
            content_type="text/plain",
            url="https://example.invalid/sample.py",
            read=AsyncMock(return_value=b"x" * (multimodal.MAX_FILE_SIZE + 1)),
        )
        with self.assertRaisesRegex(ValueError, "size limit"):
            await processor.process_attachment(attachment, 42)

    async def test_oversized_image_is_rejected_using_image_limit(self):
        processor = multimodal.MultimodalProcessor()
        attachment = SimpleNamespace(
            filename="sample.png",
            size=multimodal.MAX_IMAGE_SIZE + 1,
            content_type="image/png",
            url="https://example.invalid/sample.png",
            read=AsyncMock(),
        )
        with self.assertRaisesRegex(ValueError, "size limit"):
            await processor.process_attachment(attachment, 42)
        attachment.read.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
