"""Basic cc-cortex setup example.

Shows how to programmatically configure cc-cortex modules
and verify the installation.
"""

from cc_cortex.core.config import DEFAULT_CONFIG


def show_default_config():
    """Print the default cc-cortex configuration."""
    print("=== Default Configuration ===")
    for key, value in DEFAULT_CONFIG.items():
        if isinstance(value, dict):
            print(f"\n{key}:")
            for k, v in value.items():
                print(f"  {k}: {v}")
        elif isinstance(value, list):
            print(f"\n{key}:")
            for item in value:
                print(f"  - {item}")
        else:
            print(f"{key}: {value}")


def check_installation():
    """Verify cc-cortex modules are importable."""
    modules = [
        ("core.config", "Config"),
        ("sentinel", "check_sentinel"),
        ("security", "scan_injection"),
        ("safe_exec", "classify_command"),
        ("stop_guard", "check_stop"),
        ("knowledge", "extract_corrections"),
        ("multi_instance", "track_file"),
    ]

    print("\n=== Module Check ===")
    for mod_path, func_name in modules:
        try:
            mod = __import__(f"cc_cortex.{mod_path}", fromlist=[func_name])
            getattr(mod, func_name)
            print(f"  [OK] cc_cortex.{mod_path}.{func_name}")
        except (ImportError, AttributeError) as e:
            print(f"  [FAIL] cc_cortex.{mod_path}.{func_name}: {e}")


if __name__ == "__main__":
    show_default_config()
    check_installation()
