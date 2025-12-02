#!/usr/bin/env python3
"""
🔒 FILE UTILS - Thread-safe file operations with locking

Provides:
- File locking for JSON files
- Safe read/write operations
- Atomic file updates
"""

import json
import fcntl
import os
from pathlib import Path
from typing import Any, Dict, Optional
from contextlib import contextmanager
from datetime import datetime


class FileLockError(Exception):
    """Error acquiring file lock"""
    pass


@contextmanager
def file_lock(filepath: Path, timeout: float = 5.0):
    """
    Context manager for file locking.
    
    Usage:
        with file_lock(Path("positions.json")):
            # Safe to read/write
            data = load_json_safe(filepath)
            data["new_key"] = "value"
            save_json_safe(filepath, data)
    """
    lock_path = Path(str(filepath) + ".lock")
    lock_file = None
    
    try:
        # Create lock file if doesn't exist
        lock_file = open(lock_path, 'w')
        
        # Try to acquire exclusive lock
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
        
    except BlockingIOError:
        # Lock is held by another process
        import time
        start = time.time()
        while time.time() - start < timeout:
            try:
                if lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    yield
                    return
            except BlockingIOError:
                time.sleep(0.1)
        
        raise FileLockError(f"Could not acquire lock for {filepath} within {timeout}s")
        
    finally:
        if lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass


def load_json_safe(filepath: Path, default: Optional[Any] = None) -> Any:
    """
    Safely load JSON file with proper error handling.
    
    Args:
        filepath: Path to JSON file
        default: Default value if file doesn't exist or is invalid
        
    Returns:
        Parsed JSON data or default value
    """
    if default is None:
        default = {}
        
    try:
        if not filepath.exists():
            return default
            
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
            
    except json.JSONDecodeError as e:
        print(f"⚠️ JSON decode error in {filepath}: {e}")
        return default
    except PermissionError as e:
        print(f"⚠️ Permission denied reading {filepath}: {e}")
        return default
    except FileNotFoundError:
        return default
    except OSError as e:
        print(f"⚠️ OS error reading {filepath}: {e}")
        return default


def save_json_safe(filepath: Path, data: Any, indent: int = 2) -> bool:
    """
    Safely save JSON file with atomic write.
    
    Args:
        filepath: Path to JSON file
        data: Data to save
        indent: JSON indentation
        
    Returns:
        True if successful, False otherwise
    """
    temp_path = Path(str(filepath) + ".tmp")
    
    try:
        # Write to temp file first
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        
        # Atomic rename
        temp_path.rename(filepath)
        return True
        
    except PermissionError as e:
        print(f"⚠️ Permission denied writing {filepath}: {e}")
        return False
    except OSError as e:
        print(f"⚠️ OS error writing {filepath}: {e}")
        return False
    except (TypeError, ValueError) as e:
        print(f"⚠️ JSON serialization error: {e}")
        return False
    finally:
        # Clean up temp file if it exists
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def load_json_locked(filepath: Path, default: Optional[Any] = None) -> Any:
    """Load JSON with file locking"""
    with file_lock(filepath):
        return load_json_safe(filepath, default)


def save_json_locked(filepath: Path, data: Any, indent: int = 2) -> bool:
    """Save JSON with file locking"""
    with file_lock(filepath):
        return save_json_safe(filepath, data, indent)


def update_json_locked(filepath: Path, update_func, default: Optional[Any] = None) -> bool:
    """
    Atomically update JSON file with locking.
    
    Args:
        filepath: Path to JSON file
        update_func: Function that takes current data and returns updated data
        default: Default value if file doesn't exist
        
    Returns:
        True if successful
        
    Usage:
        def add_position(data):
            data["new_token"] = {"amount": 100}
            return data
            
        update_json_locked(Path("positions.json"), add_position)
    """
    with file_lock(filepath):
        data = load_json_safe(filepath, default)
        updated_data = update_func(data)
        return save_json_safe(filepath, updated_data)
