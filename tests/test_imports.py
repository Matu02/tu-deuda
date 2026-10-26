import subprocess
import sys


def test_bulk_import_does_not_require_new_error_names_in_cached_client():
    # Reproducir el módulo cliente previamente cargado sin las excepciones nuevas.
    script = """
import bcra
import bcra_errors
assert bcra.BCRAError is bcra_errors.BCRAError
for name in ('BCRACooldown', 'BCRACancelled', 'BCRARateLimit'):
    delattr(bcra, name)
import bulk
import bulk_ui
assert bulk.BCRACooldown is bcra_errors.BCRACooldown
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
