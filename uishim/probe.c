/* 探针：确认 launcher 是否真的调用 VO::push，以及符号解析是否成功 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
#include <time.h>
#include <stdarg.h>

static const char SYM[] = "_ZN4maix10middleware8maixcam22VO4pushEiiPNS1_5FrameE";
static FILE *lg;

static void plog(const char *fmt, ...) {
    if (!lg) lg = fopen("/tmp/shim_probe.txt", "a");
    if (!lg) return;
    va_list ap; va_start(ap, fmt); vfprintf(lg, fmt, ap); va_end(ap);
    fflush(lg);
}

__attribute__((constructor)) static void probe_ctor(void) {
    char exe[512] = {0};
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n > 0) exe[n] = 0;
    void *p1 = dlsym(RTLD_NEXT, SYM);
    void *p2 = dlsym(RTLD_DEFAULT, SYM);
    plog("ctor pid=%d exe=%s next=%p default=%p\n", (int)getpid(), exe, p1, p2);
}

int VO_push(void *self, int a1, int a2, void *frame)
    __asm__("_ZN4maix10middleware8maixcam22VO4pushEiiPNS1_5FrameE");

typedef int (*fn_t)(void *, int, int, void *);

int VO_push(void *self, int a1, int a2, void *frame) {
    static fn_t real = NULL;
    static long cnt = 0;
    if (!real) {
        real = (fn_t)dlsym(RTLD_NEXT, SYM);
        if (!real) real = (fn_t)dlsym(RTLD_DEFAULT, SYM);
        plog("VO_push first call, real=%p\n", real);
    }
    if ((++cnt % 60) == 1) {
        int len = frame ? *(int *)((uint8_t *)frame + 16) : -1;
        int w = frame ? *(int *)((uint8_t *)frame + 20) : -1;
        int h = frame ? *(int *)((uint8_t *)frame + 24) : -1;
        plog("VO_push #%ld len=%d w=%d h=%d\n", cnt, len, w, h);
    }
    if (real) return real(self, a1, a2, frame);
    return -1;
}
