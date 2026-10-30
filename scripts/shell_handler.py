import serial
import threading
import base64
import time
import re
import os

WINDOWS_PORT = "COM13"
LINUX_PORT = "/dev/ttyUSB0"
ser = serial.Serial(port=LINUX_PORT, baudrate=115200, timeout=0.2)

capture_active = threading.Event()
capture_buffer = bytearray()
capture_lock = threading.Lock()

ERROR_PATTERNS = ("No such file or directory", "Permission denied", "Is a directory", "Not a directory")


def reader():
    while True:
        data = ser.read(4096)
        if not data:
            continue
        if capture_active.is_set():
            with capture_lock:
                capture_buffer.extend(data)
        else:
            print(data.decode("utf-8", errors="replace"), end="", flush=True)


t = threading.Thread(target=reader, daemon=True)
t.start()

print("Interactive root shell over USB. Type commands, Ctrl+C to quit.")
print("  !push <local_file> <remote_file_name>        push one file into base_path")
print("  !pull <remote_file_path> <local_target_dir>   pull one file, keep its name")
print("  !push_dir <local_dir> <remote_target_dir>      push a whole directory tree")
print("  !pull_dir <remote_dir> <local_target_dir>      pull a whole directory tree")

base_path = "/home/user0/utils"


# ---------------------------------------------------------------------------
# Low-level capture helper
# ---------------------------------------------------------------------------

def _run_and_capture(cmd, timeout=30):
    """
    Sends `cmd` wrapped with start/end sentinel echoes and waits for both
    sentinels to appear in the serial stream, returning the text between
    them (the command's real output).

    The target TTY echoes typed input back, so the sentinel text appears
    TWICE in the stream: once as the echoed input line, once as real
    output. We take the LAST start-marker occurrence (the real one) and
    the first end-marker occurrence after it, which skips the echo.

    Returns None on timeout.
    """
    start_marker = "===CAP_START==="
    end_marker = "===CAP_END==="

    with capture_lock:
        capture_buffer.clear()
    capture_active.set()

    full_cmd = f'echo "{start_marker}"; {cmd}; echo "{end_marker}"\n'
    ser.write(full_cmd.encode("utf-8"))

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(0.1)
            with capture_lock:
                text = capture_buffer.decode("utf-8", errors="replace")

            start_idx = text.rfind(start_marker)
            if start_idx == -1:
                continue
            end_idx = text.find(end_marker, start_idx + len(start_marker))
            if end_idx == -1:
                continue

            return text[start_idx + len(start_marker):end_idx]
    finally:
        capture_active.clear()

    return None


# ---------------------------------------------------------------------------
# Single-file pull
# ---------------------------------------------------------------------------

def pull_single_file(remote_path, local_path, timeout=30):
    """Pulls one remote file to an exact local file path. Returns True/False."""
    payload_text = _run_and_capture(f'base64 "{remote_path}"', timeout=timeout)

    if payload_text is None:
        print(f"  TIMEOUT pulling {remote_path}")
        return False

    if any(pat in payload_text for pat in ERROR_PATTERNS):
        print(f"  ERROR reading {remote_path}: {payload_text.strip()}")
        return False

    clean = re.sub(r"\s+", "", payload_text)
    if not clean:
        print(f"  EMPTY output for {remote_path} (not a regular file?)")
        return False

    try:
        raw = base64.b64decode(clean)
    except Exception as e:
        print(f"  DECODE FAILED for {remote_path}: {e}")
        return False

    local_dir = os.path.dirname(local_path)
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)

    with open(local_path, "wb") as f:
        f.write(raw)

    print(f"  OK  {remote_path} -> {local_path} ({len(raw)} bytes)")
    return True


def pull_file(remote_path, local_dir, timeout=30):
    """!pull handler: keeps the remote filename, drops it into local_dir."""
    filename = remote_path.rstrip("/").split("/")[-1]
    if not filename:
        print(f"Could not determine filename from remote path: {remote_path}")
        return
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, filename)
    pull_single_file(remote_path, local_path, timeout=timeout)


# ---------------------------------------------------------------------------
# Single-file push
# ---------------------------------------------------------------------------

