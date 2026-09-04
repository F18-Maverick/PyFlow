import os
import json
import importlib.util

dest_dir = os.path.dirname(__file__)
added_extensions_log_file = os.path.join(dest_dir, "added_extensions.json")


def copy_extension_files(src_paths):
    """Validate extension path(s) and return them as a list.

    Args:
        src_paths: a single path string or a list of path strings.

    Returns:
        The original paths as a list (extensions are not copied).
    """
    if isinstance(src_paths, str):
        src_paths = [src_paths]
    elif not isinstance(src_paths, list):
        raise TypeError("src_paths must be a string or a list of strings.")

    for path in src_paths:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Source file '{path}' does not exist.")

    return src_paths


def add_added_extension_logs(paths):
    """Append paths to the extension registration log file."""
    if os.path.exists(added_extensions_log_file):
        with open(added_extensions_log_file, "r", encoding="utf-8") as f:
            existing_logs = json.load(f)
    else:
        existing_logs = []
    saved_logs = existing_logs + paths
    with open(added_extensions_log_file, "w", encoding="utf-8") as f:
        json.dump(saved_logs, f, indent=4)


def add_extension(src_paths):
    """Register extension file(s) in added_extensions.json.

    Args:
        src_paths: a single path string or a list of path strings.
    """
    paths = copy_extension_files(src_paths)
    add_added_extension_logs(paths)


def remove_extension(src_paths):
    """Remove registered extension path(s) from added_extensions.json.

    Args:
        src_paths: a single path string or a list of path strings.

    If the registration file does not exist, this is a no-op.
    """
    if not os.path.exists(added_extensions_log_file):
        return

    if isinstance(src_paths, str):
        src_paths = [src_paths]
    elif not isinstance(src_paths, list):
        raise TypeError("src_paths must be a string or a list of strings.")

    with open(added_extensions_log_file, "r", encoding="utf-8") as f:
        existing_logs = json.load(f)

    remaining = [p for p in existing_logs if p not in src_paths]
    with open(added_extensions_log_file, "w", encoding="utf-8") as f:
        json.dump(remaining, f, indent=4)


def load_registered_extensions(instance, instance_type):
    """Load every registered extension from added_extensions.json.

    For each registered path, the module is imported dynamically and its
    ``setup_server_commands(instance)`` or ``setup_client_commands(instance)``
    is called, depending on *instance_type*.

    Raises ImportError if the JSON file is reachable but a module cannot be
    imported or loaded, or if the required setup function is missing.
    """
    if not os.path.exists(added_extensions_log_file):
        return

    with open(added_extensions_log_file, "r", encoding="utf-8") as f:
        paths = json.load(f)

    if not paths:
        return

    setup_func_name = (
        "setup_server_commands" if instance_type == "server"
        else "setup_client_commands"
    )

    for path in paths:
        if not os.path.exists(path):
            raise ImportError(
                f"Extension file not found: {path}. "
                f"It may have been moved or deleted."
            )

        module_name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(
                f"Cannot load extension module from: {path} "
                f"(spec_from_file_location returned None)"
            )
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise ImportError(
                f"Failed to execute extension module '{path}': {e}"
            ) from e

        setup_func = getattr(module, setup_func_name, None)
        if setup_func is None:
            raise ImportError(
                f"Extension '{path}' does not define required "
                f"function '{setup_func_name}(instance)'."
            )
        try:
            setup_func(instance)
        except Exception as e:
            raise ImportError(
                f"Failed to call {setup_func_name} on extension "
                f"'{path}': {e}"
            ) from e
