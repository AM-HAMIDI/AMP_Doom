// doomgeneric_pos.c
// doomgeneric platform layer for the POS terminal
// (Quectel EC300F / Qualcomm MDM9207, Linux 3.18, ARMv7 soft-float, glibc 2.22)
//
// Hardware:
//   Display : /dev/fb0           320x240, 32 bpp, mirrored to the SPI LCD by the kernel
//   Keypad  : /dev/input/event1  matrix keypad (evdev)
//   Clock   : CLOCK_MONOTONIC
//
// Status:
//   Step 1  DG_Init, DG_SleepMs, DG_GetTicksMs, DG_SetWindowTitle, main   done
//   Step 2  DG_DrawFrame                                                  done
//   Step 3  DG_GetKey                                                     stub
//
// Built with DOOMGENERIC_RESX=320 and DOOMGENERIC_RESY=200 (see Makefile.pos),
// so DG_ScreenBuffer holds a 320x200 XRGB8888 frame.

#include "doomgeneric.h"
#include "i_system.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <linux/fb.h>

// ---------------------------------------------------------------------------
// Configuration (override with -D on the compiler command line)
// ---------------------------------------------------------------------------

#ifndef POS_FB_DEVICE
#define POS_FB_DEVICE "/dev/fb0"
#endif

#ifndef POS_KEYPAD_DEVICE
#define POS_KEYPAD_DEVICE "/dev/input/event1"
#endif

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// Framebuffer
static int      s_fbFd = -1;
static uint8_t *s_fbMem = MAP_FAILED;  // mapped framebuffer memory
static size_t   s_fbSize;              // bytes mapped
static uint32_t s_fbXres, s_fbYres;    // visible resolution (320 x 240)
static uint32_t s_fbLineLength;        // bytes per row (1280)
static uint32_t s_fbOffsetX;           // top-left corner of Doom's frame, in pixels
static uint32_t s_fbOffsetY;

// Keypad
static int s_keypadFd = -1;

// Timing
static struct timespec s_startTime;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Black out the visible screen. Signal-safe: only touches mapped memory.
static void ClearScreen(void)
{
    if (s_fbMem != MAP_FAILED)
    {
        memset(s_fbMem, 0, (size_t)s_fbLineLength * s_fbYres);
    }
}

// Runs on every normal exit, including Doom's I_Quit() and I_Error().
static void Shutdown(void)
{
    ClearScreen();

    if (s_fbMem != MAP_FAILED)
    {
        munmap(s_fbMem, s_fbSize);
        s_fbMem = MAP_FAILED;
    }
    if (s_fbFd >= 0)
    {
        close(s_fbFd);
        s_fbFd = -1;
    }
    if (s_keypadFd >= 0)
    {
        close(s_keypadFd);
        s_keypadFd = -1;
    }
}

// Ctrl+C or kill: clear the screen and leave immediately.
static void SignalHandler(int sig)
{
    ClearScreen();
    _exit(128 + sig);
}

static void InitFramebuffer(void)
{
    struct fb_var_screeninfo vinfo;
    struct fb_fix_screeninfo finfo;

    s_fbFd = open(POS_FB_DEVICE, O_RDWR);
    if (s_fbFd < 0)
    {
        I_Error("DG_Init: cannot open %s: %s", POS_FB_DEVICE, strerror(errno));
    }
    if (ioctl(s_fbFd, FBIOGET_VSCREENINFO, &vinfo) < 0)
    {
        I_Error("DG_Init: FBIOGET_VSCREENINFO failed: %s", strerror(errno));
    }
    if (ioctl(s_fbFd, FBIOGET_FSCREENINFO, &finfo) < 0)
    {
        I_Error("DG_Init: FBIOGET_FSCREENINFO failed: %s", strerror(errno));
    }

    s_fbXres       = vinfo.xres;
    s_fbYres       = vinfo.yres;
    s_fbLineLength = finfo.line_length;

    // The colour offsets tell DG_DrawFrame whether to swap channels:
    //   red 16, green 8, blue 0  -> XRGB8888, same as DG_ScreenBuffer
    //   red 0,  green 8, blue 16 -> XBGR8888, swap R and B
    printf("DG_Init: fb '%s' %ux%u (virtual %ux%u), %u bpp, line %u bytes, mem %u bytes\n",
           finfo.id, vinfo.xres, vinfo.yres, vinfo.xres_virtual, vinfo.yres_virtual,
           vinfo.bits_per_pixel, finfo.line_length, finfo.smem_len);
    printf("DG_Init: colour layout red %u/%u green %u/%u blue %u/%u alpha %u/%u (offset/length)\n",
           vinfo.red.offset, vinfo.red.length,
           vinfo.green.offset, vinfo.green.length,
           vinfo.blue.offset, vinfo.blue.length,
           vinfo.transp.offset, vinfo.transp.length);

    if (vinfo.bits_per_pixel != 32)
    {
        I_Error("DG_Init: expected a 32 bpp framebuffer, got %u bpp", vinfo.bits_per_pixel);
    }
    if (s_fbXres < DOOMGENERIC_RESX || s_fbYres < DOOMGENERIC_RESY)
    {
        I_Error("DG_Init: framebuffer %ux%u is smaller than Doom's %ux%u",
                s_fbXres, s_fbYres, DOOMGENERIC_RESX, DOOMGENERIC_RESY);
    }

    // Map only the visible screen; some drivers report an unreliable smem_len.
    s_fbSize = (size_t)s_fbLineLength * s_fbYres;
    s_fbMem  = mmap(NULL, s_fbSize, PROT_READ | PROT_WRITE, MAP_SHARED, s_fbFd, 0);
    if (s_fbMem == MAP_FAILED)
    {
        I_Error("DG_Init: mmap of %s failed: %s", POS_FB_DEVICE, strerror(errno));
    }

    // Centre the frame: 320x200 on 320x240 gives x 0, y 20.
    s_fbOffsetX = (s_fbXres - DOOMGENERIC_RESX) / 2;
    s_fbOffsetY = (s_fbYres - DOOMGENERIC_RESY) / 2;
    printf("DG_Init: Doom frame %ux%u at x=%u y=%u\n",
           DOOMGENERIC_RESX, DOOMGENERIC_RESY, s_fbOffsetX, s_fbOffsetY);

    // Make sure the panel is on (ignored if the driver does not support it).
    ioctl(s_fbFd, FBIOBLANK, FB_BLANK_UNBLANK);

    ClearScreen();
}

