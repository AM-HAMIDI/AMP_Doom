# POS Device Architecture

Prepared 2026-10-30 for the doomgeneric port.

## At a glance

The terminal can run Doom with no extra libraries. It has a plain Linux framebuffer for the screen, evdev for the keypad, and enough RAM and flash. It is a Quectel EC300F module (Qualcomm MDM9207) running a debug OpenLinux image, with the vendor's POS application on top.

| Component | What it is | Role in the Doom port |
| --- | --- | --- |
| SoC | Qualcomm MDM9207 inside a Quectel EC300F module | Platform for the whole port |
| CPU | 1 × ARM Cortex-A7, up to 1.3 GHz, NEON + VFPv4 | Runs Doom comfortably at 320×200 |
| ABI / libc | ARM EABI5 soft-float (armel), glibc 2.22 | Decides the cross-compiler and flags |
| Kernel | Linux 3.18.44 PREEMPT | Provides fbdev, evdev, clocks |
| RAM | 152 MB, about 92 MB available, no swap | Doom needs roughly 10–15 MB |
| Storage | Read-only system; 67 MB free on the writable data volume | Install to `/data/doom` |
| Display | 320×240, 32 bpp framebuffer mirrored to an SPI LCD | Target of `DG_DrawFrame` |
| Keypad | 3×5 matrix keypad on evdev | Source for `DG_GetKey` |
| Touchscreen | TSC2007 resistive, calibrated with tslib | Optional extra input |
| Audio | No sound card registered | Build without sound |
| POS application | Vendor app that owns the screen and keypad | Must be paused while Doom runs |

## Processor, ABI and toolchain

The binary must be built for ARMv7 soft-float (armel) and linked statically, because the device uses an old glibc and has no compiler.

- **CPU.** One ARM Cortex-A7 core, 1.3 GHz maximum, idling at 800 MHz under the `interactive` governor. It has NEON and VFPv4 floating point. Doom originally ran on a 33 MHz 486, so CPU time is not a concern.
- **ABI.** The ELF flags of system binaries carry no hard-float marker, and the dynamic loader is `ld-linux.so.3`. Both mean the soft-float calling convention (armel), not armhf.
- **libc.** glibc 2.22 (2015). A program linked dynamically against a modern Ubuntu glibc would refuse to start.
- **Toolchain decision.** Use `arm-linux-gnueabi-gcc` with `-march=armv7-a -mtune=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=softfp`, and link with `-static`. The `softfp` mode still uses the FPU for math while staying ABI-compatible. If the vendor provides the Quectel MDM9x07 OpenLinux SDK, its toolchain can produce dynamic binaries instead.
- **Performance tip.** Switch the CPU governor to `performance` while playing and back to `interactive` afterwards.

## Memory (RAM)

About 92 MB of the 152 MB is available to a new program, far more than Doom needs.

- Most of the "used" memory is reclaimable file cache, so the low "free" figure is not a problem.
- There is no swap. If memory ever runs out, the kernel kills a process immediately rather than slowing down.
- doomgeneric reserves a fixed zone heap (6 MB by default) plus the screen buffers and WAD data. Total use with the shareware WAD stays around 10–15 MB.

## Storage

Install Doom in `/data/doom`: it is the only persistent, writable place, with 67 MB free.

| Area | Type | Writable | Use for the port |
| --- | --- | --- | --- |
| `/` (system) | Compressed read-only image, full | No | Nothing |
| `/firmware` | Read-only modem firmware | No | Nothing |
| `/data`, `/etc`, `/home`, `/vendor` | One shared UBIFS flash volume, 67 MB free | Yes, survives reboot | Binary, WAD, Doom config and saves |
| `/run`, `/var/volatile`, `/var/lib` | RAM disks | Yes, lost on reboot | Temporary test files only |
| Raw NAND partitions (bootloaders, modem, recovery) | Flash partitions | Never touch | Writing here can brick the terminal |

The build needs about 5 MB: roughly 1.3 MB for the static binary and 4 MB for `doom1.wad`. Doom also writes its config file and save games to its working directory, so start it from `/data/doom`.

## Display

Writing pixels into `/dev/fb0` shows them on the LCD with no extra step. The black-screen and noise tests confirmed this.

- **Geometry.** 320×240 pixels, 32 bits per pixel, 1280 bytes per row with no padding, one buffer (no double buffering). One full frame is 307,200 bytes.
- **How it reaches the glass.** The panel sits on an SPI bus at 19.2 MHz. A vendor kernel driver copies the framebuffer memory to the panel on its own, so programs only write memory.
- **Frame rate limit.** A full 32-bit frame is about 2.5 Mbit over a 19.2 Mbit/s bus. If every frame is pushed whole, expect the panel to refresh at most around 7–8 times per second. Doom stays playable but not smooth.
- **Fit for Doom.** Doom renders 320×200, so it fills the full width with a 20-pixel black band above and below.
- **Pixel format.** Assumed XRGB8888 (blue in the lowest byte), which is exactly what doomgeneric produces. The port prints the driver's own colour layout at startup to confirm it.
- **Second device.** `/dev/lcdbmp` is the vendor's boot-logo interface. The port does not need it.

