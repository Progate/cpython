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

- ``pass_fds`` の fd は、同じ番号への ``dup2`` で子に渡す（カーネルは file actions で置いた fd だけを子に渡す）

BrowserOS で表せないものは ``OSError``（``ENOTSUP``）になる。``preexec_fn``・利用者やグループの切り替え・
``umask``・``start_new_session``・``process_group``、ソケットやディレクトリの fd を子の標準入出力にすること、
である。子は 0 / 1 / 2 と ``pass_fds`` しか継がない（``close_fds=False`` でも、親の他の fd は子に見えない）。

``fork_exec`` は ``_posixsubprocess.fork_exec`` と同じ引数で同じことをする（→ ``_posixsubprocess.py``）。
multiprocessing の spawn（``multiprocessing.util.spawnv_passfds``）と joblib の loky は、``Popen`` を通さずに
これを直に呼んで子を起こす。
"""
import errno
import os
import signal
import sys


def _unsupported(what):
    return OSError(errno.ENOTSUP, f"{what} is not supported on BrowserOS")


def _file_actions(p2cread, c2pwrite, errwrite, pass_fds=()):
    """子の 0 / 1 / 2 と、そのまま渡す fd を決める file actions を並べる。

    ``dup2`` は並べた順に子の中で行われるので、繋ぐ元が 0 / 1 / 2 のどれかだと、先に上書きした fd を
    後で読んでしまう（``_posixsubprocess`` が ``dup`` で避けている #12607 と同じ問題）。そこで
    いったん仮の番号へ写してから 0 / 1 / 2 へ移し、仮の番号は閉じる。仮の番号は、渡す fd と繋ぐ元の
    どれよりも大きい番号にする（決まった番号にすると、その番号の fd を ``pass_fds`` で渡せなくなる）
    """
    # 同じ番号への dup2 は、その fd を子に置くだけ（posix_spawn_file_actions_adddup2(3)）
    actions = [(os.POSIX_SPAWN_DUP2, fd, fd) for fd in pass_fds]
    scratch_base = max(2, p2cread, c2pwrite, errwrite, *pass_fds) + 1
    moves = []
    for target, source in ((0, p2cread), (1, c2pwrite), (2, errwrite)):
        if source == -1 or source == target:
            continue
        actions.append((os.POSIX_SPAWN_DUP2, source, scratch_base + target))
        moves.append((target, scratch_base + target))
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

    self.pid = _spawn(executable_list, args, environ, _file_actions(p2cread, c2pwrite, errwrite, pass_fds),
                      restore_signals, cwd, orig_executable)
    self._child_created = True
    self._close_pipe_fds(p2cread, p2cwrite,
                         c2pread, c2pwrite,
                         errread, errwrite)


def _spawn(executable_list, args, environ, file_actions, restore_signals, cwd, display):
    """``executable_list`` を順に試して子を起こし、pid を返す。

    @param executable_list [tuple] 試す実行ファイルのパス（bytes）
    @param args [list] 子の argv
    @param environ [Mapping] 子の環境
    @param file_actions [list] ``os.posix_spawn`` の file actions
    @param restore_signals [bool] 親が無視にしたシグナルを子で既定に戻すか
    @param cwd [str | bytes | PathLike | None] 子の作業ディレクトリ
    @param display 起こせなかったときに例外へ載せる名前
    @return [int] 子の pid
    """
    kwargs = {'file_actions': file_actions}
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
                return os.posix_spawn(candidate, args, environ, **kwargs)
            except OSError as error:
                if error.errno == errno.ENOTSUP:
                    # 実行するものではなく、0 / 1 / 2 の繋ぎ方が BrowserOS で表せない
                    raise _unsupported("this stdin / stdout / stderr (pipes, files, DEVNULL and the "
                                       "parent's own streams can be given to a child)") from None
                if error.errno not in (errno.ENOENT, errno.ENOTDIR) and saved is None:
                    saved = error.errno
                last = error.errno
        errno_num = saved if saved is not None else last
        raise OSError(errno_num, os.strerror(errno_num), display)
    finally:
        if previous_cwd is not None:
            os.chdir(previous_cwd)


def fork_exec(args, executable_list, close_fds, pass_fds, cwd, env,
              p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
              errpipe_read, errpipe_write, restore_signals, call_setsid, pgid_to_set,
              gid, extra_groups, uid, child_umask, preexec_fn, allow_vfork=True):
    """``_posixsubprocess.fork_exec`` を BrowserOS の posix_spawn で行う。

    引数は CPython 3.13 の ``_posixsubprocess.fork_exec`` と同じ。上流は子の中で exec が失敗したことを ``errpipe_write``
    で親へ知らせるが、posix_spawn は起こせなかったことをその場で返すので、ここで ``OSError`` にする（``errpipe_*`` は
    使わない）。``close_fds`` はいつも真と同じで、子には 0 / 1 / 2 と ``pass_fds`` だけが渡る。``allow_vfork`` は
    fork しないので意味を持たない

    @return [int] 子の pid
    """
    if preexec_fn is not None:
        raise _unsupported("preexec_fn")
    if gid is not None or extra_groups is not None or uid is not None or child_umask >= 0:
        raise _unsupported("changing users, groups or umask")
    if call_setsid or pgid_to_set >= 0:
        raise _unsupported("start_new_session and process_group")
    # env は `KEY=VALUE` の bytes の並び（subprocess と multiprocessing が渡す形）。None なら親の環境を継ぐ
    if env is None:
        environ = os.environ
    else:
        environ = {}
        for entry in env:
            key, _, value = os.fsencode(entry).partition(b'=')
            environ[key] = value
    return _spawn(tuple(executable_list), list(args), environ,
                  _file_actions(p2cread, c2pwrite, errwrite, tuple(pass_fds)), restore_signals, cwd,
                  executable_list[0] if executable_list else None)


def install(popen):
    """``subprocess.Popen`` の POSIX 版の ``_execute_child`` を差し替える。"""
    popen._execute_child = _execute_child
