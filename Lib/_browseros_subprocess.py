"""BrowserOS の上の WASI で ``subprocess.Popen`` の子を起こす（``subprocess`` の末尾が ``install`` を呼ぶ）。

WASI preview1 には fork も exec も無いので、CPython の ``_posixsubprocess.fork_exec`` は組めない。BrowserOS の
libc は ``pipe`` / ``posix_spawn`` / ``waitpid`` / ``kill`` を本物で持つので（→ @progate/browser-libc）、
``os.pipe`` / ``os.posix_spawn`` / ``os.waitpid`` / ``os.kill`` は posixmodule がそのまま生やす。``Popen`` の
POSIX 版はほぼそのまま動き、**差し替えるのは子を起こす ``_execute_child`` だけ**である。パイプを作る
``_get_handles``・``communicate``・``wait``・``poll``・``send_signal`` は CPython のコードが本物の fd に対して動く。

上流にも ``os.posix_spawn`` で子を起こす道（``Popen._posix_spawn``）はあるが、``cwd`` を渡せず、実行するものを
PATH から探せず、0 / 1 / 2 どうしを繋ぎ替えられない。ここでは ``_posixsubprocess`` の ``child_exec`` と同じことを
``os.posix_spawn`` の file actions で書く。

- 子の 0 / 1 / 2 を、親が渡した fd（パイプの端・``DEVNULL``・親自身の標準入出力・開いたファイル）へ ``dup2`` で繋ぐ
- 作業ディレクトリは親が ``chdir`` してから起こし、戻す（libc の posix_spawn は親のいまの cwd を子に渡す。
  ``os.posix_spawn`` に addchdir は無い。wasm32-wasi の CPython にスレッドは無いので、親の cwd が一時的に
  変わるのを他から見られることはない）
- 実行するものの探し方は ``_posixsubprocess`` と同じで、``env`` の ``PATH`` の各ディレクトリを順に試す
- ``restore_signals`` なら、親が無視している SIGPIPE / SIGXFSZ を子では既定に戻す（上流の ``_posix_spawn`` と同じ）

BrowserOS で表せないものは ``OSError``（``ENOTSUP``）になる。``preexec_fn``・``pass_fds``・利用者や
グループの切り替え・``umask``・``start_new_session``・``process_group``、ソケットやディレクトリの fd を
子の標準入出力にすること、である。子は 0 / 1 / 2 しか継がない（``close_fds=False`` でも、親の他の fd は
子に見えない）。
"""
import errno
import os
import signal
import sys

# 0 / 1 / 2 を組み替える間だけ使う、子の中の仮の番号（最後に閉じる）
_SCRATCH_FD = 100


def _unsupported(what):
    return OSError(errno.ENOTSUP, f"{what} is not supported on BrowserOS")


def _file_actions(p2cread, c2pwrite, errwrite):
    """子の 0 / 1 / 2 を決める file actions を並べる。

    ``dup2`` は並べた順に子の中で行われるので、繋ぐ元が 0 / 1 / 2 のどれかだと、先に上書きした fd を
    後で読んでしまう（``_posixsubprocess`` が ``dup`` で避けている #12607 と同じ問題）。そこで
    いったん仮の番号へ写してから 0 / 1 / 2 へ移し、仮の番号は閉じる
    """
    actions = []
    moves = []
    for target, source in ((0, p2cread), (1, c2pwrite), (2, errwrite)):
        if source == -1 or source == target:
            continue
        actions.append((os.POSIX_SPAWN_DUP2, source, _SCRATCH_FD + target))
        moves.append((target, _SCRATCH_FD + target))
    for target, scratch in moves:
        actions.append((os.POSIX_SPAWN_DUP2, scratch, target))
        actions.append((os.POSIX_SPAWN_CLOSE, scratch))
    return actions


