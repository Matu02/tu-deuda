from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_network_waits():
    """Las respuestas son simuladas; el control de ritmo se prueba por separado."""
    with patch("bcra._REQUEST_GATE"):
        yield
