"""Create a controlling TTY outside the threaded HTTP process."""
import fcntl
import os
import sys
import termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.execvp('bash', ['bash', sys.argv[1]])
