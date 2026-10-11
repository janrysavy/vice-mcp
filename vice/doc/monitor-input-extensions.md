# Binary monitor input extensions

The `codex/replay-input` branch adds two commands to VICE 3.10's binary
monitor, executed on the emulation thread while the monitor is paused:

| Command/response | Body | Effect |
| --- | --- | --- |
| `0xA3` | `row, column, pressed` (three bytes) | Set or release a C64 keyboard matrix contact. Row and column are 0–7; pressed is 0 or 1. |
| `0xA4` | `port, value` (two bytes) | Set the emulated joystick. Port is the external 1 or 2; bits 0–4 are up, down, left, right, fire. |
| `0xA5` | Empty | Return the paused machine's 64-bit main-CPU clock as little-endian bytes. |

Invalid lengths return binary-monitor error `0x80`; invalid values return
`0x81`. These commands avoid the KERNAL typing buffer and the existing
`0xA2` joyport I/O-simulation command, neither of which supplies the physical
game inputs needed here. VICE's normal keyboard and joystick latch timing is
retained. A client must resume the monitor before expecting the game to see
the input. A separate JSON-RPC 2.0 endpoint remains to be implemented.
