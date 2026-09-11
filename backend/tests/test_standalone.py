import os
import unittest
from unittest.mock import patch

from standalone import format_startup_error


class StartupErrorTests(unittest.TestCase):
    def test_formats_general_startup_error(self):
        message = format_startup_error(RuntimeError("configuration failed"))

        self.assertIn("Application startup failed", message)
        self.assertIn("RuntimeError: configuration failed", message)

    def test_formats_mongo_connection_error_and_redacts_uri(self):
        error_type = type("ServerSelectionTimeoutError", (Exception,), {})
        uri = "mongodb+srv://user:password@example.mongodb.net/"

        with patch.dict(os.environ, {"MONGO_URI": uri}):
            message = format_startup_error(error_type(f"SSL failed for {uri}"))

        self.assertIn("Cannot connect to MongoDB", message)
        self.assertIn("<redacted MongoDB URI>", message)
        self.assertNotIn(uri, message)


if __name__ == "__main__":
    unittest.main()
