/*
 * C の関数ポインタを、その関数の本当の引数の数で呼ぶ（CPython の「call trampoline」の WASI 版）。
 *
 * C 拡張は `static PyObject *f(PyObject *self)` を `(PyCFunction)f` にキャストして METH_NOARGS で登録する
 * ことが多い（kiwisolver・cppy など）。CPython はそれを `f(self, NULL)` の 2 引数で呼ぶ。ネイティブの ABI は
 * 余った引数を無視するので動くが、wasm の call_indirect は型を厳密に照合するので
 * 「null function or function signature mismatch」で止まる。CPython は Emscripten のときだけこれを
 * 手当てしている（Python/emscripten_trampoline.c）ので、pycore_emscripten_trampoline.h で同じ差し込み口を
 * WASI に開け、中身をここに置く。
 *
 * 引数の数は wasm の中からは分からない。そこでホスト（@progate/browser-dyld の `browser_dyld.arity`）に
 * 間接関数テーブルの関数の `length` を聞く。答えは関数ごとに変わらないので覚えておき、
 * ホストへ聞くのは関数ごとに最初の 1 回だけにする（毎回ホストを挟むと C の関数呼び出しのたびに
 * JS との境界をまたぐことになる）。覚えた答えが古くなることは無い。browser-dyld は dlclose で空けた
 * テーブルの枠を使い回さず、新しいモジュールにはいつもテーブルを伸ばして枠を割り当てるので、
 * 同じ添字が別の関数を指すことが無いからである。
 *
 * 引数と戻り値はどれも i32（ポインタか int）なので、数さえ合えば型は合う。
 */
#include "Python.h"
#include "pycore_emscripten_trampoline.h"

#include <stddef.h>
#include <stdint.h>

__attribute__((import_module("browser_dyld"), import_name("arity")))
extern int32_t __browser_dyld_arity(uint32_t function_index);

typedef PyObject *(*call0)(void);
typedef PyObject *(*call1)(PyObject *);
typedef PyObject *(*call2)(PyObject *, PyObject *);

/* 覚えておく関数の数。拡張 1 つで数百、numpy を読んでも数千なので、溢れたら上書きしてよい大きさにする */
#define CACHE_SIZE 8192

/*
 * 関数の番号（上位 32 ビット）と引数の数（下位 32 ビット）を 1 語にまとめて覚える。
 * 2 つを別々に書くと、スレッド版では別のスレッドが「番号は新しく、引数の数は古い」途中の枠を読み、
 * 違う数の引数で呼んで trap する。1 語なら不可分に読み書きできる（wasm の i64 の atomic）
 */
static uint64_t cache[CACHE_SIZE];

/**
 * 関数の引数の数。分からなければ 3（CPython が渡す最大の数。呼び分けずにそのまま呼ぶ）。
 *
 * @param function 間接関数テーブルの添字（wasm の関数ポインタの値）
 * @return 引数の数
 */
static int32_t
arity_of(uintptr_t function)
{
    size_t slot = (function * 2654435761u) % CACHE_SIZE;
    uint64_t entry = __atomic_load_n(&cache[slot], __ATOMIC_RELAXED);
    // 0 は空の枠。関数ポインタ 0 は NULL なので、覚える対象にならない
    if (entry != 0 && (uint32_t)(entry >> 32) == (uint32_t)function) {
        return (int32_t)(uint32_t)entry;
    }
    int32_t arity = __browser_dyld_arity((uint32_t)function);
    if (arity < 0 || arity > 3) {
        arity = 3;
    }
    __atomic_store_n(&cache[slot], ((uint64_t)(uint32_t)function << 32) | (uint32_t)arity, __ATOMIC_RELAXED);
    return arity;
}

/**
 * `func` を `(self, args, kw)` のうち、func が受け取る数だけ渡して呼ぶ（宣言は pycore_emscripten_trampoline.h）
 */
PyObject *
_PyWASI_TrampolineCall(PyCFunctionWithKeywords func, PyObject *self, PyObject *args, PyObject *kw)
{
    switch (arity_of((uintptr_t)func)) {
    case 0:
        return ((call0)(void (*)(void))func)();
    case 1:
        return ((call1)(void (*)(void))func)(self);
    case 2:
        return ((call2)(void (*)(void))func)(self, args);
    default:
        return func(self, args, kw);
    }
}
