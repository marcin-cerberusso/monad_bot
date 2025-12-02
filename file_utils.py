#!/usr/bin/env python3
"""
🔒 FILE UTILS - Bezpieczny dostęp do plików z locking

Zapewnia:
- File locking (fcntl) dla uniknięcia race conditions
- Proper exception handling
- Atomic writes
"""

import json
import fcntl
import os
import tempfile
import shutil
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Optional, Union
import logging

logger = logging.getLogger(__name__)


@contextmanager
def locked_file(filepath: Union[str, Path], mode: str = 'r', timeout: float = 10.0):
    """
    Context manager dla bezpiecznego dostępu do plików z locking.
    
    Args:
        filepath: Ścieżka do pliku
        mode: Tryb otwarcia ('r', 'w', 'r+', etc.)
        timeout: Timeout w sekundach (nie używany w tej wersji - fcntl blokuje)
    
    Yields:
        File handle z aktywnym lockiem
    
    Example:
        with locked_file('positions.json', 'r') as f:
            data = json.load(f)
    """
    filepath = Path(filepath)
    
    # Dla zapisu - upewnij się że katalog istnieje
    if 'w' in mode or 'a' in mode:
        filepath.parent.mkdir(parents=True, exist_ok=True)
    
    f = None
    try:
        f = open(filepath, mode)
        # Exclusive lock dla zapisu, shared dla odczytu
        lock_type = fcntl.LOCK_EX if ('w' in mode or '+' in mode or 'a' in mode) else fcntl.LOCK_SH
        fcntl.flock(f.fileno(), lock_type)
        yield f
    finally:
        if f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
            f.close()


def safe_load_json(filepath: Union[str, Path], default: Any = None) -> Any:
    """
    Bezpieczne wczytanie JSON z locking.
    
    Args:
        filepath: Ścieżka do pliku JSON
        default: Wartość domyślna jeśli plik nie istnieje lub jest błędny
    
    Returns:
        Zawartość pliku JSON lub default
    """
    if default is None:
        default = {}
    
    filepath = Path(filepath)
    
    if not filepath.exists():
        return default
    
    try:
        with locked_file(filepath, 'r') as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except FileNotFoundError:
        logger.debug(f"File not found: {filepath}")
        return default
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error in {filepath}: {e}")
        return default
    except PermissionError as e:
        logger.error(f"Permission denied reading {filepath}: {e}")
        return default
    except IOError as e:
        logger.error(f"IO error reading {filepath}: {e}")
        return default


def safe_save_json(filepath: Union[str, Path], data: Any, indent: int = 2) -> bool:
    """
    Bezpieczny zapis JSON z locking i atomic write.
    
    Używa atomic write (zapis do temp file + rename) dla uniknięcia
    uszkodzenia pliku przy crashu.
    
    Args:
        filepath: Ścieżka do pliku JSON
        data: Dane do zapisania
        indent: Wcięcie JSON (domyślnie 2)
    
    Returns:
        True jeśli sukces, False jeśli błąd
    """
    filepath = Path(filepath)
    
    try:
        # Upewnij się że katalog istnieje
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # Atomic write: zapisz do temp file, potem rename
        fd, temp_path = tempfile.mkstemp(
            dir=filepath.parent,
            prefix=f".{filepath.name}.",
            suffix=".tmp"
        )
        
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=indent, default=str)
            
            # Atomic rename
            shutil.move(temp_path, filepath)
            return True
            
        except Exception:
            # Cleanup temp file on error
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
            
    except TypeError as e:
        logger.error(f"JSON serialization error for {filepath}: {e}")
        return False
    except PermissionError as e:
        logger.error(f"Permission denied writing {filepath}: {e}")
        return False
    except IOError as e:
        logger.error(f"IO error writing {filepath}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error writing {filepath}: {e}")
        return False


def safe_update_json(filepath: Union[str, Path], update_func, default: Any = None) -> bool:
    """
    Bezpieczna aktualizacja JSON z locking (read-modify-write).
    
    Args:
        filepath: Ścieżka do pliku JSON
        update_func: Funkcja (data) -> data która modyfikuje dane
        default: Wartość domyślna jeśli plik nie istnieje
    
    Returns:
        True jeśli sukces, False jeśli błąd
    
    Example:
        def add_position(data):
            data['positions']['0x123'] = {'amount': 10}
            return data
        
        safe_update_json('positions.json', add_position, default={})
    """
    if default is None:
        default = {}
    
    filepath = Path(filepath)
    
    try:
        # Użyj exclusive lock na czas całej operacji
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # Wczytaj istniejące dane
        data = safe_load_json(filepath, default)
        
        # Zastosuj modyfikację
        updated_data = update_func(data)
        
        # Zapisz
        return safe_save_json(filepath, updated_data)
        
    except Exception as e:
        logger.error(f"Error updating {filepath}: {e}")
        return False


# Aliasy dla kompatybilności wstecznej
load_json = safe_load_json
save_json = safe_save_json


if __name__ == "__main__":
    # Test
    import tempfile
    
    test_file = Path(tempfile.gettempdir()) / "test_file_utils.json"
    
    # Test save
    test_data = {"test": "data", "number": 42}
    assert safe_save_json(test_file, test_data), "Save failed"
    
    # Test load
    loaded = safe_load_json(test_file)
    assert loaded == test_data, f"Load mismatch: {loaded}"
    
    # Test update
    def add_field(data):
        data["new_field"] = "added"
        return data
    
    assert safe_update_json(test_file, add_field), "Update failed"
    loaded = safe_load_json(test_file)
    assert loaded.get("new_field") == "added", "Update didn't work"
    
    # Cleanup
    test_file.unlink()
    
    print("✅ All tests passed!")
