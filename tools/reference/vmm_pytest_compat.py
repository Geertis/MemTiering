"""Allow the old Xonsh collector API with the frozen pytest 9 environment.

Only its two collector classes are affected; test assertions stay unchanged.
Loaded by PYTEST_PLUGINS, rather than on every Python interpreter startup.
"""
from pathlib import Path
import pytest
from _pytest.recwarn import WarningsRecorder
from _pytest.nodes import NodeMeta

_original_call = NodeMeta.__call__

def _compatible_call(cls, *args, **kwargs):
    if cls.__module__ == 'xonsh.pytest_plugin':
        if cls.__name__ == 'XshFile' and len(args) == 2:
            return cls._create(path=Path(str(args[0])), parent=args[1], **kwargs)
        if cls.__name__ == 'XshFunction':
            return cls._create(*args, **kwargs)
    return _original_call(cls, *args, **kwargs)

NodeMeta.__call__ = _compatible_call

# Legacy pytest.warns(None) records warnings without requiring one. Keep that
# meaning while leaving all real Warning-category checks and test assertions.
_original_warns = pytest.warns
def _legacy_warns(expected_warning=Warning, *args, **kwargs):
    if expected_warning is not None:
        return _original_warns(expected_warning, *args, **kwargs)
    recorder = WarningsRecorder(_ispytest=True)
    if not args:
        kwargs.pop("match", None)
        if kwargs:
            raise TypeError("unexpected legacy warns keyword arguments")
        return recorder
    if not callable(args[0]):
        raise TypeError("legacy warns callable is not callable")
    with recorder:
        return args[0](*args[1:], **kwargs)
pytest.warns = _legacy_warns
