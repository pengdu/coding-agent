import unittest
from module import VALUE
class TestValue(unittest.TestCase):
    def test_value(self):
        self.assertEqual(VALUE, "new")
