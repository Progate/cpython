"""BrowserOS の上の WASI の ``_posixsubprocess``（CPython の C の実装は fork で子を起こすので組めない）。

``fork_exec`` は ``_browseros_subprocess`` が posix_spawn で行う。multiprocessing の spawn と joblib の loky は
このモジュールを直に import して子を起こす。``subprocess`` は ``_can_fork_exec`` が偽の wasi ではこれを使わず、
``_browseros_subprocess.install`` が差し替えた ``Popen._execute_child`` で子を起こす（→ Lib/subprocess.py の ``_browseros``）。
"""
from _browseros_subprocess import fork_exec

__all__ = ['fork_exec']