def push_single_file(local_file_path, remote_file_path, chunk_size=256, chunk_delay=0.05):
    """Pushes one local file to an exact remote file path."""
    with open(local_file_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    # NOTE: fixed from the original version, which ran
    #   rm -f {local_file_path}.b64   (a Windows path — wrong target, silently failed)
    # It must clear the REMOTE staging file, or a leftover from a previous
    # failed push gets appended onto instead of overwritten.
    ser.write(f'rm -f "{remote_file_path}.b64"\n'.encode("utf-8"))
    time.sleep(0.2)

    for i in range(0, len(b64_data), chunk_size):
        chunk = b64_data[i:i + chunk_size]
        ser.write(f"echo -n '{chunk}' >> \"{remote_file_path}.b64\"\n".encode("utf-8"))
        time.sleep(chunk_delay)

    ser.write(f'base64 -d "{remote_file_path}.b64" > "{remote_file_path}"\n'.encode("utf-8"))
    time.sleep(0.2)
    ser.write(f'rm -f "{remote_file_path}.b64"\n'.encode("utf-8"))
    time.sleep(0.1)

    print(f"  OK  {local_file_path} -> {remote_file_path} ({len(b64_data)} b64 chars)")


def push_file(local_file_path, remote_file_name):
    """!push handler: pushes into base_path under the given name."""
    remote_file_path = f"{base_path}/{remote_file_name}"
    print(f"Pushing {local_file_path} ({os.path.getsize(local_file_path)} bytes)...")
    push_single_file(local_file_path, remote_file_path)


# ---------------------------------------------------------------------------
# Directory pull
# ---------------------------------------------------------------------------

def list_remote_tree(remote_dir, timeout=20):
    """Returns (dirs, files) as absolute remote paths under remote_dir, or (None, None) on timeout."""
    remote_dir = remote_dir.rstrip("/")
    dirs_out = _run_and_capture(f'find "{remote_dir}" -type d 2>/dev/null', timeout=timeout)
    files_out = _run_and_capture(f'find "{remote_dir}" -type f 2>/dev/null', timeout=timeout)

    if dirs_out is None or files_out is None:
        return None, None

    dirs = [l.strip() for l in dirs_out.splitlines() if l.strip()]
    files = [l.strip() for l in files_out.splitlines() if l.strip()]
    return dirs, files


def pull_dir(remote_dir, local_target_dir, timeout_per_file=30):
    remote_dir = remote_dir.rstrip("/")
    base_name = remote_dir.split("/")[-1]
    if not base_name:
        print(f"Could not determine directory name from: {remote_dir}")
        return

    print(f"Listing remote directory {remote_dir} ...")
    dirs, files = list_remote_tree(remote_dir)

    if dirs is None:
        print("Timed out listing remote directory.")
        return
    if not dirs and not files:
        print(f"Remote path not found or empty: {remote_dir}")
        return

    local_root = os.path.join(local_target_dir, base_name)

    # Recreate every subdirectory first, so files always have somewhere to land.
    os.makedirs(local_root, exist_ok=True)
    for d in dirs:
        rel = os.path.relpath(d, remote_dir)
        local_d = local_root if rel in (".", "") else os.path.join(local_root, rel)
        os.makedirs(local_d, exist_ok=True)

    print(f"Pulling {len(files)} file(s) into {local_root} ...")
    ok, fail = 0, 0
    for i, remote_f in enumerate(files, 1):
        rel = os.path.relpath(remote_f, remote_dir)
        local_f = os.path.join(local_root, rel)
        print(f"[{i}/{len(files)}] {remote_f}")
        if pull_single_file(remote_f, local_f, timeout=timeout_per_file):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} pulled, {fail} failed -> {local_root}")


# ---------------------------------------------------------------------------
# Directory push
# ---------------------------------------------------------------------------

def push_dir(local_dir, remote_target_dir, chunk_size=256, chunk_delay=0.05):
    if not os.path.isdir(local_dir):
        print(f"Local directory not found: {local_dir}")
        return

    base_name = os.path.basename(os.path.normpath(local_dir))
    remote_root = f"{remote_target_dir.rstrip('/')}/{base_name}"

    all_dirs = []
    all_files = []  # (local_path, remote_path)
    for root, _dirnames, filenames in os.walk(local_dir):
        rel = os.path.relpath(root, local_dir)
        remote_subdir = remote_root if rel == "." else f"{remote_root}/{rel.replace(os.sep, '/')}"
        all_dirs.append(remote_subdir)
        for fn in filenames:
            local_f = os.path.join(root, fn)
            remote_f = f"{remote_subdir}/{fn}"
            all_files.append((local_f, remote_f))

    print(f"Creating {len(all_dirs)} remote directory(ies) under {remote_root} ...")
    for d in all_dirs:
        ser.write(f'mkdir -p "{d}"\n'.encode("utf-8"))
        time.sleep(0.1)

    print(f"Pushing {len(all_files)} file(s) ...")
    for i, (local_f, remote_f) in enumerate(all_files, 1):
        print(f"[{i}/{len(all_files)}] {local_f}")
        try:
            push_single_file(local_f, remote_f, chunk_size=chunk_size, chunk_delay=chunk_delay)
        except Exception as e:
            print(f"  FAILED: {e}")

    print(f"\nDone pushing directory -> {remote_root}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

try:
    while True:
        line = input()

        if line.startswith("!push_dir"):
            parts = line.split()
            if len(parts) != 3:
                print("Usage: !push_dir <local_dir> <remote_target_dir>")
                continue
            try:
                push_dir(parts[1], parts[2])
            except Exception as e:
                print(f"Failed to push directory: {e}")

        elif line.startswith("!pull_dir"):
            parts = line.split()
            if len(parts) != 3:
                print("Usage: !pull_dir <remote_dir> <local_target_dir>")
                continue
            try:
                pull_dir(parts[1], parts[2])
            except Exception as e:
                print(f"Failed to pull directory: {e}")

        elif line.startswith("!push"):
            parts = line.split()
            if len(parts) != 3:
                print("Usage: !push <local_file> <remote_file_name>")
                continue
            try:
                push_file(parts[1], parts[2])
            except Exception as e:
                print(f"Failed to push file: {e}")

        elif line.startswith("!pull"):
            parts = line.split()
            if len(parts) != 3:
                print("Usage: !pull <remote_file_path> <local_target_dir>")
                continue
            try:
                pull_file(parts[1], parts[2])
            except Exception as e:
                print(f"Failed to pull file: {e}")

        else:
            ser.write((line + "\n").encode("utf-8"))

except KeyboardInterrupt:
    pass
finally:
    ser.close()