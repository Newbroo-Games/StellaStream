"""A tiny 4K Atari 2600 test ROM, assembled here so the tests need no game.

Every frame is 262 lines (3 VSYNC, 37 VBLANK, 192 shown, 30 overscan). Each
frame adds 1 to RAM $80 and sets the background colour to it, so the picture
and the RAM can be checked against each other. Channel 0 plays a steady tone
(AUDC0 4, AUDF0 10, AUDV0 8); channel 1 is silent.
"""
from pathlib import Path

VSYNC, VBLANK, WSYNC, COLUBK = 0x00, 0x01, 0x02, 0x09
AUDC0, AUDF0, AUDV0 = 0x15, 0x17, 0x19
COUNTER = 0x80
TONE_VOLUME = 8

# Hand-assembled 6502 at $F000; each line is one instruction
PROGRAM = [
    0x78,                     # F000 sei
    0xD8,                     # F001 cld
    0xA2, 0x00,               # F002 ldx #0
    0x8A,                     # F004 txa
    0xA8,                     # F005 tay
    0xCA,                     # F006 clear: dex
    0x9A,                     # F007 txs
    0x48,                     # F008 pha            ; zero TIA and RAM
    0xD0, 0xFB,               # F009 bne clear
    0xA9, 0x04,               # F00B lda #4
    0x85, AUDC0,              # F00D sta AUDC0
    0xA9, 0x0A,               # F00F lda #10
    0x85, AUDF0,              # F011 sta AUDF0
    0xA9, TONE_VOLUME,        # F013 lda #8
    0x85, AUDV0,              # F015 sta AUDV0
    0xA9, 0x02,               # F017 frame: lda #2
    0x85, VSYNC,              # F019 sta VSYNC
    0x85, WSYNC,              # F01B sta WSYNC      ; 3 lines of VSYNC
    0x85, WSYNC,              # F01D sta WSYNC
    0x85, WSYNC,              # F01F sta WSYNC
    0xA9, 0x00,               # F021 lda #0
    0x85, VSYNC,              # F023 sta VSYNC
    0xA9, 0x02,               # F025 lda #2
    0x85, VBLANK,             # F027 sta VBLANK
    0xA2, 37,                 # F029 ldx #37
    0x85, WSYNC,              # F02B vb: sta WSYNC
    0xCA,                     # F02D dex
    0xD0, 0xFB,               # F02E bne vb
    0xA9, 0x00,               # F030 lda #0
    0x85, VBLANK,             # F032 sta VBLANK
    0xE6, COUNTER,            # F034 inc $80
    0xA5, COUNTER,            # F036 lda $80
    0x85, COLUBK,             # F038 sta COLUBK
    0xA2, 192,                # F03A ldx #192
    0x85, WSYNC,              # F03C shown: sta WSYNC
    0xCA,                     # F03E dex
    0xD0, 0xFB,               # F03F bne shown
    0xA9, 0x02,               # F041 lda #2
    0x85, VBLANK,             # F043 sta VBLANK
    0xA2, 30,                 # F045 ldx #30
    0x85, WSYNC,              # F047 os: sta WSYNC
    0xCA,                     # F049 dex
    0xD0, 0xFB,               # F04A bne os
    0x4C, 0x17, 0xF0,         # F04C jmp frame
]


def build():
    """The ROM image: 4096 bytes, reset and IRQ vectors pointing at $F000."""
    rom = bytearray(4096)
    rom[:len(PROGRAM)] = bytes(PROGRAM)
    rom[0xFFC:0x1000] = bytes([0x00, 0xF0, 0x00, 0xF0])
    return bytes(rom)


def write(path):
    """Write the ROM to path (folders made as needed); returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build())
    return path