static void InitKeypad(void)
{
    // Non-blocking, so DG_GetKey can poll it every frame without stalling.
    s_keypadFd = open(POS_KEYPAD_DEVICE, O_RDONLY | O_NONBLOCK);
    if (s_keypadFd < 0)
    {
        // Not fatal: the demo loop still runs, it just cannot be played.
        fprintf(stderr, "DG_Init: warning: cannot open %s: %s\n",
                POS_KEYPAD_DEVICE, strerror(errno));
        return;
    }
    printf("DG_Init: keypad %s opened\n", POS_KEYPAD_DEVICE);
}

// ---------------------------------------------------------------------------
// doomgeneric interface
// ---------------------------------------------------------------------------

void DG_Init(void)
{
    clock_gettime(CLOCK_MONOTONIC, &s_startTime);

    InitFramebuffer();
    InitKeypad();

    atexit(Shutdown);
    signal(SIGINT, SignalHandler);
    signal(SIGTERM, SignalHandler);
    signal(SIGHUP, SignalHandler);
}

void DG_DrawFrame(void)
{
    static uint32_t frames = 0;
    static uint32_t lastReport = 0;
    const size_t rowBytes = DOOMGENERIC_RESX * sizeof(uint32_t);
    const uint8_t *src = (const uint8_t *)DG_ScreenBuffer;
    uint8_t *dst = s_fbMem
                 + (size_t)s_fbOffsetY * s_fbLineLength
                 + (size_t)s_fbOffsetX * sizeof(uint32_t);
    uint32_t now;
    int y;

    // DG_ScreenBuffer is XRGB8888 and the panel reported red 16, green 8,
    // blue 0, so pixels are copied as they are, one row at a time.
    for (y = 0; y < DOOMGENERIC_RESY; y++)
    {
        memcpy(dst, src, rowBytes);
        src += rowBytes;
        dst += s_fbLineLength;
    }

    // Report the frame rate every 5 seconds.
    now = DG_GetTicksMs();
    frames++;
    if (now - lastReport >= 5000)
    {
        printf("DG_DrawFrame: %u frames in %u ms (%.1f fps)\n",
               frames, now - lastReport, frames * 1000.0f / (now - lastReport));
        frames = 0;
        lastReport = now;
    }
}

void DG_SleepMs(uint32_t ms)
{
    struct timespec req, rem;

    req.tv_sec  = ms / 1000;
    req.tv_nsec = (long)(ms % 1000) * 1000000L;

    // Resume the sleep if a signal interrupts it.
    while (nanosleep(&req, &rem) < 0 && errno == EINTR)
    {
        req = rem;
    }
}

uint32_t DG_GetTicksMs(void)
{
    struct timespec now;

    // CLOCK_MONOTONIC does not jump when the terminal syncs its wall clock.
    clock_gettime(CLOCK_MONOTONIC, &now);

    return (uint32_t)((now.tv_sec - s_startTime.tv_sec) * 1000
                      + (now.tv_nsec - s_startTime.tv_nsec) / 1000000);
}

int DG_GetKey(int *pressed, unsigned char *doomKey)
{
    // Step 3: read evdev events from s_keypadFd and translate them.
    (void)pressed;
    (void)doomKey;
    return 0;  // no key event pending
}

void DG_SetWindowTitle(const char *title)
{
    // No window on the terminal; just log it.
    printf("DG_SetWindowTitle: %s\n", title);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

int main(int argc, char **argv)
{
    // Line-buffered so logs appear immediately over the USB shell.
    setvbuf(stdout, NULL, _IOLBF, 0);

    doomgeneric_Create(argc, argv);

    for (;;)
    {
        doomgeneric_Tick();
    }

    return 0;
}