def _signals_to_restore():
    """親が起動時に無視にしたシグナル（→ Python/pylifecycle.c の _Py_RestoreSignals）"""
    return [signum for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ')
            if (signum := getattr(signal, name, None)) is not None]


def _execute_child(self, args, executable, preexec_fn, close_fds,
                   pass_fds, cwd, env,
                   startupinfo, creationflags, shell,
                   p2cread, p2cwrite,
                   c2pread, c2pwrite,
                   errread, errwrite,
                   restore_signals,
                   gid, gids, uid, umask,
                   start_new_session, process_group):
    """BrowserOS の posix_spawn で子を起こす（POSIX 版の _execute_child の代わり）。"""
    if preexec_fn is not None:
        raise _unsupported("preexec_fn")
    if pass_fds:
        raise _unsupported("pass_fds")
    if gid is not None or gids is not None or uid is not None or umask >= 0:
        raise _unsupported("changing users, groups or umask")
    if start_new_session or process_group >= 0:
        raise _unsupported("start_new_session and process_group")

    # ここから下の引数の整え方は CPython の POSIX 版と同じ
    if isinstance(args, (str, bytes)):
        args = [args]
    elif isinstance(args, os.PathLike):
        if shell:
            raise TypeError('path-like args is not allowed when '
                            'shell is true')
        args = [args]
    else:
        args = list(args)

    if shell:
        args = ['/bin/sh', "-c"] + args
        if executable:
            args[0] = executable

    if executable is None:
        executable = args[0]

    sys.audit("subprocess.Popen", executable, args, cwd, env)
    orig_executable = executable

    environ = os.environ if env is None else env

    executable = os.fsencode(executable)
    if os.path.dirname(executable):
        executable_list = (executable,)
    else:
        # This matches the behavior of os._execvpe() and _posixsubprocess.
        executable_list = tuple(os.path.join(os.fsencode(dir), executable)
                                for dir in os.get_exec_path(env))

    kwargs = {'file_actions': _file_actions(p2cread, c2pwrite, errwrite)}
    if restore_signals:
        kwargs['setsigdef'] = _signals_to_restore()

    # 移れない cwd は、子を探す前に _posixsubprocess と同じ形の例外にする（ファイル名は cwd）。
    # 戻すのは起こした直後で、起こせなかったときも戻す
    previous_cwd = None
    if cwd is not None:
        previous_cwd = os.getcwd()
        try:
            os.chdir(cwd)
        except OSError as error:
            raise OSError(error.errno, os.strerror(error.errno), cwd) from None
    try:
        # 候補を順に試す。見つからない（ENOENT / ENOTDIR）なら次へ進み、全部だめなら最初の別の失敗
        # （EACCES など）を報告する（_posixsubprocess の child_exec と同じ）。見つからなかった起動は子を作らない
        saved = None
        last = errno.ENOENT
        for candidate in executable_list:
            try:
                self.pid = os.posix_spawn(candidate, args, environ, **kwargs)
            except OSError as error:
                if error.errno == errno.ENOTSUP:
                    # 実行するものではなく、0 / 1 / 2 の繋ぎ方が BrowserOS で表せない
                    raise _unsupported("this stdin / stdout / stderr (pipes, files, DEVNULL and the "
                                       "parent's own streams can be given to a child)") from None
                if error.errno not in (errno.ENOENT, errno.ENOTDIR) and saved is None:
                    saved = error.errno
                last = error.errno
                continue
            break
        else:
            errno_num = saved if saved is not None else last
            raise OSError(errno_num, os.strerror(errno_num), orig_executable)
    finally:
        if previous_cwd is not None:
            os.chdir(previous_cwd)

    self._child_created = True
    self._close_pipe_fds(p2cread, p2cwrite,
                         c2pread, c2pwrite,
                         errread, errwrite)


def install(popen):
    """``subprocess.Popen`` の POSIX 版の ``_execute_child`` を差し替える。"""
    popen._execute_child = _execute_child