## Input

The keypad is a standard Linux input device, so Doom can read it directly with no vendor library.

| Device | Node | What it reports | Plan |
| --- | --- | --- | --- |
| Matrix keypad (3 columns × 5 rows of GPIO) | `/dev/input/event1` | Digits 0–9, Cancel, Clear, Enter, Up, Down, one function key | Main game control |
| TSC2007 resistive touchscreen | `/dev/input/event2` | X, Y, pressure, touch on/off | Optional later, e.g. tap to fire |
| PMIC power key | `/dev/input/event0` | Power button only | Not used, so the device can still be switched off |

The exact key-to-code mapping comes from the driver's key bitmap and still needs a press-by-press check. A first layout: 2/8 forward and back, 4/6 turn, 5 fire, 0 use, Enter select, Cancel menu, and the remaining digits for strafing and weapon switching.

The POS application also reads this keypad. While Doom runs, it must be paused, or it will react to the same key presses.

## Audio

The port will be silent. No sound card is registered with the kernel, even though the ALSA library and Qualcomm audio calibration files are present. The audio codec is most likely reserved for modem voice calls. doomgeneric is built without its sound feature. If the terminal has a buzzer, it could later give simple beeps through the vendor firmware API.

## Software stack

The terminal runs Quectel's OpenLinux on an EC300F module, with the POS vendor's own firmware and application layered on top.

- **Base system.** Quectel project EC300F_ALL_IN_ONE, MDM9x07 platform, release branch R07, built February 2024. BusyBox supplies the shell tools; there is no compiler, debugger or Python on the device.
- **Vendor firmware.** Image version 5.0.14, built March 2025, marked as a Debug build. The debug build is why a root shell is available over USB.
- **Graphics libraries.** DirectFB 1.4 and libpng are installed and the DirectFB kernel helper is active, so the POS UI probably draws through DirectFB. SDL is not installed. The libraries named "drm" are Qualcomm digital-rights libraries, not graphics.
- **POS application.** It redraws its idle screen every few seconds, overwriting anything else on the display. It must be paused while Doom runs, not killed, because init may restart it. If it also feeds a hardware watchdog, a long pause could reboot the terminal; this is still to be checked.
- **File transfer.** The USB shell tool works but is slow for large files. ADB is mounted on the device and may offer a much faster path.

## Porting decisions

The port is one C file, `doomgeneric_pos.c`, using only the kernel's framebuffer and input interfaces, built static for armel.

```
                         doomgeneric_pos.c
                       +----------------------------+
+---------------+      |  DG_Init                   | ---> Framebuffer /dev/fb0
|  Doom engine  |      |    open + mmap fb0,        |      320x240, 32 bpp,
|  renders      |      |    open keypad             |      copied to SPI LCD
|  320x200 into | <--> |  DG_DrawFrame              | ---> (rows 20-219)
|  DG_Screen-   |      |    copy frame to fb0       |
|  Buffer, polls|      |  DG_GetKey                 | <--- Keypad /dev/input/event1
|  keys + time  |      |    evdev codes -> Doom keys|
+---------------+      |  DG_GetTicksMs/DG_SleepMs  | ---> Kernel clock
                       |    monotonic clock         |      CLOCK_MONOTONIC
                       +----------------------------+
```

- **Resolution.** Doom keeps its native 320×200 (`DOOMGENERIC_RESX=320`, `DOOMGENERIC_RESY=200`), drawn 1:1 with a 20-pixel band above and below.
- **No external libraries.** No SDL, no DirectFB, no vendor SDK: only `open`, `mmap`, `ioctl` and `read`.
- **No sound.** Built without the sound feature.
- **Coexisting with the POS app.** It is paused with `SIGSTOP` before Doom starts and resumed with `SIGCONT` after Doom exits.
- **Step order.**
  1. File skeleton, `DG_Init`, timing.
  2. `DG_DrawFrame`.
  3. `DG_GetKey`.
  4. Build, deploy and launcher script.

## Open items

- [ ] Confirm the pixel colour order (red test, or the colour layout the port prints at startup)
- [ ] Find the POS application's process ID and check that pausing it keeps the screen and keypad free
- [ ] Check whether pausing the POS application triggers a watchdog reboot
- [ ] Record the real key code of every keypad button
- [ ] Measure how fast the SPI panel actually refreshes
- [ ] Choose the file transfer path for the 5 MB build (ADB, or a faster shell push)
