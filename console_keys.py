"""Read console shortcuts without blocking on Windows, macOS, or Linux."""
import atexit
import os
import select
import sys

_restore = None


def read_keys():
    """Return keys currently typed in the controlling terminal."""
    if sys.platform == "win32":
        import msvcrt
        keys = []
        while msvcrt.kbhit():
            keys.append(msvcrt.getwch().lower())
        return keys

    global _restore
    if not sys.stdin.isatty():
        return []
    if _restore is None:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)

        def restore():
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        _restore = restore
        atexit.register(restore)
    keys = []
    while select.select([sys.stdin], [], [], 0)[0]:
        chunk = os.read(sys.stdin.fileno(), 64).decode(errors="ignore").lower()
        keys.extend(chunk)
    return keys
